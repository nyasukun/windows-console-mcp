"""Forward a private Unix socket to the service in this container."""
import asyncio
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path


async def pipe(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


async def relay(reader, writer, port):
    writers = [writer]
    try:
        peer_reader, peer_writer = await asyncio.open_connection('127.0.0.1', port)
        writers.append(peer_writer)
        async with asyncio.TaskGroup() as group:
            group.create_task(pipe(reader, peer_writer))
            group.create_task(pipe(peer_reader, writer))
    except (OSError, ExceptionGroup):
        pass
    finally:
        for stream in writers:
            stream.close()
            with suppress(OSError):
                await stream.wait_closed()


async def main():
    port = int(sys.argv[1])
    path = Path('/endpoint/socket/service.sock')
    path.unlink(missing_ok=True)
    child = await asyncio.create_subprocess_exec(*sys.argv[2:])
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, lambda: child.terminate() if child.returncode is None else None)
    try:
        server = await asyncio.start_unix_server(lambda rd, wr: relay(rd, wr, port), path=path)
        os.chown(path, int(os.environ['ENDPOINT_UID']), int(os.environ['ENDPOINT_GID']))
        path.chmod(0o600)
        async with server:
            return await child.wait()
    finally:
        path.unlink(missing_ok=True)
        if child.returncode is None:
            child.terminate()
            await child.wait()


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
