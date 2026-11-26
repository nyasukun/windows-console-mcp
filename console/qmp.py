"""Serialize commands on the VM’s host-local QMP socket."""
import asyncio
import json

from control_config import STATE


class SocketReplaced(ConnectionError):
    pass


def socket_identity(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino, info.st_ctime_ns


class Monitor:
    def __init__(self, demo=None, path=STATE / 'qmp.sock', identity=None):
        self.path = path
        self.identity = identity
        self.demo = demo
        self.lock = asyncio.Lock()
        self.writer = None
        self.idle_timer = None

    def close(self):
        if self.idle_timer is not None:
            self.idle_timer.cancel()
            self.idle_timer = None
        if self.writer is not None:
            self.writer.close()
            self.writer = None

    def check_socket(self):
        if self.identity is not None and socket_identity(self.path) != self.identity:
            raise SocketReplaced('VMの接続先が変わりました。再接続してください')

    async def command(self, name, args=None):
        async with self.lock:
            if self.idle_timer is not None:
                self.idle_timer.cancel()
            try:
                self.check_socket()
                if self.writer is None or self.reader.at_eof():
                    self.close()
                    await asyncio.wait_for(self.open(), 4)
                result = await self.exchange(name, args)
                if self.demo and name == 'input-send-event':
                    axes = {e['data']['axis']: e['data']['value'] for e in args['events'] if e['type'] == 'abs'}
                    if 'x' in axes and 'y' in axes:
                        self.demo.move(axes['x'], axes['y'])
                # Release the single QMP client slot when the console is idle.
                self.idle_timer = asyncio.get_running_loop().call_later(2, self.close)
                return result
            except BaseException:
                self.close()
                raise

    async def open(self):
        self.reader, self.writer = await asyncio.open_unix_connection(self.path)
        self.check_socket()
        await asyncio.wait_for(self.reader.readline(), 3)
        await self.exchange('qmp_capabilities')

    async def exchange(self, name, args=None):
        self.writer.write(json.dumps({'execute': name, 'arguments': args or {}}).encode() + b'\n')
        await self.writer.drain()
        async with asyncio.timeout(4):
            while line := await self.reader.readline():
                answer = json.loads(line)
                if 'error' in answer:
                    raise ValueError(answer['error']['desc'])
                if 'return' in answer:
                    return answer['return']
        raise ConnectionError('QEMU disconnected')
