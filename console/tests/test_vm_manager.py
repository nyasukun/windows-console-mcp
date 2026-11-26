import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / 'mcp')]
import service
from management_http import install
from qmp import SocketReplaced
from server import ManagedConsoleSessions, local_only
from vm_manager import VMManager


class ManagedVMTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.media = Path(directory)
        (self.media / 'Windows, 日本語.iso').touch()
        self.manager = VMManager(state=self.media / 'state', media=self.media)
        self.statuses = self.enterContext(patch.object(self.manager, 'statuses', return_value={}))
        self.enterContext(patch.object(service, 'manager', self.manager))
        app = web.Application(middlewares=[local_only])
        install(app, self.manager)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = {'Origin': str(self.client.make_url('/').origin())}
        self.template = {'name': 'Windows', 'image_id': 'Windows, 日本語.iso',
                         'vcpus': 2, 'ram_mib': 4096, 'disk_gib': 64}

    async def test_web_and_mcp_share_inventory_and_vm_snapshots(self):
        response = await self.client.post('/api/templates', json=self.template, headers=self.origin)
        self.assertEqual(response.status, 200, await response.text())
        template = await response.json()
        self.assertEqual((await service.vm_inventory())['templates'], [template])
        vm = await service.vm_create('Endpoint', template['id'])
        response = await self.client.put('/api/templates/' + template['id'],
                                         json=dict(self.template, disk_gib=90), headers=self.origin)
        self.assertEqual(response.status, 200)
        await service.vm_template_delete(template['id'])
        info = await self.client.get('/api/management')
        self.assertEqual((await info.json())['vms'], [vm])
        self.assertEqual(vm['disk_gib'], 64)
        response = await self.client.delete('/api/vms/' + vm['id'], json={}, headers=self.origin)
        self.assertEqual(response.status, 200)
        self.assertEqual((await service.vm_inventory())['vms'], [])

    async def test_web_rejects_cross_origin_mutations_and_invalid_topologies(self):
        response = await self.client.post('/api/templates', json=self.template,
                                          headers={'Origin': 'https://example.org'})
        self.assertEqual(response.status, 403)
        response = await self.client.post('/api/topologies', headers=self.origin,
                                          json={'name': 'overlap', 'segments': ['10.40.0.0/24', '10.40.0.0/25']})
        self.assertEqual(response.status, 400)
        self.assertEqual((await service.vm_inventory())['templates'], [])

    async def test_running_vm_blocks_topology_changes_and_deletion(self):
        template = await service.vm_template_save(**self.template)
        network = await service.vm_topology_save('Office', ['10.40.0.0/24', '10.40.1.0/24'])
        vm = await service.vm_create('Endpoint', template['id'], network['id'], '10.40.1.0/24')
        self.statuses.return_value = {self.manager.name('vm', vm['id']): 'running'}
        response = await self.client.put('/api/topologies/' + network['id'], headers=self.origin,
                                         json={'name': 'Office', 'segments': network['segments'], 'proxy': True})
        self.assertEqual(response.status, 409)
        response = await self.client.delete('/api/vms/' + vm['id'], headers=self.origin, json={})
        self.assertEqual(response.status, 409)
        self.statuses.return_value = {}
        await service.vm_set_network(vm['id'])
        await service.vm_topology_delete(network['id'])
        info = await service.vm_inventory()
        self.assertEqual(info['topologies'], [])
        self.assertIsNone(info['vms'][0]['topology_id'])

    async def test_graceful_stop_requests_acpi_and_removes_exited_container(self):
        template = await service.vm_template_save(**self.template)
        vm = await service.vm_create('Endpoint', template['id'])
        name = self.manager.name('vm', vm['id'])
        self.statuses.side_effect = [{name: 'running'}, {name: 'exited'}, {name: 'exited'}]
        with (patch.object(self.manager, 'monitor_command') as monitor,
              patch.object(self.manager, 'docker') as docker):
            result = await service.vm_stop(vm['id'])
        self.assertEqual(result['status'], 'stopped')
        monitor.assert_called_once_with(vm['id'], 'system_powerdown')
        docker.assert_called_once_with('container', 'rm', name)

    async def test_force_delete_retries_container_removal_without_losing_disk(self):
        template = await service.vm_template_save(**self.template)
        vm = await service.vm_create('Unresponsive VM', template['id'])
        ident = vm['id']
        name = self.manager.name('vm', ident)
        disk = self.manager.vm_dir(ident) / 'disk.qcow2'
        disk.parent.mkdir(parents=True)
        disk.touch()
        self.statuses.return_value = {name: 'running'}
        url = '/api/vms/' + ident
        response = await self.client.delete(url, headers=self.origin, json={'force': 'true'})
        self.assertEqual(response.status, 400)
        response = await self.client.delete(url, headers=self.origin, json={})
        self.assertEqual(response.status, 409)
        with patch.object(self.manager, 'docker', side_effect=RuntimeError('busy')):
            response = await self.client.delete(url, headers=self.origin, json={'force': True})
            self.assertEqual(response.status, 503)
        self.assertTrue(disk.is_file())
        self.assertEqual((await service.vm_inventory())['vms'][0]['status'], 'deleting')
        with patch.object(self.manager, 'docker') as docker:
            self.assertEqual(await service.vm_delete(ident, force=True), {'deleted': ident})
            docker.assert_called_once_with('container', 'rm', '--force', name)
        self.assertFalse(disk.exists())
        self.assertFalse((await service.vm_inventory())['vms'])

    async def test_console_session_tracks_rebound_monitor_socket(self):
        template = await service.vm_template_save(**self.template)
        vm = await service.vm_create('Endpoint', template['id'])
        state = self.manager.vm_dir(vm['id'])
        state.mkdir(parents=True)
        socket = state / 'qmp.sock'
        registry = ManagedConsoleSessions(self.manager)

        async def listen(generation):
            async def reply(reader, writer):
                try:
                    writer.write(b'{"QMP":{}}\n')
                    while line := await reader.readline():
                        command = json.loads(line)['execute']
                        result = {} if command == 'qmp_capabilities' else {'generation': generation}
                        writer.write(json.dumps({'return': result}).encode() + b'\n')
                        await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()

            return await self.enterAsyncContext(await asyncio.start_unix_server(reply, path=socket))

        try:
            await listen(1)
            old = registry.get(vm['id'])
            self.assertEqual(await old['monitor'].command('query-status'), {'generation': 1})
            # Rename the listening socket while the first client's connection is still open.
            socket.rename(state / 'retired.sock')
            await listen(2)
            fresh = registry.get(vm['id'])
            self.assertIsNot(fresh, old)
            with self.assertRaises(SocketReplaced):
                await old['monitor'].command('query-status')
            self.assertEqual(await fresh['monitor'].command('query-status'), {'generation': 2})
        finally:
            registry.close()

    async def test_migration_keeps_existing_vm_data_container_and_power_state(self):
        legacy = self.media / 'legacy'
        legacy.mkdir()
        self.manager.legacy_state = legacy
        disk = legacy / 'disk.qcow2'
        header = b'QFI\xfb' + bytes(20) + (80 * 2**30).to_bytes(8, 'big')
        disk.write_bytes(header)
        (legacy / 'internet-enabled').touch()
        container_id = '9' * 64
        inspection = {
            'Name': '/existing-windows', 'Id': container_id,
            'Mounts': [
                {'Type': 'bind', 'Destination': '/vm-clean', 'Source': str(legacy)},
                {'Type': 'bind', 'Destination': '/media/windows11.iso',
                 'Source': str(self.media / self.template['image_id'])},
            ],
            'Config': {'Env': ['WINDOWS_SPICE=1', 'WINDOWS_VCPUS=6']},
        }
        with patch.object(self.manager, 'docker', return_value=json.dumps([inspection])) as docker:
            vm = self.manager.adopt_legacy('existing-windows')
            self.assertEqual(self.manager.adopt_legacy('existing-windows'), vm)
            self.assertEqual(vm['vcpus'], 6)
            self.assertEqual(vm['disk_gib'], 80)
            self.assertEqual(len(self.manager.read()['vms']), 1)
            self.assertEqual(disk.read_bytes(), header)
            docker.assert_called_with('container', 'inspect', 'existing-windows')

        self.statuses.return_value = {container_id: 'running'}
        snapshot = (await service.vm_inventory())['vms'][0]
        self.assertTrue(snapshot['legacy_internet'])
        self.assertEqual(snapshot['image_id'], self.template['image_id'])
        self.assertEqual(snapshot['status'], 'running')
        registry = ManagedConsoleSessions(self.manager)
        self.addCleanup(registry.close)
        self.assertIs(registry.get(vm['id']), registry.legacy())
        self.assertTrue(registry.get(vm['id'])['legacy_rfb'])

        ident = vm['id']
        self.statuses.side_effect = [{container_id: 'running'}, {container_id: 'exited'}, {container_id: 'exited'}]
        with (patch.object(self.manager, 'docker') as docker,
              patch('legacy_vm.monitor') as monitor):
            self.assertEqual((await service.vm_stop(ident))['status'], 'stopped')
            monitor.assert_called_once_with(legacy, 'system_powerdown')
            docker.assert_not_called()
        self.statuses.side_effect = None
        self.statuses.return_value = {container_id: 'exited'}
        with (patch.object(self.manager, 'docker') as docker,
              patch('vm_manager.build_setup_iso') as setup,
              patch.object(self.manager, 'wait_container')):
            self.assertEqual((await service.vm_start(ident))['status'], 'running')
            setup.assert_called_once_with(legacy)
            docker.assert_called_once_with('container', 'start', container_id)
        archived = legacy / 'transfers'
        archived.mkdir()
        (archived / 'retained').write_bytes(b'transfer')
        with patch.object(self.manager, 'docker') as docker:
            response = await self.client.delete('/api/vms/' + ident, headers=self.origin, json={})
            self.assertEqual(response.status, 200)
            docker.assert_called_once_with('container', 'rm', container_id)
        self.assertFalse(disk.exists())
        self.assertEqual((archived / 'retained').read_bytes(), b'transfer')
        self.assertEqual(self.manager.read()['vms'], {})
