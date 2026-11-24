"""One duplex screen/input stream per MCP session, with latest-frame backpressure."""
import asyncio
import hashlib
import json
import uuid
from typing import Annotated, Literal

import aiohttp
from anyio import CancelScope
from mcp import types
from mcp.server.fastmcp import Image
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl, Field
from typing_extensions import TypedDict

from demo_frame import demo_jpeg
from live_screen import frames, jpeg


class Move(TypedDict):
    op: Literal['move']
    x: int
    y: int


class Key(TypedDict):
    op: Literal['key']
    key: str
    down: bool


class Button(TypedDict):
    op: Literal['button']
    button: Literal['left','middle','right','side','extra']
    down: bool


class Wheel(TypedDict):
    op: Literal['wheel']
    direction: Literal['up','down']


Events = Annotated[list[Move | Key | Button | Wheel], Field(min_length=1, max_length=256)]


class FrameView:
    def __init__(self, uri):
        self.uri = uri
        self.sequence = 0
        self.frame = None
        self.size = self.native = None
        self.timestamp_ms = 0
        self.subscriber = None
        self.notified = False

    async def notify(self):
        if self.subscriber and not self.notified:
            self.notified = True
            await self.subscriber.send_resource_updated(AnyUrl(self.uri))


View = Literal['bare', 'demo']


class LiveStream:
    def __init__(self, connect, views):
        self.id = uuid.uuid4().hex
        uri = f'windows-console://live/{self.id}/frame'
        names = ('bare', 'demo') if views == 'both' else (views,)
        self.views = {name: FrameView(uri if name == 'bare' else uri + '/demo') for name in names}
        self.primary = names[0]
        self.connect = connect
        self.changed = asyncio.Condition()
        self.repaint = asyncio.Event()
        self.canvas = self.step = None
        self.deadline = 0
        self.demo_ready = asyncio.Event()
        self.started_at = asyncio.get_running_loop().time()
        self.input_lock = asyncio.Lock()
        self.input_seq = 0
        self.last_ack = self.last_request = None
        self.closed = False
        self.problem = None
        self.client = self.ws = self.task = None

    def get_view(self, name=None):
        try:
            return self.views[name or self.primary]
        except KeyError:
            raise ValueError(f'View {name!r} was not enabled by console_stream_open') from None

    def metadata(self, name=None):
        view = self.get_view(name)
        return dict(stream_id=self.id, view=name or self.primary, frame_sequence=view.sequence,
                    timestamp_ms=view.timestamp_ms, native_size=view.native,
                    image_size=view.size, frame_uri=view.uri)

    async def publish(self, view, encoded, native):
        frame, size = encoded
        if view.frame == frame and view.native == native:
            return
        async with self.changed:
            view.frame, view.size, view.native = frame, size, native
            view.sequence += 1
            view.timestamp_ms = round((asyncio.get_running_loop().time() - self.started_at) * 1000)
            self.changed.notify_all()
        await view.notify()

    async def capture(self, fps, width):
        async for canvas in frames(fps):
            if 'demo' in self.views:
                # RFB updates its image in place after this iteration returns.
                self.canvas = canvas.copy()
                self.repaint.set()
            if 'bare' in self.views:
                await self.publish(self.views['bare'], await asyncio.to_thread(jpeg, canvas, width), canvas.size)
        raise RuntimeError('Display stream disconnected')

    async def watch_demo(self):
        async with self.connect() as client:
            async with client.get('http://localhost/walkthrough',
                                  timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=25)) as response:
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.content.iter_any():
                    buffer.extend(chunk)
                    while (end := buffer.find(b'\n\n')) >= 0:
                        message = bytes(buffer[:end])
                        del buffer[:end + 2]
                        if message.startswith(b'event: step\ndata: '):
                            self.step = json.loads(message.split(b'\n', 1)[1][6:])
                            remaining = self.step['remaining_ms'] / 1000 if self.step else 0
                            self.deadline = asyncio.get_running_loop().time() + remaining
                            self.demo_ready.set()
                            self.repaint.set()
                        elif message.startswith(b'event: pointer\ndata: ') and self.step:
                            self.step = dict(self.step, pointer=json.loads(message.split(b'\n', 1)[1][6:]))
                            self.repaint.set()
        raise RuntimeError('Walkthrough feed disconnected')

    async def render_demo(self, fps, width):
        await self.demo_ready.wait()
        loop = asyncio.get_running_loop()
        while True:
            await self.repaint.wait()
            self.repaint.clear()
            canvas = self.canvas
            if canvas is None:
                continue
            began = loop.time()
            remaining = self.deadline - began
            encoded = await asyncio.to_thread(demo_jpeg, canvas, width, self.step, remaining)
            await self.publish(self.views['demo'], encoded, canvas.size)
            if self.step and self.step['text'] and remaining > 0:
                # Render the countdown even when the guest framebuffer is idle.
                self.repaint.set()
            await asyncio.sleep(max(0, 1/fps - (loop.time() - began)))

    async def run_capture(self, fps, width):
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.capture(fps, width))
                if 'demo' in self.views:
                    tasks.create_task(self.watch_demo())
                    tasks.create_task(self.render_demo(fps, width))
        except ExceptionGroup as group:
            exc = group.exceptions[0]
            self.problem = str(exc) or 'Display stream disconnected'
        finally:
            async with self.changed:
                self.changed.notify_all()
            if self.problem and not self.closed:
                for view in self.views.values():
                    view.notified = False
                    try:
                        await view.notify()
                    except (OSError, RuntimeError):
                        pass

    async def start(self, fps, width):
        self.task = asyncio.create_task(self.run_capture(fps, width))
        for view in self.views.values():
            await self.wait(view, 0, 10)
            if not view.frame:
                raise TimeoutError('No initial display frame received')

    async def wait(self, view, after, timeout):
        async with self.changed:
            try:
                async with asyncio.timeout(timeout):
                    await self.changed.wait_for(lambda: view.sequence > after or self.problem or self.closed)
            except TimeoutError:
                pass
            if self.problem or self.closed:
                raise RuntimeError(self.problem or 'Stream closed')

    async def input(self, seq, events):
        async with self.input_lock:
            if self.closed or self.problem:
                raise RuntimeError(self.problem or 'Stream closed')
            signature = hashlib.sha256(json.dumps(events, sort_keys=True).encode()).digest()
            if seq == self.input_seq and signature == self.last_request:
                return self.last_ack
            if seq != self.input_seq + 1:
                raise ValueError(f'Expected input sequence {self.input_seq+1}')
            if self.client is None:
                self.client = self.connect()
            if self.ws is not None and self.ws.closed:
                raise RuntimeError('Input stream disconnected; close and reopen it')
            if self.ws is None:
                self.ws = await self.client.ws_connect('http://localhost/stream-input', heartbeat=15)
            try:
                await self.ws.send_json({'events':events})
                ack = await self.ws.receive_json(timeout=20)
            except BaseException:
                self.problem = 'Input acknowledgement lost; reopen and inspect the screen before retrying'
                async with self.changed:
                    self.changed.notify_all()
                raise
            self.input_seq, self.last_request = seq, signature
            self.last_ack = dict(ack, input_sequence=seq, frame_sequence=self.get_view().sequence)
            return self.last_ack

    async def close(self):
        self.closed = True
        async with self.changed:
            self.changed.notify_all()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.client:
            await self.client.close()


