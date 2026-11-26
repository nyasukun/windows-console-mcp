"""Read-only RFB framebuffer updates from the host-local QEMU display."""
import asyncio
import io
import os
import struct
from contextlib import asynccontextmanager

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from PIL import Image

from control_config import STATE


def surface(width, height):
    if not (width and height and width * height <= 64 * 1024 * 1024):
        raise ValueError('Invalid framebuffer dimensions')
    return Image.new('RGB', (width, height))


@asynccontextmanager
async def display_connection(state=None):
    if state is None:
        reader, writer = await asyncio.open_connection(
            '127.0.0.1', int(os.environ.get('WINDOWS_CONSOLE_RFB_PORT', '5912')))
    else:
        reader, writer = await asyncio.open_unix_connection(state / 'vnc.sock')
    read = reader.readexactly
    try:
        async with asyncio.timeout(7):
            if await read(12) != b'RFB 003.008\n':
                raise ValueError('Expected the local QEMU RFB 3.8 display')
            writer.write(b'RFB 003.008\n')
            security = await read((await read(1))[0])
            if 2 not in security:
                raise ValueError('QEMU VNC authentication unavailable')
            writer.write(b'\x02')
            challenge = await read(16)
            password = ((state or STATE) / 'vnc-password').read_bytes()[:8].ljust(8, b'\0')
            key = bytes(int(f'{n:08b}'[::-1], 2) for n in password)
            enc = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
            writer.write(enc.update(challenge) + enc.finalize())
            if int.from_bytes(await read(4), 'big'):
                raise ValueError('Local QEMU display authentication failed')
            writer.write(b'\x01')  # Shared view; other viewers stay connected.
            width, height = struct.unpack('>HH', await read(4))
            await read(16)
            name_size = int.from_bytes(await read(4), 'big')
            if name_size > 65536:
                raise ValueError('Invalid display name length')
            await read(name_size)
            pixel_format = struct.pack('>BBBBHHHBBBxxx', 32,24,0,1,255,255,255,16,8,0)
            writer.write(bytes(4) + pixel_format)
            writer.write(struct.pack('>BBHii', 2,0,2,0,-223))  # Raw, DesktopSize
            await writer.drain()
        yield reader, writer, surface(width, height)
    finally:
        writer.close()
        await writer.wait_closed()


async def frames(fps, state=None):
    async with display_connection(state) as (reader, writer, canvas):
        read = reader.readexactly
        incremental = False
        while True:
            writer.write(struct.pack('>BBHHHH', 3, int(incremental), 0,0,*canvas.size))
            await writer.drain()
            while True:
                kind = (await read(1))[0]
                if kind == 0:
                    break
                if kind == 2:  # Bell
                    continue
                if kind == 3:  # ServerCutText is unrelated to the framebuffer.
                    remaining = int.from_bytes((await read(7))[3:], 'big')
                    while remaining:
                        block = min(remaining, 65536)
                        await read(block)
                        remaining -= block
                    continue
                raise ValueError(f'Unsupported display message {kind}')
            count = int.from_bytes((await read(3))[1:], 'big')
            incremental = True
            for _ in range(count):
                x,y,w,h,encoding = struct.unpack('>HHHHi', await read(12))
                if encoding == -223:
                    canvas = surface(w,h)
                    incremental = False
                elif encoding == 0 and x+w <= canvas.width and y+h <= canvas.height:
                    if w and h:
                        pixels = await read(w*h*4)
                        canvas.paste(Image.frombytes('RGB',(w,h),pixels,'raw','BGRX'), (x,y))
                else:
                    raise ValueError('Unsupported framebuffer rectangle')
            if count and incremental:
                yield canvas
            await asyncio.sleep(1/fps)


def fit(canvas, max_width):
    w,h = canvas.size
    if w > max_width:
        canvas = canvas.resize((max_width, max(1, round(h*max_width/w))), Image.Resampling.BILINEAR)
    return canvas


def jpeg(canvas, max_width):
    canvas = fit(canvas, max_width)
    out = io.BytesIO()
    canvas.save(out, format='JPEG', quality=78)
    return out.getvalue(), canvas.size
