"""Shared VM inventory and Docker lifecycle for the web console and MCP."""
import asyncio
import csv
import fcntl
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

import legacy_vm
from control_config import ROOT, STATE, legacy_lock
from qmp import Monitor
from setup_media import build_setup_iso
from transfer_http import install_relay

PROJECT = ROOT.parent
PRIVATE_NETWORKS = [ipaddress.IPv4Network(cidr) for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
VM_ID = r'[0-9a-f]{12}'
STOPPED = (None, 'created', 'exited', 'dead')


class Conflict(RuntimeError):
    pass


def named(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise ValueError('名前は1〜80文字で指定してください')
    return value.strip()


def number(value, low, high, field):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{field} は {low}〜{high} の整数で指定してください')
    return value


def topology_settings(segments, proxy):
    if not isinstance(segments, list) or not 1 <= len(segments) <= 8 or type(proxy) is not bool:
        raise ValueError('1〜8個のセグメントとプロキシの有効/無効を指定してください')
    networks = []
    for cidr in segments:
        if not isinstance(cidr, str) or '/' not in cidr:
            raise ValueError('セグメントをIPv4 CIDRで指定してください')
        network = ipaddress.IPv4Network(cidr)
        if not 16 <= network.prefixlen <= 28 or not any(network.subnet_of(n) for n in PRIVATE_NETWORKS):
            raise ValueError('セグメントには /16〜/28 のプライベートIPv4 CIDRを指定してください')
        if any(network.overlaps(n) for n in networks):
            raise ValueError('セグメント同士が重複しています')
        networks.append(network)
    return {'segments': [str(n) for n in networks], 'proxy': proxy}


def bind_mount(source, target, readonly=False):
    out = io.StringIO()
    values = ['type=bind', f'source={source}', f'target={target}']
    if readonly:
        values.append('readonly')
    csv.writer(out, lineterminator='\n').writerow(values)
    return out.getvalue().removesuffix('\n')


class VMManager:
    def __init__(self, state=None, media=None, image=None, legacy_state=STATE):
        self.state = Path(state or os.environ.get('WINDOWS_MANAGER_STATE', PROJECT / 'state-managed')).resolve()
        default_media = os.environ.get('WINDOWS_MANAGER_ISO_DIR', PROJECT)
        self.media = Path(media or default_media).resolve()
        self.image = image or os.environ.get('WINDOWS_MANAGER_IMAGE', 'windows-console-mcp:local')
        self.scope = hashlib.sha256(os.fsencode(self.state)).hexdigest()[:12]
        self.label = f'windows-console.manager={self.scope}'
        self.file = self.state / 'inventory.json'
        self.legacy_state = legacy_state

    def read(self):
        try:
            return json.loads(self.file.read_text())
        except FileNotFoundError:
            return {key: {} for key in ('templates', 'vms', 'topologies')}

    def save(self, inventory):
        with tempfile.NamedTemporaryFile('w', dir=self.state, delete=False) as file:
            path = Path(file.name)
            try:
                json.dump(inventory, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
                os.replace(path, self.file)
            finally:
                path.unlink(missing_ok=True)

    @contextmanager
    def edit(self):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.state / 'inventory.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Conflict('別のVM管理操作が進行中です') from exc
            yield self.read()

    def images(self):
        return [{'id': p.name, 'bytes': p.stat().st_size}
                for p in sorted(self.media.glob('*')) if p.suffix.lower() == '.iso'
                and not p.is_symlink() and p.is_file() and os.access(p, os.R_OK)]

    def iso(self, image_id):
        if not isinstance(image_id, str) or not any(image_id == p['id'] for p in self.images()):
            raise ValueError('一覧にあるインストールISOを指定してください')
        return self.media / image_id

    def name(self, kind, ident):
        return f'windows-console-{self.scope}-{kind}-{ident}'

    def vm(self, vm_id):
        try:
            vm = self.read()['vms'][vm_id]
        except KeyError as exc:
            raise ValueError('Use a VM id from vm_inventory') from exc
        if vm.get('deleting'):
            raise Conflict('VMの削除が進行中です')
        return vm

    def transfer_network(self, vm):
        topology_id = vm.get('topology_id')
        segments = (self.read()['topologies'][topology_id]['segments'] if topology_id else ())
        networks = [ipaddress.IPv4Network(segment) for segment in segments]
        # At most eight /16 or smaller guest segments are allowed. Each candidate
        # is in a different /16, leaving at least one available transfer network.
        for second_octet in range(77, 86):
            net = ipaddress.IPv4Network(f'10.{second_octet}.0.0/24')
            if not any(net.overlaps(segment) for segment in networks):
                return str(net), str(net[4])

    def internet_status(self, vm_id):
        vm = self.vm(vm_id)
        status = self.statuses().get(self.vm_container(vm), 'stopped')
        topology = self.read()['topologies'].get(vm.get('topology_id'))
        if topology:
            enabled = topology['proxy']
            return {'container': status, 'current_enabled': enabled if status == 'running' else None,
                    'next_boot_enabled': enabled, 'restart_required': False,
                    'topology_id': topology['id']}
        enabled = vm.get('internet_enabled', False)
        current = None
        if status == 'running':
            raw = self.docker('container', 'inspect', '--format', '{{json .Config.Env}}', self.vm_container(vm))
            current = 'WINDOWS_INTERNET=1' in json.loads(raw)
        return {'container': status, 'current_enabled': current, 'next_boot_enabled': enabled,
                'restart_required': status == 'running' and current != enabled}

    def set_internet(self, vm_id, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        with self.edit() as inv:
            vm = inv['vms'][vm_id]
            if vm.get('topology_id') or vm.get('legacy_container') or vm.get('deleting'):
                raise Conflict('Use vm_topology_save for a network topology or the imported VM NAT controls')
            vm['internet_enabled'] = enabled
            self.save(inv)
        return {'vm_id': vm_id, 'internet_enabled': enabled}

    def vm_dir(self, vm_id):
        if not isinstance(vm_id, str) or not re.fullmatch(VM_ID, vm_id):
            raise ValueError('無効なVM IDです')
        return self.state / 'v' / vm_id

    def docker(self, *args, timeout=30):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or 'Docker操作に失敗しました')
        if args[:2] == ('container', 'logs'):
            return (result.stdout + result.stderr).strip()
        return result.stdout.strip()

    def adopt_legacy(self, name):
        with self.edit() as inv, legacy_lock(self.legacy_state):
            vm = legacy_vm.describe(self, name)
            imported = next((item for item in inv['vms'].values() if item.get('legacy_container')), None)
            if imported is not None and imported['legacy_container'] != vm['legacy_container']:
                raise Conflict('既存コンソールのVMは登録済みです')
            inv['vms'][vm['id']] = vm
            self.save(inv)
            return vm

    def vm_container(self, vm):
        return vm.get('legacy_container') or self.name('vm', vm['id'])

    def statuses(self):
        output = self.docker('container', 'ls', '--all', '--filter', 'label=' + self.label,
                             '--format', '{{.Names}} {{.State}}')
        statuses = dict(line.split() for line in output.splitlines())
        for vm in self.read()['vms'].values():
            if container := vm.get('legacy_container'):
                legacy = self.docker('container', 'ls', '--all', '--no-trunc', '--filter', 'id=' + container,
                                     '--format', '{{.ID}} {{.State}}')
                statuses.update(line.split() for line in legacy.splitlines())
        return statuses

    def overview(self):
        inventory = self.read()
        error = None
        try:
            statuses = self.statuses()
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            statuses, error = {}, str(exc)
        def container_status(kind, ident):
            return 'unknown' if error else statuses.get(self.name(kind, ident), 'stopped')

        vms = []
        for vm in inventory['vms'].values():
            status = 'unknown' if error else statuses.get(self.vm_container(vm), 'stopped')
            item = dict(vm, status='deleting' if vm.get('deleting') else status)
            if vm.get('legacy_container'):
                item['legacy_internet'] = (self.legacy_state / 'internet-enabled').is_file()
                selected = self.legacy_state / 'selected-install.iso'
                if selected.is_symlink():
                    item['image_id'] = selected.resolve().name
            vms.append(item)
        topologies = []
        for topology_id, topology in inventory['topologies'].items():
            topologies.append(dict(topology, router_status=container_status('router', topology_id),
                                   proxy_status=container_status('proxy', topology_id) if topology['proxy'] else None))
        return {'templates': list(inventory['templates'].values()), 'vms': vms,
                'topologies': topologies, 'images': self.images(),
                'docker_error': error}

    def save_template(self, name, image_id, vcpus, ram_mib, disk_gib, template_id=None):
        self.iso(image_id)
        values = dict(name=named(name), image_id=image_id, vcpus=number(vcpus, 1, 64, 'vCPU'),
                      ram_mib=number(ram_mib, 2048, 262144, 'RAM (MiB)'),
                      disk_gib=number(disk_gib, 32, 4096, 'ディスク (GiB)'))
        with self.edit() as inv:
            if template_id is not None:
                inv['templates'][template_id]
            ident = template_id or uuid.uuid4().hex[:12]
            template = inv['templates'][ident] = dict(id=ident, **values)
            self.save(inv)
            return template

    def delete_template(self, template_id):
        with self.edit() as inv:
            del inv['templates'][template_id]
            self.save(inv)
        return {'deleted': template_id}

    def attachment(self, inv, topology_id, segment):
        if topology_id is None:
            if segment is not None:
                raise ValueError('先にトポロジを指定してください')
        else:
            topology = inv['topologies'][topology_id]
            if segment not in topology['segments']:
                raise ValueError('トポロジ内のセグメントを指定してください')
        return dict(topology_id=topology_id, segment=segment)

    def create_vm(self, name, template_id, topology_id=None, segment=None, internet_enabled=False):
        if type(internet_enabled) is not bool or (internet_enabled and topology_id is not None):
            raise ValueError('個別NATはトポロジーに接続しないVMで設定してください')
        with self.edit() as inv:
            template = inv['templates'][template_id]
            self.iso(template['image_id'])
            ident = uuid.uuid4().hex[:12]
            size = {key: template[key] for key in ('image_id', 'vcpus', 'ram_mib', 'disk_gib')}
            vm = inv['vms'][ident] = dict(id=ident, name=named(name), template_id=template_id,
                                         template_name=template['name'], **size,
                                         **self.attachment(inv, topology_id, segment))
            if internet_enabled:
                vm['internet_enabled'] = True
            self.save(inv)
            return dict(vm, status='stopped')

    def set_vm_network(self, vm_id, topology_id=None, segment=None):
        with self.edit() as inv:
            vm = inv['vms'][vm_id]
            if vm.get('legacy_container'):
                raise Conflict('移行したVMは既存のネットワーク設定を使用しています')
            self.require_stopped(self.vm_container(vm), self.statuses())
            if vm.get('deleting'):
                raise Conflict('VMの削除が進行中です')
            attachment = self.attachment(inv, topology_id, segment)
            if any(vm[key] != value for key, value in attachment.items()):
                self.remove_tap(vm)
                vm.update(attachment)
                self.save(inv)
            return dict(vm, status='stopped')

    def save_topology(self, name, segments, proxy=False, topology_id=None):
        settings = topology_settings(segments, proxy)
        name = named(name)
        with self.edit() as inv:
            if topology_id is not None:
                previous = inv['topologies'][topology_id]
                if any(previous[key] != value for key, value in settings.items()):
                    members = [vm for vm in inv['vms'].values() if vm['topology_id'] == topology_id]
                    if any(vm['segment'] not in settings['segments'] for vm in members):
                        raise Conflict('使用中のセグメントから先にVMを移動してください')
                    statuses = self.statuses()
                    for vm in members:
                        self.require_stopped(self.vm_container(vm), statuses)
                    self.remove_topology(topology_id, statuses)
            ident = topology_id or uuid.uuid4().hex[:12]
            topology = inv['topologies'][ident] = dict(id=ident, name=name, **settings)
            self.save(inv)
            return topology

    def delete_topology(self, topology_id):
        with self.edit() as inv:
            inv['topologies'][topology_id]
            if any(vm['topology_id'] == topology_id for vm in inv['vms'].values()):
                raise Conflict('トポロジに接続中のVMがあります')
            self.remove_topology(topology_id, self.statuses())
            del inv['topologies'][topology_id]
            self.save(inv)
        return {'deleted': topology_id}

    def remove_topology(self, ident, statuses):
        for kind in ('proxy', 'router'):
            name = self.name(kind, ident)
            if name in statuses:
                self.docker('container', 'rm', '-f', name)

    def require_stopped(self, name, statuses):
        if statuses.get(name) not in STOPPED:
            raise Conflict('先にVMを停止してください')

    def wait_container(self, name, ready, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.statuses().get(name) != 'running':
                break
            if ready():
                return
            time.sleep(.3)
        logs = self.docker('container', 'logs', '--tail', '15', name)
        raise RuntimeError(f'{name} の起動を確認できませんでした\n{logs}')

    def ensure_router(self, topology, statuses, members):
        ident = topology['id']
        router = self.name('router', ident)
        proxy = self.name('proxy', ident)
        containers = [self.vm_container(vm) for vm in members]
        inactive = all(statuses.get(name) in STOPPED for name in containers)
        replace = statuses.get(router) != 'running'
        if not replace and inactive:
            desired = self.docker('image', 'inspect', '--format', '{{.Id}}', self.image)
            replace = any(
                self.docker('container', 'inspect', '--format', '{{.Image}}', name) != desired
                for name in (router, proxy) if statuses.get(name) == 'running'
            )
        if replace:
            for name in containers:
                self.require_stopped(name, statuses)
            for name in containers:
                if name in statuses:
                    self.docker('container', 'rm', name)
            self.remove_topology(ident, statuses)
            self.docker('run', '-d', '--name', router, '--label', self.label,
                        '--network', 'bridge' if topology['proxy'] else 'none',
                        '--sysctl', 'net.ipv4.ip_forward=1',
                        '--sysctl', 'net.ipv6.conf.all.disable_ipv6=1',
                        '--sysctl', 'net.ipv6.conf.default.disable_ipv6=1',
                        '--cap-add', 'NET_ADMIN', '--device', '/dev/net/tun',
                        '--security-opt', 'no-new-privileges',
                        '-e', 'WINDOWS_TOPOLOGY=' + json.dumps(topology), self.image,
                        'python3', '/opt/windows-network/router.py')
            statuses = self.statuses()
        self.wait_container(router, lambda: self.docker('exec', router, 'cat',
                            '/proc/1/comm') == 'dnsmasq', 15)
        if topology['proxy']:
            if statuses.get(proxy) != 'running':
                if proxy in statuses:
                    self.docker('container', 'rm', proxy)
                self.docker('run', '-d', '--name', proxy, '--label', self.label,
                            '--network', 'container:' + router, '--cap-drop', 'ALL',
                            '--security-opt', 'no-new-privileges', '--user', '65534:65534',
                            self.image, 'python3', '/opt/windows-network/proxy.py')
            probe = "import socket; socket.create_connection(('127.0.0.1',15001),1).close()"

            def proxy_ready():
                try:
                    self.docker('exec', proxy, 'python3', '-c', probe)
                except RuntimeError:
                    return False
                return True

            self.wait_container(proxy, proxy_ready, 10)
        return router

    def remove_tap(self, vm):
        if vm['topology_id'] is None:
            return
        router = self.name('router', vm['topology_id'])
        if self.statuses().get(router) == 'running':
            tap = 'vm' + vm['id']
            self.docker('exec', router, 'sh', '-ec',
                        'if ip link show "$1" >/dev/null 2>&1; then ip tuntap del dev "$1" mode tap; fi',
                        'sh', tap)

    def monitor_command(self, vm_id, op):
        if self.read()['vms'].get(vm_id, {}).get('legacy_container'):
            return legacy_vm.monitor(self.legacy_state, op)

        async def call():
            monitor = Monitor(path=self.vm_dir(vm_id) / 'manage.sock')
            try:
                return await monitor.command(op)
            finally:
                monitor.close()

        return asyncio.run(call())

    def start_vm(self, vm_id):
        with self.edit() as inv:
            vm = inv['vms'][vm_id]
            if vm.get('deleting'):
                raise Conflict('VMの削除が進行中です')
            name = self.vm_container(vm)

            def qmp_ready():
                try:
                    return self.monitor_command(vm_id, 'query-status')['running']
                except (OSError, TimeoutError, ValueError):
                    return False

            if vm.get('legacy_container'):
                with legacy_lock(self.legacy_state):
                    legacy_status = self.statuses().get(name)
                    if legacy_status in ('created', 'exited'):
                        build_setup_iso(self.legacy_state)
                        self.docker('container', 'start', name)
                    elif legacy_status != 'running':
                        raise Conflict('既存のVMコンテナを起動できません')
                    self.wait_container(name, qmp_ready, 20)
                return dict(vm, status='running')

            state = self.vm_dir(vm_id)
            statuses = self.statuses()
            if statuses.get(name) == 'running':
                self.wait_container(name, qmp_ready, 20)
                return dict(vm, status='running')
            self.require_stopped(name, statuses)
            if name in statuses:
                self.docker('container', 'rm', name)
                statuses.pop(name)
            if len(os.fsencode(state / 'spice/console.sock')) > 107:
                raise ValueError('VMの状態ディレクトリのパスがUnixソケットには長すぎます')
            iso = self.iso(vm['image_id'])
            state.mkdir(mode=0o700, parents=True, exist_ok=True)
            build_setup_iso(state)
            install_relay(state)
            transfer_net, transfer_host = self.transfer_network(vm)
            nat = vm.get('internet_enabled', False) and not vm.get('topology_id')
            password = state / 'vnc-password'
            if not password.exists():
                with os.fdopen(
                        os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as file:
                    file.write(secrets.token_urlsafe(6))
            network = ['--network', 'bridge' if nat else 'none']
            if vm['topology_id'] is not None:
                topology = inv['topologies'][vm['topology_id']]
                members = [item for item in inv['vms'].values() if item['topology_id'] == topology['id']]
                router = self.ensure_router(topology, statuses, members)
                tap = 'vm' + vm_id
                bridge = f"lan{topology['segments'].index(vm['segment'])}"
                self.docker('exec', router, 'sh', '-ec',
                            'ip link show "$1" >/dev/null 2>&1 || ip tuntap add dev "$1" mode tap user "$2"; '
                            'ip link set "$1" master "$3"; ip link set "$1" up',
                            'sh', tap, str(os.getuid()), bridge)
                mac = '02:' + ':'.join(vm_id[i:i+2] for i in range(0, 10, 2))
                network = ['--network', 'container:' + router, '--device', '/dev/net/tun',
                           '-e', f'WINDOWS_TAP={tap}', '-e', f'WINDOWS_MAC={mac}']

            self.docker('run', '-d', '--name', name, '--label', self.label, *network,
                        '--user', f'{os.getuid()}:{os.getgid()}',
                        '--group-add', str(Path('/dev/kvm').stat().st_gid),
                        '--device', '/dev/kvm', '--cap-drop', 'ALL',
                        '--security-opt', 'no-new-privileges',
                        '-e', 'WINDOWS_MANAGED=1', '-e', 'WINDOWS_SPICE=1',
                        '-e', f'WINDOWS_TRANSFER_NET={transfer_net}', '-e', f'WINDOWS_TRANSFER_HOST={transfer_host}',
                        '-e', 'WINDOWS_INTERNET=' + str(int(nat)),
                        '-e', f"WINDOWS_VCPUS={vm['vcpus']}", '-e', f"WINDOWS_RAM_MIB={vm['ram_mib']}",
                        '-e', f"WINDOWS_DISK_GIB={vm['disk_gib']}",
                        '--mount', bind_mount(state, '/vm-clean'),
                        '--mount', bind_mount(iso, '/media/windows11.iso', True),
                        '--mount', bind_mount(PROJECT / 'run-vm-clean.sh', '/run-vm-clean.sh', True),
                        self.image, '/bin/bash', '/run-vm-clean.sh')

            self.wait_container(name, qmp_ready, 20)
            return dict(vm, status='running')

    def stop_vm(self, vm_id, force=False, timeout_seconds=60):
        timeout_seconds = number(timeout_seconds, 5, 180, '停止タイムアウト (秒)')
        if type(force) is not bool:
            raise ValueError('forceには真偽値を指定してください')
        with self.edit() as inv:
            vm = inv['vms'][vm_id]
            lock = legacy_lock(self.legacy_state) if vm.get('legacy_container') else nullcontext()
            with lock:
                name = self.vm_container(vm)
                status = self.statuses().get(name)
                if status == 'running' and not force:
                    self.monitor_command(vm_id, 'system_powerdown')
                elif force and status in ('running', 'restarting', 'paused'):
                    if status == 'paused':
                        self.docker('unpause', name)
                    try:
                        self.docker('kill', name)
                    except RuntimeError:
                        self.require_stopped(name, self.statuses())
                else:
                    self.require_stopped(name, {name: status})
                statuses = self.wait_stopped(name, timeout_seconds)
                if not vm.get('legacy_container') and name in statuses:
                    self.docker('container', 'rm', name)
            return dict(vm, status='stopped')

    def wait_stopped(self, name, timeout_seconds):
        deadline = time.monotonic() + timeout_seconds
        while True:
            statuses = self.statuses()
            if statuses.get(name) in STOPPED:
                return statuses
            if time.monotonic() >= deadline:
                raise Conflict('Windowsの終了を確認できません。必要なら強制停止して削除できます')
            time.sleep(.25)

    def delete_vm(self, vm_id, force=False):
        if type(force) is not bool:
            raise ValueError('forceには真偽値を指定してください')
        with self.edit() as inv:
            vm = inv['vms'][vm_id]
            name = self.vm_container(vm)
            legacy = vm.get('legacy_container')
            lock = legacy_lock(self.legacy_state) if legacy else nullcontext()
            with lock:
                statuses = self.statuses()
                if not force:
                    self.require_stopped(name, statuses)
                vm['deleting'] = True
                self.save(inv)
                if name in statuses:
                    options = ('--force',) if force else ()
                    try:
                        self.docker('container', 'rm', *options, name)
                    except RuntimeError:
                        # Docker may have finished an overlapping removal.
                        if name in self.statuses():
                            raise
                if legacy:
                    legacy_vm.delete_guest_state(self.legacy_state)
                else:
                    self.remove_tap(vm)
                    path = self.vm_dir(vm_id)
                    if path.exists():
                        shutil.rmtree(path)
                del inv['vms'][vm_id]
                self.save(inv)
        return {'deleted': vm_id}
