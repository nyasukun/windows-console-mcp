"""Relay redirected IPv4 TCP connections to their original public destinations."""
import asyncio
import ipaddress
import socket
import struct

SO_ORIGINAL_DST = 80
LIMIT = 256


async def copy(source, sink):
    while data := await asyncio.wait_for(source.read(65536), 300):
        sink.write(data)
        await asyncio.wait_for(sink.drain(), 30)
    sink.write_eof()


async def relay(reader, writer):
    try:
        address = writer.get_extra_info('socket').getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
        port, packed = struct.unpack_from('!H4s', address, 2)
        ip = ipaddress.IPv4Address(packed)
        if not ip.is_global or ip.is_multicast:
            return
        upstream, remote = await asyncio.wait_for(asyncio.open_connection(str(ip), port), 10)
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(copy(reader, remote))
                tasks.create_task(copy(upstream, writer))
        finally:
            remote.close()
    except* (OSError, TimeoutError):
        pass
    finally:
        writer.close()


async def main():
    active = set()

    def connect(reader, writer):
        if len(active) >= LIMIT:
            writer.close()
            return
        task = asyncio.create_task(relay(reader, writer))
        active.add(task)
        task.add_done_callback(active.discard)

    async with await asyncio.start_server(connect, '0.0.0.0', 15001) as server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
