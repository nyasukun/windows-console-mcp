"""One timed walkthrough step, shared by MCP clients and browser viewers."""
import asyncio
import json
import sys

from aiohttp import web


def coordinate(value):
    if type(value) is not int or not 0 <= value <= 32767:
        raise ValueError('Coordinates must be integers from 0 to 32767')
    return value


class Walkthrough:
    def __init__(self):
        self.step = None
        self.generation = 0
        self.last_pointer = None
        self.pointer = None
        self.timer = None
        self.deadline = 0
        self.changed = asyncio.Event()

    def notify(self):
        self.changed.set()
        self.changed = asyncio.Event()

    def clear(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.step = None
        self.pointer = None
        self.notify()

    def show(self, data):
        x, y = coordinate(data['x']), coordinate(data['y'])
        text, seconds = data['text'], data['duration_seconds']
        if not isinstance(text, str):
            raise ValueError('text must be a string')
        if type(seconds) not in (int, float) or not 0 < seconds <= sys.float_info.max / 1000:
            raise ValueError('duration_seconds must be positive and finite')
        px, py = data.get('pointer_x'), data.get('pointer_y')
        if (px is None) != (py is None):
            raise ValueError('Supply both pointer coordinates together')
        pointer = (self.last_pointer or (x, y)) if px is None else (coordinate(px), coordinate(py))
        if self.timer:
            self.timer.cancel()
        self.generation += 1
        self.step = dict(id=self.generation, x=x, y=y, text=text, duration_ms=seconds * 1000)
        self.pointer = pointer
        loop = asyncio.get_running_loop()
        self.deadline = loop.time() + seconds
        self.timer = loop.call_at(self.deadline, self.clear)
        self.notify()
        return {'step_id': self.generation, 'duration_seconds': seconds}

    def move(self, x, y):
        self.last_pointer = (x, y)
        if self.step and self.pointer != self.last_pointer:
            self.pointer = self.last_pointer
            self.notify()

    async def events(self, request):
        response = web.StreamResponse(headers={'Content-Type':'text/event-stream',
                                               'Cache-Control':'no-store', 'X-Accel-Buffering':'no'})
        await response.prepare(request)
        previous = -1
        previous_pointer = None
        try:
            while True:
                changed = self.changed
                snapshot_pointer = self.pointer
                remaining = (self.deadline - asyncio.get_running_loop().time()) * 1000
                current = self.generation if self.step and remaining > 0 else 0
                if current != previous:
                    snapshot = dict(self.step, pointer=snapshot_pointer, remaining_ms=remaining) if current else None
                    await response.write(f'event: step\ndata: {json.dumps(snapshot)}\n\n'.encode())
                    previous = current
                elif current and snapshot_pointer != previous_pointer:
                    await response.write(f'event: pointer\ndata: {json.dumps(snapshot_pointer)}\n\n'.encode())
                previous_pointer = snapshot_pointer
                try:
                    await asyncio.wait_for(changed.wait(), 15)
                except TimeoutError:
                    await response.write(b': keepalive\n\n')
        except ConnectionError:
            pass
        return response
