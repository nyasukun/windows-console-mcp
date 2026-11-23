import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import transfers
from transfer_http import GuestServer, guest_file


class Transfers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = patch.object(transfers, 'STORE', Path(temp.name) / 'store')
        store.start()
        self.addCleanup(store.stop)
        app = web.Application(client_max_size=0)
        app.router.add_get('/f/{token}', guest_file)
        app.router.add_post('/f/{token}', guest_file)
        self.client = TestClient(TestServer(app), headers={'Host':transfers.GUEST_HOST})
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_can_prepare_guest_endpoint_before_vm_boot(self):
        with tempfile.TemporaryDirectory() as temp, patch('transfer_http.STATE', Path(temp)):
            server = GuestServer()
            try:
                await server.ensure(None)
                await server.ensure(None)
                self.assertTrue((Path(temp) / 'transfer-http.sock').is_socket())
                self.assertEqual((Path(temp) / 'transfer-http.sock').stat().st_mode & 0o777, 0o600)
                self.assertTrue((Path(temp) / 'transfer-relay.py').is_file())
            finally:
                await server.close()

    async def test_sparse_download_beyond_four_gib(self):
        size = 2**32 + 17
        token = transfers.create('to_vm', '大容量.bin', size)
        url = '/f/' + token
        self.assertEqual((await self.client.get(url)).status, 404)
        partial = transfers.directory(token) / 'partial'
        with partial.open('r+b') as file:
            file.truncate(size - 1)
        with self.assertRaises(ValueError):
            transfers.publish(token)
        for _ in range(2):
            self.assertEqual(transfers.write_chunk(token, size-1, 'eA==')['next_offset'], size)
        with self.assertRaises(ValueError):
            transfers.write_chunk(token, size-1, 'eQ==')
        transfers.publish(token)
        response = await self.client.get(url, headers={'Range':f'bytes={size-2}-'})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b'\x00x')
        self.assertTrue(response.headers['Content-Range'].endswith('/' + str(size)))
        self.assertIn('filename*=', response.headers['Content-Disposition'])
        transfers.discard(token)
        self.assertEqual((await self.client.get(url)).status, 404)

    async def test_upload_is_atomic_and_single_use(self):
        token = transfers.create('from_vm')
        url = '/f/' + token
        self.assertIn('type="file"', await (await self.client.get(url)).text())
        bad = FormData()
        bad.add_field('file', b'first', filename='a.bin')
        bad.add_field('file', b'second', filename='b.bin')
        self.assertEqual((await self.client.post(url, data=bad)).status, 400)
        self.assertFalse(transfers.describe(token)['ready'])
        self.assertEqual(transfers.describe(token)['bytes'], 0)
        with self.assertRaises(ValueError):
            transfers.read_chunk(token, 0, 2)
        upload = FormData(quote_fields=False)
        upload.add_field('file', b'\x00\xff' * 700000, filename='転送.bin')
        self.assertEqual((await self.client.post(url, data=upload)).status, 200)
        state = transfers.describe(token)
        self.assertEqual((state['name'], state['size']), ('転送.bin', 1400000))
        last = transfers.read_chunk(token, 1399998, 64)
        self.assertEqual(base64.b64decode(last['data_base64']), b'\x00\xff')
        self.assertTrue(last['eof'])
        self.assertEqual((await self.client.post(url)).status, 409)

    async def test_empty_files_and_id_validation(self):
        token = transfers.create('to_vm', 'empty.txt', 0)
        transfers.publish(token)
        resp = await self.client.get('/f/' + token)
        self.assertEqual((resp.status, await resp.read()), (200, b''))
        back = transfers.create('from_vm')
        form = FormData()
        form.add_field('file', b'', filename='empty.txt')
        self.assertEqual((await self.client.post('/f/' + back, data=form)).status, 200)
        self.assertEqual(transfers.read_chunk(back, 0, 1)['next_offset'], 0)
        with self.assertRaises(ValueError):
            transfers.discard('../elsewhere')
        self.assertEqual((await self.client.get('/f/' + token, headers={'Host':'localhost'})).status, 404)


if __name__ == '__main__':
    unittest.main()
