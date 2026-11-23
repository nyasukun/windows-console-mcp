import asyncio
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'mcp')]
import live_screen


class IncrementalDisplay(unittest.IsolatedAsyncioTestCase):
    async def test_shared_display_resize_and_incremental_rectangle(self):
        calls = []
        async def peer(reader, writer):
            read = reader.readexactly
            try:
                writer.write(b'RFB 003.008\n')
                await read(12)
                writer.write(b'\x01\x02')
                await read(1)
                writer.write(bytes(16))
                await read(16)
                writer.write(bytes(4))
                calls.append(await read(1))  # shared flag
                writer.write(struct.pack('>HH',2,1)+bytes(20))
                await read(20+12)
                for info, pixels in [
                    ((0,0,2,1,0), b'\0\0\xff\0'*2),
                    ((0,0,3,1,-223), b''),
                    ((0,0,3,1,0), b'\0\xff\0\0'*3),
                    ((2,0,1,1,0), b'\xff\0\0\0')]:
                    request = await read(10)
                    calls.append((request[1],int.from_bytes(request[6:8], 'big')))
                    writer.write(b'\0\0\0\1' + struct.pack('>HHHHi',*info) + pixels)
                    await writer.drain()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(peer,'127.0.0.1',0) as server:
            connect = asyncio.open_connection
            port = server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp)/'vnc-password').write_bytes(b'fixture')
                with patch.object(live_screen, 'STATE', Path(tmp)), patch.object(
                        live_screen.asyncio, 'open_connection', lambda *_:connect('127.0.0.1',port)):
                    stream = live_screen.frames(30)
                    try:
                        self.assertEqual((await anext(stream)).getpixel((1,0)),(255,0,0))
                        self.assertEqual((await anext(stream)).size,(3,1))
                        image = await anext(stream)
                        self.assertEqual(image.getpixel((0,0)),(0,255,0))
                        self.assertEqual(image.getpixel((2,0)),(0,0,255))
                        self.assertEqual(calls,[b'\1',(0,2),(1,2),(0,3),(1,3)])
                    finally:
                        await stream.aclose()
