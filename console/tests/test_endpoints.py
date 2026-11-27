import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vm_manager import Conflict, VMManager


class EndpointInventory(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.manager = VMManager(state=self.root / 'state', media=self.root)
        self.docker = self.enterContext(patch.object(self.manager, 'docker', return_value=''))
        self.network = self.manager.save_topology('Office', ['10.93.1.0/28', '10.93.2.0/28'])

    def test_endpoints_allocate_unique_addresses_and_appear_in_inventory(self):
        ident = self.network['id']
        linux = self.manager.create_endpoint('App', 'linux', ident, '10.93.1.0/28')
        browser = self.manager.create_endpoint('Browser', 'browser', ident, '10.93.1.0/28')
        self.assertEqual(linux['address'], '10.93.1.14')
        self.assertEqual(browser['address'], '10.93.1.13')
        self.assertEqual([endpoint['id'] for endpoint in self.manager.overview()['endpoints']],
                         [linux['id'], browser['id']])
        with self.assertRaises(Conflict):
            self.manager.delete_topology(ident)
        moved = self.manager.set_endpoint_network(browser['id'], ident, '10.93.2.0/28')
        self.assertEqual(moved['address'], '10.93.2.14')

    def test_dns_accepts_private_segment_addresses_and_protects_named_endpoints(self):
        ident = self.network['id']
        linux = self.manager.create_endpoint('App', 'linux', ident, '10.93.1.0/28')
        record = {'name': 'App.Example.test', 'address': linux['address']}
        updated = self.manager.save_topology('Office', self.network['segments'], topology_id=ident,
                                             dns=[record])
        self.assertEqual(updated['dns'], [dict(record, name='app.example.test')])
        with self.assertRaisesRegex(Conflict, 'app.example.test'):
            self.manager.delete_endpoint(linux['id'])
        with self.assertRaises(ValueError):
            self.manager.save_topology('Office', self.network['segments'], topology_id=ident,
                                       dns=[dict(record, address='1.1.1.1')])

    def test_running_older_router_cannot_allocate_overlapping_container_addresses(self):
        tid = self.network['id']
        item = self.manager.create_endpoint('Chrome', 'browser', tid, self.network['segments'][0])
        vm = {'id': 'old-windows'}
        router = self.manager.name('router', tid)
        with patch.object(self.manager, 'wait_container'):
            with self.assertRaisesRegex(Conflict, '新しいルーター'):
                self.manager.ensure_router(self.network, {router: 'running',
                    self.manager.vm_container(vm): 'running'}, [item, vm])
