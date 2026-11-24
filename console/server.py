import asyncio
import base64
import fcntl
import json
import os
import tempfile
from contextlib import nullcontext
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from qmp import Monitor
from stream_input import batch_input
from walkthrough import Walkthrough
from transfer_http import GuestServer
from control_config import CONTROL, KEYS, ROOT, STATE, configured_port
SPICE_JS = Path(os.environ.get('CONSOLE_SPICE_SRC', Path.home() / '.local/share/windows-console/spice-src'))
BUTTONS = ('left', 'middle', 'right', 'side', 'extra')


@web.middleware
async def local_only(request, handler):
    host = request.url.host
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise web.HTTPForbidden()
    if request.path in ('/input', '/display'):
        origin = urlsplit(request.headers.get('Origin', ''))
        if origin.scheme not in ('http', 'https') or origin.netloc != request.host:
            raise web.HTTPForbidden()
    return await handler(request)


async def text_bridge(op, text=''):
    data = text.encode()
    if len(data) > 65536:
        raise ValueError('テキストは64 KiBまでです')
    reader, writer = await asyncio.open_unix_connection(STATE / 'console-text.sock', limit=100000)
    try:
        request_id = uuid.uuid4().hex
        writer.write(json.dumps(dict(id=request_id, op=op, data=base64.b64encode(data).decode())).encode()+b'\n')
        await writer.drain()
        async with asyncio.timeout(20):
            while line := await reader.readline():
                answer = json.loads(line)
                if answer.get('id') == request_id:
                    if not answer.get('ok'):
                        raise ValueError(answer.get('error', 'Windows clipboard error'))
                    return base64.b64decode(answer.get('data', '')).decode()
        raise ConnectionError('Windows text bridge disconnected')
    finally:
        writer.close()
        await writer.wait_closed()


def key_event(key, down):
    return {'type':'key', 'data':{'key':{'type':'qcode','data':key}, 'down':down}}


def button_event(button, down):
    return {'type':'btn', 'data':{'button':button, 'down':down}}


async def input_session(request):
    admin = request.app.get('admin', False)
    shared = request.app['shared']
    lock = shared['input_lock'] if admin else shared['browser']
    if lock.locked():
        raise web.HTTPConflict(text='別のタブが操作中です')
    async with lock:
        if admin and shared['held']:
            raise web.HTTPConflict(text='ブラウザでキーまたはボタンが押されています')
        ws = web.WebSocketResponse(heartbeat=15, receive_timeout=30 if admin else None,
                                   max_msg_size=400000)
        monitor = shared['monitor']
        keys, buttons = set(), set()
        try:
            await monitor.command('query-status')
            await ws.prepare(request)
            await ws.send_json({'ready':True})
            async for packet in ws:
                if packet.type != WSMsgType.TEXT:
                    break
                msg = json.loads(packet.data)
                op = msg['op']
                guard = nullcontext() if admin else shared['input_lock']
                async with guard:
                    try:
                        events = []
                        if op in ('move','button'):
                            for axis in ('x','y'):
                                value = max(0, min(32767, int(msg[axis])))
                                events.append({'type':'abs','data':{'axis':axis,'value':value}})
                        if op == 'button':
                            button = BUTTONS[msg['button']]
                            down = msg['down'] is True
                            (buttons.add if down else buttons.discard)(button)
                            events.append(button_event(button, down))
                        elif op == 'key':
                            key, down = msg['key'], msg['down'] is True
                            if key not in KEYS:
                                raise ValueError('未対応のキー')
                            (keys.add if down else keys.discard)(key)
                            events.append(key_event(key, down))
                        elif op == 'wheel':
                            button = 'wheel-up' if msg['delta'] < 0 else 'wheel-down'
                            events += [button_event(button, True), button_event(button, False)]
                        elif op == 'release':
                            events = [key_event(k,False) for k in keys]+[button_event(b,False) for b in buttons]
                            keys.clear()
                            buttons.clear()
                        elif op in ('paste','type','copy','ping'):
                            result = await text_bridge(op, msg.get('text',''))
                            await ws.send_json({'id':msg.get('id'), 'result':result})
                        elif op != 'move':
                            raise ValueError('未知の入力')
                        if not admin:
                            shared['held'] = bool(keys or buttons)
                        if events:
                            await monitor.command('input-send-event', {'events': events})
                            if op == 'move':
                                await ws.send_json({'motion':msg.get('seq')})
                        if msg.get('id') and op not in ('paste','type','copy','ping'):
                            await ws.send_json({'id':msg['id'], 'result':None})
                    except (ValueError, KeyError, IndexError, OSError, TimeoutError) as exc:
                        await ws.send_json({'id':msg.get('id'), 'error':str(exc) or 'Windows 応答タイムアウト'})
        finally:
            guard = nullcontext() if admin else shared['input_lock']
            async with guard:
                if not admin:
                    shared['held'] = False
                release = [key_event(k,False) for k in keys]+[button_event(b,False) for b in buttons]
                if release:
                    await monitor.command('input-send-event', {'events':release})
        return ws