def install(mcp, connection):
    active = None
    lifecycle = asyncio.Lock()

    def stream(ident):
        if active is None or active.id != ident or active.closed:
            raise ValueError('Unknown stream; open a stream on this MCP connection')
        return active

    @mcp.tool()
    async def console_stream_open(max_fps: Annotated[int, Field(ge=1,le=30)]=12,
                                  max_width: Annotated[int, Field(ge=160,le=4096)]=1280,
                                  views: Literal['bare', 'demo', 'both']='bare') -> dict:
        """Start incremental JPEG capture: bare, demo with annotations, or both.
        Each enabled view has its own frame_uri and frame_sequence in the views map.
        Subscribe to its URI or pass view to console_stream_next. Both share one input stream.
        """
        nonlocal active
        async with lifecycle:
            if active:
                raise ValueError('Close the current stream first')
            item = LiveStream(connection, views)
            try:
                await item.start(max_fps, max_width)
            except BaseException:
                await item.close()
                raise
            active = item
            return dict(item.metadata(), next_input_sequence=1, max_fps=max_fps,
                        views={name: item.metadata(name) for name in item.views})

    @mcp.tool()
    async def console_stream_next(stream_id: str, after: Annotated[int, Field(ge=0)]=0,
                                  timeout_ms: Annotated[int, Field(ge=0,le=30000)]=5000,
                                  view: View | None = None) -> types.CallToolResult:
        """Wait for a newer frame. Returns JPEG and metadata, or metadata only on timeout."""
        item = stream(stream_id)
        selected = item.get_view(view)
        await item.wait(selected, after, timeout_ms / 1000)
        changed = selected.sequence > after
        content = [types.TextContent(type='text', text=json.dumps(dict(item.metadata(view), changed=changed)))]
        if changed:
            content.append(Image(data=selected.frame, format='jpeg').to_image_content())
        return types.CallToolResult(content=content)

    @mcp.tool()
    async def console_stream_input(stream_id: str, sequence: Annotated[int, Field(ge=1)], events: Events) -> dict:
        """Send an ordered input batch over the persistent console link. Coordinates 0..32767.
        Keys/buttons may span batches. Release within 20 seconds of inactivity. ACK reports applied count;
        inspect error on partial failure. After ACK advance sequence, including on a rejected batch.
        """
        return await stream(stream_id).input(sequence, events)

    @mcp.tool()
    async def console_stream_close(stream_id: str) -> dict:
        """Stop capture, release the input link and drop the cached frame."""
        nonlocal active
        async with lifecycle:
            await stream(stream_id).close()
            active = None
        return {'closed':True}

    def read_frame(stream_id, view):
        item = stream(stream_id)
        if item.problem:
            raise RuntimeError(item.problem)
        selected = item.get_view(view)
        selected.notified = False
        return selected.frame

    @mcp.resource('windows-console://live/{stream_id}/frame', mime_type='image/jpeg')
    def latest_frame(stream_id: str) -> bytes:
        return read_frame(stream_id, 'bare')

    @mcp.resource('windows-console://live/{stream_id}/frame/demo', mime_type='image/jpeg')
    def latest_demo_frame(stream_id: str) -> bytes:
        return read_frame(stream_id, 'demo')

    low = mcp._mcp_server

    def lookup(uri):
        if active:
            for view in active.views.values():
                if str(uri) == view.uri:
                    return view
        raise ValueError('Unknown live frame resource')

    @low.subscribe_resource()
    async def subscribe(uri):
        item = lookup(uri)
        item.subscriber = low.request_context.session
        item.notified = False

    @low.unsubscribe_resource()
    async def unsubscribe(uri):
        item = lookup(uri)
        item.subscriber = None
        item.notified = False

    async def run(reader=None, writer=None):
        if reader is None:
            async with stdio_server() as streams:
                return await run(*streams)
        options = low.create_initialization_options()
        options.capabilities.resources.subscribe = True
        try:
            await low.run(reader, writer, options)
        finally:
            if active:
                with CancelScope(shield=True):
                    await active.close()

    return run
