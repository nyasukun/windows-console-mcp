"""Exercise the browser/MCP arbitration and media rollback without a VM."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer

CONSOLE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CONSOLE), str(CONSOLE / 'mcp')]
import server
import service


class InputSharing(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.monitor = AsyncMock()
        shared = {'monitor': self.monitor, 'browser': asyncio.Lock(),
                  'input_lock': asyncio.Lock(), 'held':False}
        self.clients = []
        for admin in [False, True]:
            app = web.Application()
            app['shared'] = shared
            app['admin'] = admin
            app.router.add_get('/input', server.input_session)
            client = TestClient(TestServer(app))
            await client.start_server()
            self.addAsyncCleanup(client.close)
            self.clients.append(client)

    async def test_mcp_lease_and_release_with_browser_connected(self):
        browser_client, mcp_client = self.clients
        async with browser_client.ws_connect('/input') as browser:
            self.assertTrue((await browser.receive_json())['ready'])
            await browser.send_json(dict(op='key', key='shift', down=True, id='press'))
            self.assertEqual((await browser.receive_json())['id'], 'press')
            with self.assertRaises(WSServerHandshakeError) as refused:
                await mcp_client.ws_connect('/input')
            self.assertEqual(refused.exception.status, 409)
            await browser.send_json(dict(op='release', id='release'))
            await browser.receive_json()
            async with mcp_client.ws_connect('/input') as admin:
                self.assertTrue((await admin.receive_json())['ready'])
                await admin.send_json(dict(op='key', key='ctrl', down=True, id='test'))
                await admin.receive_json()
                await browser.send_json(dict(op='move', x=2, y=3, seq=1))
            self.assertEqual(await browser.receive_json(timeout=2), {'motion':1})
            batches = [c.args[1]['events'] for c in self.monitor.command.await_args_list
                       if c.args[0] == 'input-send-event']
            self.assertEqual(batches[-2], [server.key_event('ctrl', False)])
            self.assertEqual([event['data']['value'] for event in batches[-1]], [2, 3])


class MediaSelection(unittest.IsolatedAsyncioTestCase):
    async def test_failed_live_change_restores_previous_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            state = project / 'state-clean'
            state.mkdir()
            (project / 'test.iso').touch()
            choice = state / 'selected-install.iso'
            choice.symlink_to('original.iso')
            failure = AsyncMock(side_effect=RuntimeError('CD locked'))
            with patch.multiple(service, STATE=state, PROJECT=project, SELECTION=choice,
                                container_state=AsyncMock(return_value='running'), control=failure):
                with self.assertRaisesRegex(RuntimeError, 'CD locked'):
                    await service.select_image('workspace/test.iso')
                self.assertEqual(str(choice.readlink()), 'original.iso')
                self.assertNotIn('guest-tools/selected-install.iso', service.catalog())


if __name__ == '__main__':
    unittest.main()
