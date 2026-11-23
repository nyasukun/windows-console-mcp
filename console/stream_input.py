"""Bounded, atomic input batches shared with the console's other controllers."""
import asyncio

from aiohttp import WSMsgType, web

from control_config import KEYS

BUTTONS = {'left', 'middle', 'right', 'side', 'extra'}


def transition(kind, name, down):
    if kind == 'key':
        return {'type':'key', 'data':{'key':{'type':'qcode', 'data':name}, 'down':down}}
    return {'type':'btn', 'data':{'button':name, 'down':down}}


def prepare(events, pressed):
    if not isinstance(events, list) or not 1 <= len(events) <= 256:
        raise ValueError('Send 1..256 input events')
    held = set(pressed)
    batches = []
    for event in events:
        kind = event['op']
        change = None
        if kind == 'move':
            if any(type(event[a]) is not int or not 0 <= event[a] <= 32767 for a in ('x','y')):
                raise ValueError('Coordinates must be integers in 0..32767')
            group = [{'type':'abs','data':{'axis':a,'value':event[a]}} for a in ('x','y')]
        elif kind in ('button','key'):
            name, down = event[kind], event['down']
            if name not in (KEYS if kind == 'key' else BUTTONS) or type(down) is not bool:
                raise ValueError('Invalid key or button transition')
            ident = (kind, name)
            if down == (ident in held):
                raise ValueError('Unbalanced key or button transition')
            (held.add if down else held.remove)(ident)
            change = (ident, down)
            group = [transition(kind, name, down)]
        elif kind == 'wheel' and event['direction'] in ('up','down'):
            name = 'wheel-' + event['direction']
            group = [transition('btn', name, True), transition('btn', name, False)]
        else:
            raise ValueError('Unknown input event')
        batches.append((group, change))
    return batches


async def batch_input(request):
    shared = request.app['shared']
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=100000)
    await ws.prepare(request)
    gate = shared['input_lock']
    held = set()
    owns = False

    async def release():
        nonlocal owns
        try:
            if held:
                await shared['monitor'].command('input-send-event', {
                    'events':[transition(k,n,False) for k,n in held]})
        finally:
            held.clear()
            if owns:
                gate.release()
                owns = False

    try:
        while not ws.closed:
            async with asyncio.timeout(20 if held else None):
                packet = await ws.receive()
            if packet.type != WSMsgType.TEXT:
                break
            count = 0
            try:
                batches = prepare(packet.json()['events'], held)
                if not owns:
                    if gate.locked():
                        raise ValueError('Another input operation is in progress')
                    await gate.acquire()
                    owns = True
                    if shared['held']:
                        raise ValueError('Browser input is held')
                for group, change in batches:
                    if change and change[1]:
                        held.add(change[0])
                    await shared['monitor'].command('input-send-event', {'events':group})
                    count += 1
                    if change and not change[1]:
                        held.discard(change[0])
                if not held:
                    await release()
                await ws.send_json({'accepted':count, 'held':len(held)})
            except (KeyError, TypeError, ValueError, OSError, TimeoutError) as exc:
                await release()
                await ws.send_json({'accepted':count, 'held':0, 'error':str(exc) or 'Input timed out'})
    except asyncio.TimeoutError:
        await release()
        await ws.close(message=b'Input hold timed out')
    finally:
        await release()
    return ws
