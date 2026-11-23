"""Exercise the walkthrough event feed and its QMP input hook without a VM."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Monitor
from walkthrough import Walkthrough


async def event(response):
    raw = await asyncio.wait_for(response.content.readuntil(b'\n\n'), 2)
    name, payload = raw.decode().strip().split('\n')
    return name.removeprefix('event: '), json.loads(payload.removeprefix('data: '))


class WalkthroughTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.demo = Walkthrough()
        self.addCleanup(self.demo.clear)
        self.step = dict(x=10, y=20, text='日本語', duration_seconds=5)
        app = web.Application()
        app.router.add_get('/walkthrough', self.demo.events)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_show_motion_rejoin_replace_and_clear(self):
        first = await self.client.get('/walkthrough')
        self.assertEqual(await event(first), ('step', None))
        step_id = self.demo.show(self.step)['step_id']
        _, shown = await event(first)
        self.assertEqual((shown['id'], shown['pointer'], shown['text']), (step_id, [10, 20], '日本語'))
        self.demo.move(30, 40)
        self.assertEqual(await event(first), ('pointer', [30, 40]))
        second = await self.client.get('/walkthrough')
        _, joined = await event(second)
        self.assertEqual((joined['id'], joined['pointer']), (step_id, [30, 40]))
        self.assertLess(joined['remaining_ms'], shown['remaining_ms'])
        self.demo.show(dict(self.step, text='次'))
        for viewer in (first, second):
            kind, replacement = await event(viewer)
            self.assertEqual(kind, 'step')
            self.assertGreater(replacement['id'], step_id)
            self.assertEqual(replacement['pointer'], [30, 40])
        self.demo.clear()
        for viewer in (first, second):
            self.assertEqual(await event(viewer), ('step', None))
            viewer.close()

    async def test_expiry_and_invalid_replacement(self):
        self.demo.show(dict(self.step, duration_seconds=0.1, pointer_x=0, pointer_y=32767))
        viewer = await self.client.get('/walkthrough')
        _, initial = await event(viewer)
        self.assertEqual(initial['pointer'], [0, 32767])
        for bad in (dict(pointer_x=1), dict(x=-1), dict(y=32768), dict(text=None),
                    dict(duration_seconds=0), dict(duration_seconds=float('nan')),
                    dict(duration_seconds=10**400)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.demo.show(dict(self.step, **bad))
        self.assertEqual(await event(viewer), ('step', None))
        viewer.close()
        rejoined = await self.client.get('/walkthrough')
        self.assertEqual(await event(rejoined), ('step', None))
        rejoined.close()

    async def test_only_acknowledged_absolute_motion_updates_pointer(self):
        monitor = Monitor(self.demo)
        monitor.writer = Mock()
        monitor.reader = asyncio.StreamReader()
        monitor.exchange = AsyncMock(return_value={})
        self.addCleanup(monitor.close)
        events = [{'type': 'abs', 'data': {'axis': axis, 'value': value}}
                  for axis, value in [('x', 123), ('y', 456)]]
        self.demo.show(self.step)
        await monitor.command('input-send-event', {'events': events})
        self.assertEqual(self.demo.pointer, (123, 456))
        await monitor.command('input-send-event', {'events': [{'type': 'btn', 'data': {}}]})
        monitor.exchange.side_effect = ValueError('rejected')
        events[0]['data']['value'] = 999
        with self.assertRaisesRegex(ValueError, 'rejected'):
            await monitor.command('input-send-event', {'events': events})
        self.assertEqual(self.demo.pointer, (123, 456))
