"""Exercise the stdio frame subscription and duplex input with a synthetic desktop."""
import asyncio
import base64
import json
import sys
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from PIL import Image
from pydantic import AnyUrl

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'mcp')]
import live
from stream_input import batch_input
from walkthrough import Walkthrough


async def serve_fixture():
    updated = asyncio.Event()
    calls = []
    class Monitor:
        async def command(self, name, payload):
            calls.extend(payload['events'])
            updated.set()

    async def frames(_fps):
        while True:
            yield Image.new('RGB',(640,360),(len(calls)*35 % 255,0,0))
            await updated.wait()
            updated.clear()

    live.frames = frames
    app = web.Application()
    demo = Walkthrough()
    mcp = FastMCP('test-live')

    @mcp.tool()
    async def show_demo():
        demo.show(dict(text='スタートメニュー', x=8000, y=10000,
                       pointer_x=6000, pointer_y=8000, duration_seconds=1.5))

    app['shared'] = {'input_lock':asyncio.Lock(), 'held':False, 'monitor':Monitor()}
    app.router.add_get('/stream-input', batch_input)
    app.router.add_get('/walkthrough', demo.events)
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        path = str(Path(root)/'stream.sock')
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.UnixSite(runner,path).start()
            def connect():
                return aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=path))
            await live.install(mcp,connect)()
        finally:
            demo.clear()
            await runner.cleanup()


class StreamProtocol(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_input_and_latest_frame(self):
        updated = asyncio.Event()
        async def receive(msg):
            if isinstance(msg, types.ServerNotification) and isinstance(msg.root, types.ResourceUpdatedNotification):
                updated.set()
        params = StdioServerParameters(command=sys.executable, args=[__file__, '--serve'])
        async with stdio_client(params) as streams, ClientSession(*streams,message_handler=receive) as client:
            self.assertTrue((await client.initialize()).capabilities.resources.subscribe)
            async def call(name, **kwargs):
                r = await client.call_tool(name,kwargs)
                self.assertFalse(r.isError, r.content[0].text[:200])
                return r.content
            info = json.loads((await call('console_stream_open',max_width=320))[0].text)
            ident,uri = info['stream_id'],AnyUrl(info['frame_uri'])
            self.assertEqual(info['image_size'],[320,180])
            await client.subscribe_resource(uri)
            first = (await client.read_resource(uri)).contents[0]
            self.assertEqual(base64.b64decode(first.blob)[:2],b'\xff\xd8')
            pending = asyncio.create_task(call('console_stream_next',stream_id=ident,after=1))
            actions=[dict(op='key',key='shift',down=True)]
            for _ in range(2):
                ack=json.loads((await call('console_stream_input',stream_id=ident,sequence=1,events=actions))[0].text)
                self.assertEqual((ack['accepted'],ack['held']),(1,1))
            await asyncio.wait_for(updated.wait(),3)
            frame = await pending
            self.assertIsInstance(frame[1],types.ImageContent)
            second=(await client.read_resource(uri)).contents[0]
            self.assertNotEqual(first.blob,second.blob)
            ack=json.loads((await call('console_stream_input',stream_id=ident,sequence=2,
                                      events=[dict(op='key',key='shift',down=False)]))[0].text)
            self.assertEqual(ack['held'],0)
            await client.unsubscribe_resource(uri)
            await call('console_stream_close',stream_id=ident)

    async def test_demo_updates_independently_on_static_screen(self):
        notifications = asyncio.Queue()

        async def receive(msg):
            if isinstance(msg, types.ServerNotification) and isinstance(msg.root, types.ResourceUpdatedNotification):
                notifications.put_nowait(str(msg.root.params.uri))

        params = StdioServerParameters(command=sys.executable, args=[__file__, '--serve'])
        async with stdio_client(params) as streams, ClientSession(*streams, message_handler=receive) as client:
            await client.initialize()

            async def call(name, **kwargs):
                result = await client.call_tool(name, kwargs)
                self.assertFalse(result.isError, result.content)
                return result.content

            opened = json.loads((await call('console_stream_open', views='both', max_width=320))[0].text)
            ident = opened['stream_id']
            bare, demo = (AnyUrl(opened['views'][name]['frame_uri']) for name in ('bare', 'demo'))
            for uri in (bare, demo):
                await client.subscribe_resource(uri)
            pristine = (await client.read_resource(bare)).contents[0].blob
            self.assertEqual((await client.read_resource(demo)).contents[0].blob, pristine)
            await call('show_demo')
            self.assertEqual(await asyncio.wait_for(notifications.get(), 2), str(demo))
            annotated = (await client.read_resource(demo)).contents[0].blob
            self.assertNotEqual(annotated, pristine)
            self.assertEqual(await asyncio.wait_for(notifications.get(), 2), str(demo))
            countdown = (await client.read_resource(demo)).contents[0].blob
            self.assertNotIn(countdown, (annotated, pristine))

            seq = 1
            async with asyncio.timeout(4):
                while True:
                    meta, frame = await call('console_stream_next', stream_id=ident, view='demo', after=seq)
                    seq = json.loads(meta.text)['frame_sequence']
                    if frame.data == pristine:
                        break
            bare_result = await call('console_stream_next', stream_id=ident, view='bare', after=1, timeout_ms=0)
            self.assertFalse(json.loads(bare_result[0].text)['changed'])
            self.assertEqual((await client.read_resource(bare)).contents[0].blob, pristine)
            await call('console_stream_close', stream_id=ident)


if __name__ == '__main__':
    if '--serve' in sys.argv:
        asyncio.run(serve_fixture())
    else:
        unittest.main()
