"""Register the original console VM without moving or rewriting its guest state."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

import aiohttp

from control_config import CONTROL, STATE

LEGACY_VM = os.environ.get('WINDOWS_CONSOLE_VM', 'windows11-vm-clean')


def describe(manager, container_name):
    container, = json.loads(manager.docker('container', 'inspect', container_name))
    if container['Name'] != '/' + container_name or not re.fullmatch('[0-9a-f]{64}', container['Id']):
        raise ValueError('指定のVMコンテナを確認できません')
    mounts = {mount['Destination']: mount for mount in container['Mounts']}
    state = mounts.get('/vm-clean', {})
    if state.get('Type') != 'bind' or Path(state['Source']).resolve() != manager.legacy_state.resolve():
        raise ValueError('既存コンソールの状態ディレクトリを使用しているVMを指定してください')
    media = mounts.get('/media/windows11.iso', {})
    if media.get('Type') != 'bind' or not Path(media['Source']).is_file():
        raise ValueError('既存VMのインストールISOを確認できません')

    # The qcow2 header records the virtual capacity, independent of host allocation.
    with (manager.legacy_state / 'disk.qcow2').open('rb') as disk:
        header = disk.read(32)
    if len(header) != 32 or header[:4] != b'QFI\xfb':
        raise ValueError('既存VMの仮想ディスクを確認できません')
    size = int.from_bytes(header[24:32], 'big')
    config = dict(entry.split('=', 1) for entry in container['Config']['Env'])
    if config.get('WINDOWS_SPICE') != '1':
        raise ValueError('SPICE付きの既存コンソールVMを指定してください')
    container_id = container['Id']
    ident = hashlib.sha256(('legacy:' + container_id).encode()).hexdigest()[:12]
    return {
        'id': ident, 'name': container_name, 'legacy_container': container_id,
        'image_id': Path(media['Source']).name, 'vcpus': int(config.get('WINDOWS_VCPUS', '4')),
        'ram_mib': int(config.get('WINDOWS_RAM_MIB', '8192')), 'disk_gib': (size + 2**30 - 1) // 2**30,
        'topology_id': None, 'segment': None,
    }


def monitor(state, command):
    endpoint = {'query-status': 'status', 'system_powerdown': 'powerdown'}[command]

    async def call():
        connector = aiohttp.UnixConnector(path=str(CONTROL if CONTROL.exists() else state / 'console-admin.sock'))
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=8)) as client:
            async with client.post('http://localhost/control/' + endpoint) as response:
                if not response.ok:
                    raise OSError(await response.text())
                return await response.json()

    return asyncio.run(call())


def delete_guest_state(state):
    for name in ('disk.qcow2', 'OVMF_VARS.fd', 'internet-enabled', 'selected-install.iso',
                 'qmp.sock', 'tpm.sock', 'console-text.sock', 'vnc-password'):
        (state / name).unlink(missing_ok=True)
    for name in ('tpm', 'spice'):
        path = state / name
        if path.is_dir():
            shutil.rmtree(path)


def main():
    from vm_manager import VMManager

    if len(sys.argv) > 2:
        raise SystemExit('Usage: python console/legacy_vm.py [container-name]')
    manager = VMManager(legacy_state=STATE)
    print(json.dumps(manager.adopt_legacy(sys.argv[1] if len(sys.argv) == 2 else LEGACY_VM), ensure_ascii=False))


if __name__ == '__main__':
    main()
