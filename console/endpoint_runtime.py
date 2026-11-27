"""Start and attach the Chrome and SSH containers."""
import ipaddress
import os
import shlex
import socket
import subprocess
from contextlib import suppress


def service_ready(path, kind):
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1)
            client.connect(str(path))
            if kind == 'browser':
                client.sendall(b'GET /json/version HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
                return b'200 OK' in client.recv(128)
            return client.recv(128).startswith(b'SSH-')
    except OSError:
        return False


def prepare_ssh(directory):
    directory.mkdir(mode=0o700, exist_ok=True)
    for key in ('server', 'client'):
        path = directory / key
        if not path.exists():
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)], check=True)
    (directory / 'sshd_config').write_text(
        'ListenAddress 127.0.0.1\nHostKey /endpoint/ssh/server\n'
        'AuthorizedKeysFile /endpoint/ssh/client.pub\nStrictModes no\n'
        'PermitRootLogin prohibit-password\nPasswordAuthentication no\n'
        'KbdInteractiveAuthentication no\nUsePAM yes\nDisableForwarding yes\n'
        'X11Forwarding no\nPrintMotd no\nPermitUserEnvironment no\n'
    )


def create_container(manager, item, directory):
    name = manager.endpoint_name(item['id'])
    kind = item['kind']
    uid, gid = os.getuid(), os.getgid()
    from vm_manager import bind_mount

    args = ['container', 'create', '--name', name, '--label', manager.label,
            '--network', 'none', '--init', '--security-opt', 'no-new-privileges',
            '--pids-limit', '512', '-e', f'ENDPOINT_UID={uid}', '-e', f'ENDPOINT_GID={gid}',
            '--mount', bind_mount(directory / 'socket', '/endpoint/socket'),
            '--mount', bind_mount(directory / 'resolv.conf', '/etc/resolv.conf', True)]
    if kind == 'browser':
        args += ['--user', f'{uid}:{gid}', '--cap-drop', 'ALL', '--memory', '2g',
                 '--mount', bind_mount(directory / 'home', '/endpoint/home')]
    else:
        prepare_ssh(directory / 'ssh')
        args += ['--cap-drop', 'NET_RAW',
                 '--mount', bind_mount(directory / 'home', '/root'),
                 '--mount', bind_mount(directory / 'ssh' / 'server', '/endpoint/ssh/server', True),
                 '--mount', bind_mount(directory / 'ssh' / 'client.pub', '/endpoint/ssh/client.pub', True),
                 '--mount', bind_mount(directory / 'ssh' / 'sshd_config', '/endpoint/ssh/sshd_config', True)]
    image = os.environ.get(f'WINDOWS_{kind.upper()}_IMAGE', f'windows-console-{kind}:local')
    manager.docker(*args, image)


def attach(manager, item, topology, router):
    guest = manager.endpoint_name(item['id'])
    pid = int(manager.docker('container', 'inspect', '--format', '{{.State.Pid}}', guest))
    if pid <= 0:
        raise RuntimeError('コンテナのネットワークを開けません')
    port = 'ep' + item['id']
    peer = 'eg' + item['id']
    segment = ipaddress.IPv4Network(item['segment'])
    bridge = 'lan' + str(topology['segments'].index(item['segment']))
    # These short-lived helpers hold NET_ADMIN only in the selected router/guest namespace.
    # The endpoint process itself receives neither a Docker socket nor network administration.
    helper = ['run', '--rm', '--cap-drop', 'ALL', '--cap-add', 'NET_ADMIN',
              '--security-opt', 'no-new-privileges']
    try:
        manager.docker(*helper, '--network', 'container:' + router, '--mount',
                       f'type=bind,source=/proc/{pid}/ns/net,target=/run/netns/endpoint,readonly',
                       manager.image, 'sh', '-ec',
                       'ip link add "$1" type veth peer name "$2"; '
                       'ip link set "$1" master "$3"; ip link set "$1" up; '
                       'ip link set "$2" netns endpoint', 'sh', port, peer, bridge)
        manager.docker(*helper, '--network', 'container:' + guest, manager.image, 'sh', '-ec',
                       'ip link set "$1" name eth0; ip addr add "$2" dev eth0; '
                       'ip link set eth0 up; ip route replace default via "$3"',
                       'sh', peer, f"{item['address']}/{segment.prefixlen}", str(segment[1]))
    except BaseException:
        with suppress(RuntimeError, OSError, subprocess.TimeoutExpired):
            manager.docker('exec', router, 'ip', 'link', 'delete', port)
        raise
