"""One duplex screen/input stream per stdio client, with latest-frame backpressure."""
import asyncio
import hashlib
import json
import uuid
from typing import Annotated, Literal

from mcp import types
from mcp.server.fastmcp import Image
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl, Field
from typing_extensions import TypedDict

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


class LiveStream:
    def __init__(self, connect):
        self.id = uuid.uuid4().hex
        self.uri = f'windows-console://live/{self.id}/frame'
        self.connect = connect
        self.changed = asyncio.Condition()
        self.input_lock = asyncio.Lock()
        self.input_seq = self.seq = 0
        self.frame = None
        self.last_ack = self.last_request = None
        self.closed = False
        self.problem = None
        self.client = self.ws = self.task = self.subscriber = None
        self.notified = False

    def metadata(self):
        return dict(stream_id=self.id, frame_sequence=self.seq, native_size=self.native,
                    image_size=self.dimensions, frame_uri=self.uri)

    async def capture(self, fps, width):
        try:
            async for canvas in frames(fps):
                frame, dimensions = await asyncio.to_thread(jpeg, canvas, width)
                if frame == self.frame and self.native == canvas.size:
                    continue
                async with self.changed:
                    self.seq += 1
                    self.frame, self.dimensions, self.native = frame, dimensions, canvas.size
                    self.changed.notify_all()
                if self.subscriber and not self.notified:
                    self.notified = True
                    await self.subscriber.send_resource_updated(AnyUrl(self.uri))
        except Exception as exc:
            self.problem = str(exc) or 'Display stream disconnected'
        finally:
            async with self.changed:
                self.changed.notify_all()
            if self.problem and self.subscriber and not self.closed:
                # Wake a resource-only reader so its next read reports the disconnect.
                try:
                    await self.subscriber.send_resource_updated(AnyUrl(self.uri))
                except (OSError, RuntimeError):
                    pass

    async def start(self, fps, width):
        self.task = asyncio.create_task(self.capture(fps,width))
        await self.wait(0, 10)
        if not self.frame:
            raise TimeoutError('No initial display frame received')

    async def wait(self, after, timeout):
        async with self.changed:
            try:
                async with asyncio.timeout(timeout):
                    await self.changed.wait_for(lambda: self.seq > after or self.problem or self.closed)
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
            self.last_ack = dict(ack, input_sequence=seq, frame_sequence=self.seq)
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
                                  max_width: Annotated[int, Field(ge=160,le=4096)]=1280) -> dict:
        """Start persistent incremental display capture. Subscribe to frame_uri or call console_stream_next."""
        nonlocal active
        async with lifecycle:
            if active:
                raise ValueError('Close the current stream first')
            item = LiveStream(connection)
            try:
                await item.start(max_fps, max_width)
            except BaseException:
                await item.close()
                raise
            active = item
            return dict(item.metadata(), next_input_sequence=1, max_fps=max_fps)

    @mcp.tool()
    async def console_stream_next(stream_id: str, after: Annotated[int, Field(ge=0)]=0,
                                  timeout_ms: Annotated[int, Field(ge=0,le=30000)]=5000) -> types.CallToolResult:
        """Wait for a newer frame. Returns JPEG and metadata, or metadata only on timeout."""
        item = stream(stream_id)
        await item.wait(after, timeout_ms / 1000)
        changed = item.seq > after
        content = [types.TextContent(type='text', text=json.dumps(dict(item.metadata(), changed=changed)))]
        if changed:
            content.append(Image(data=item.frame, format='jpeg').to_image_content())
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

    @mcp.resource('windows-console://live/{stream_id}/frame', mime_type='image/jpeg')
    def latest_frame(stream_id: str) -> bytes:
        item = stream(stream_id)
        if item.problem:
            raise RuntimeError(item.problem)
        item.notified = False
        return item.frame

    low = mcp._mcp_server

    def lookup(uri):
        if active is None or str(uri) != active.uri:
            raise ValueError('Unknown live frame resource')
        return active

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

    async def run():
        options = low.create_initialization_options()
        options.capabilities.resources.subscribe = True
        try:
            async with stdio_server() as (reader, writer):
                await low.run(reader, writer, options)
        finally:
            if active:
                await active.close()

    return run