async def display(request):
    reader, writer = await asyncio.open_unix_connection(STATE / 'spice/console.sock')
    ws = web.WebSocketResponse(protocols=('binary',), max_msg_size=4*1024*1024)
    await ws.prepare(request)

    async def upstream():
        async for msg in ws:
            if msg.type != WSMsgType.BINARY:
                break
            writer.write(msg.data)
            await writer.drain()

    async def downstream():
        while data := await reader.read(65536):
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        await ws.close()
    return ws


async def administrative(request):
    monitor = request.app['shared']['monitor']
    op = request.match_info['op']
    try:
        if op == 'demo-show':
            try:
                result = request.app['shared']['demo'].show(await request.json())
            except (KeyError, ValueError, TypeError) as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc
        elif op == 'demo-clear':
            request.app['shared']['demo'].clear()
            result = {'cleared':True}
        elif op == 'transfer-server':
            attach = (await request.json()).get('attach', True)
            result = await request.app['guest'].ensure(monitor if attach else None)
        elif op == 'health':
            result = {'port': request.app['port']}
        elif op == 'status':
            result = await monitor.command('query-status')
        elif op in ('medium', 'mount'):
            blocks = await monitor.command('query-block')
            drive = next((b for b in blocks if b.get('device') == 'install'), None)
            if not drive:
                raise ValueError('Installation CD drive missing')
            if op == 'mount':
                path = (await request.json())['path']
                if not isinstance(path, str) or path not in allowed_media_paths():
                    raise ValueError('Unknown ISO')
                await monitor.command('blockdev-change-medium', {
                    'id':drive['qdev'], 'filename':path, 'format':'raw',
                    'read-only-mode':'read-only'})
                return web.json_response({'mounted':path})
            result = {'path': drive.get('inserted', {}).get('file')}
        elif op == 'powerdown':
            result = await monitor.command('system_powerdown')
        elif op == 'screen':
            with tempfile.NamedTemporaryFile(dir=STATE, suffix='.png') as out:
                await monitor.command('screendump', {
                    'filename':'/vm-clean/' + Path(out.name).name, 'format':'png'})
                return web.Response(body=Path(out.name).read_bytes(), content_type='image/png')
        else:
            raise web.HTTPNotFound()
        return web.json_response(result)
    except (KeyError, ValueError, OSError, TimeoutError) as exc:
        raise web.HTTPServiceUnavailable(text=str(exc) or 'QEMU timed out') from exc


def allowed_media_paths():
    paths = {'/media/windows11.iso'}
    for directory in (STATE, STATE / 'media'):
        paths.update('/vm-clean/' + str(p.relative_to(STATE))
                     for p in directory.glob('*.iso') if p.is_file() and not p.is_symlink())
    return paths


async def admin_site(app):
    private = web.Application()
    private['admin'] = True
    private['shared'] = app['shared']
    private['port'] = app['port']
    guest = GuestServer()
    private['guest'] = guest
    private.router.add_post('/control/{op}', administrative)
    private.router.add_get('/input', input_session)
    private.router.add_get('/stream-input', batch_input)
    private.router.add_get('/walkthrough', app['shared']['demo'].events)
    runner = web.AppRunner(private, access_log=None, shutdown_timeout=2)
    await runner.setup()
    site = web.UnixSite(runner, str(CONTROL))
    try:
        await site.start()
        CONTROL.chmod(0o600)
        if (STATE / 'transfers').is_dir():
            await guest.listen()
        yield
    finally:
        await runner.cleanup()
        await guest.close()
        app['shared']['monitor'].close()


def main():
    app = web.Application(middlewares=[local_only])
    app['port'] = configured_port()
    demo = Walkthrough()
    app['shared'] = {'demo':demo, 'monitor':Monitor(demo), 'browser':asyncio.Lock(),
                     'input_lock':asyncio.Lock(), 'held':False}
    app.cleanup_ctx.append(admin_site)
    app.router.add_get('/walkthrough', demo.events)
    app.router.add_get('/input', input_session)
    app.router.add_get('/display', display)
    async def index(request):
        return web.FileResponse(ROOT/'web/index.html')
    app.router.add_get('/', index)
    app.router.add_static('/ui/', ROOT/'web')
    app.router.add_static('/spice/', SPICE_JS)
    # asyncio removes an existing Unix socket on bind. Protect the running
    # service when someone also launches the console manually.
    with (STATE / 'console-service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Windows console service already running')
        web.run_app(app, host='127.0.0.1', port=app['port'], access_log=None, shutdown_timeout=2)


if __name__ == '__main__':
    main()
