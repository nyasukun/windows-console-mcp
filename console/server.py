import asyncio
import base64
import fcntl
import json
import os
import tempfile
from contextlib import nullcontext, suppress
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from live_screen import frames, jpeg
from management_http import install as install_management
from vm_manager import VMManager
from qmp import Monitor, SocketReplaced, socket_identity
from stream_input import CONSOLE_SESSION, batch_input
from walkthrough import Walkthrough
from transfer_http import GuestServer
from control_config import CONTROL, LEGACY_CONTROL, KEYS, ROOT, SERVICE_STATE, STATE, configured_port
SPICE_JS = Path(os.environ.get('CONSOLE_SPICE_SRC', Path.home() / '.local/share/windows-console/spice-src'))
BUTTONS = ('left', 'middle', 'right', 'side', 'extra')


@web.middleware
async def local_only(request, handler):
    host = request.url.host
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise web.HTTPForbidden()
    socket_path = request.path.rsplit('/', 1)[-1] in ('input', 'display', 'display-frames')
    mutation = request.path.startswith('/api/') and request.method not in ('GET', 'HEAD')
    if socket_path or mutation:
        origin = urlsplit(request.headers.get('Origin', ''))
        if origin.scheme not in ('http', 'https') or origin.netloc != request.host:
            raise web.HTTPForbidden()
    return await handler(request)


async def text_bridge(op, text='', state=None, check_socket=None):
    data = text.encode()
    if len(data) > 65536:
        raise ValueError('テキストは64 KiBまでです')
    reader, writer = await asyncio.open_unix_connection((state or STATE) / 'console-text.sock', limit=100000)
    try:
        if check_socket is not None:
            check_socket()
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
        raise ConnectionError('Windows の補助ツールとの接続が切れました')
    except TimeoutError as exc:
        raise TimeoutError(
            'Windows の補助ツールから応答がありません。サインイン直後は少し待ってください。'
            '応答しない場合は、エクスプローラーの「PC」から「CONSOLE_TOOLS」を開き、'
            'install.cmd を実行してから再試行してください。'
        ) from exc
    finally:
        writer.close()
        await writer.wait_closed()


def key_event(key, down):
    return {'type':'key', 'data':{'key':{'type':'qcode','data':key}, 'down':down}}


def button_event(button, down):
    return {'type':'btn', 'data':{'button':button, 'down':down}}


async def input_session(request):
    admin = request.app.get('admin', False)
    managed = request.get(CONSOLE_SESSION)
    shared = managed if managed is not None else request.app['shared']
    check_socket = shared['monitor'].check_socket if managed is not None else None
    lock = shared['input_lock'] if admin else nullcontext()
    if admin and lock.locked():
        raise web.HTTPConflict(text='別の入力操作が進行中です')
    async with lock:
        if admin and shared['held']:
            raise web.HTTPConflict(text='ブラウザでキーまたはボタンが押されています')
        ws = web.WebSocketResponse(heartbeat=15, receive_timeout=30 if admin else None,
                                   max_msg_size=400000)
        monitor = shared['monitor']
        keys, buttons = set(), set()
        primary = not admin and shared.get('spice_owner') is None
        if primary:
            shared['spice_owner'] = ws
        try:
            try:
                await monitor.command('query-status')
            except (OSError, TimeoutError) as exc:
                raise web.HTTPServiceUnavailable(text='VM に接続できません') from exc
            await ws.prepare(request)
            await ws.send_json({'ready':True, 'display':'spice' if primary else 'frames'} if not admin else {'ready':True})
            async for packet in ws:
                if packet.type != WSMsgType.TEXT:
                    break
                msg = json.loads(packet.data)
                op = msg['op']
                guard = nullcontext() if admin else shared['input_lock']
                async with guard:
                    try:
                        if check_socket is not None:
                            check_socket()
                        owner = shared['held']
                        if not admin and owner and owner is not ws:
                            if op in ('move', 'release') or (op in ('key', 'button') and msg.get('down') is not True):
                                continue
                            raise ValueError('別のタブでキーまたはマウスボタンが押されています')
                        if not admin:
                            # Ignore stale releases after a rejected press or focus change.
                            if op == 'key' and msg.get('down') is not True and msg['key'] not in keys:
                                continue
                            if op == 'button' and msg.get('down') is not True and BUTTONS[msg['button']] not in buttons:
                                continue
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
                            result = await text_bridge(op, msg.get('text',''), shared.get('state'), check_socket)
                            await ws.send_json({'id':msg.get('id'), 'result':result})
                        elif op != 'move':
                            raise ValueError('未知の入力')
                        if not admin:
                            shared['held'] = ws if keys or buttons else None
                        if events:
                            await monitor.command('input-send-event', {'events': events})
                            if op == 'move':
                                await ws.send_json({'motion':msg.get('seq')})
                        if msg.get('id') and op not in ('paste','type','copy','ping'):
                            await ws.send_json({'id':msg['id'], 'result':None})
                    except SocketReplaced:
                        await ws.close(code=1012, message=b'VM restarted')
                        break
                    except (ValueError, KeyError, IndexError, OSError, TimeoutError) as exc:
                        await ws.send_json({'id':msg.get('id'), 'error':str(exc) or 'Windows 応答タイムアウト'})
        finally:
            guard = nullcontext() if admin else shared['input_lock']
            async with guard:
                if shared['held'] is ws:
                    shared['held'] = None
                if primary:
                    shared['spice_owner'] = None
                release = [key_event(k,False) for k in keys]+[button_event(b,False) for b in buttons]
                if release:
                    with suppress(OSError, TimeoutError):
                        await monitor.command('input-send-event', {'events':release})
        return ws


async def display(request):
    state = request.get(CONSOLE_SESSION, {}).get('state', STATE)
    try:
        reader, writer = await asyncio.open_unix_connection(state / 'spice/console.sock')
    except OSError as exc:
        raise web.HTTPServiceUnavailable(text='VM に接続できません') from exc
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


async def display_frames(request):
    """Serve a shared read-only RFB connection without claiming the SPICE session."""
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=1024)
    await ws.prepare(request)

    async def deliver():
        last = None
        shared = request.get(CONSOLE_SESSION, {})
        state = None if shared.get('legacy_rfb') else shared.get('state')
        async for canvas in frames(20, state):
            encoded, _ = await asyncio.to_thread(jpeg, canvas, 1920)
            if encoded != last:
                async with asyncio.timeout(10):
                    await ws.send_bytes(encoded)
                last = encoded

    async def wait_for_close():
        async for _ in ws:
            pass

    tasks = [asyncio.create_task(deliver()), asyncio.create_task(wait_for_close())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (EOFError, OSError, ValueError, TimeoutError):
        await ws.close(code=1011, message=b'Display disconnected')
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await ws.close()
    return ws


async def administrative(request):
    shared = request.get(CONSOLE_SESSION) or request.app['shared']
    monitor = shared['monitor']
    state = shared.get('state', STATE)
    op = request.match_info['op']
    try:
        if op == 'demo-show':
            try:
                result = shared['demo'].show(await request.json())
            except (KeyError, ValueError, TypeError) as exc:
                raise web.HTTPBadRequest(text=str(exc)) from exc
        elif op == 'demo-clear':
            shared['demo'].clear()
            result = {'cleared':True}
        elif op in ('clipboard-read', 'clipboard-write'):
            value = (await request.json())['text'] if op == 'clipboard-write' else ''
            if not isinstance(value, str):
                raise web.HTTPBadRequest(text='text must be a string')
            if '\x00' in value:
                raise web.HTTPBadRequest(text='Clipboard text must not contain NUL')
            async with shared['input_lock']:
                if request.get(CONSOLE_SESSION) is None:
                    answer = await text_bridge(op, value)
                else:
                    answer = await text_bridge(op, value, state, monitor.check_socket)
            result = {'text': answer} if op == 'clipboard-read' else {'written': True}
        elif op == 'transfer-server':
            attach = (await request.json()).get('attach', True)
            result = await request.app['guest_server'](request, state, monitor if attach else None)
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
                if not isinstance(path, str) or path not in allowed_media_paths(state):
                    raise ValueError('Unknown ISO')
                await monitor.command('blockdev-change-medium', {
                    'id':drive['qdev'], 'filename':path, 'format':'raw',
                    'read-only-mode':'read-only'})
                return web.json_response({'mounted':path})
            result = {'path': drive.get('inserted', {}).get('file')}
        elif op == 'powerdown':
            result = await monitor.command('system_powerdown')
        elif op == 'screen':
            with tempfile.NamedTemporaryFile(dir=state, suffix='.png') as out:
                await monitor.command('screendump', {
                    'filename':'/vm-clean/' + Path(out.name).name, 'format':'png'})
                return web.Response(body=Path(out.name).read_bytes(), content_type='image/png')
        else:
            raise web.HTTPNotFound()
        return web.json_response(result)
    except (KeyError, ValueError, OSError, TimeoutError) as exc:
        raise web.HTTPServiceUnavailable(text=str(exc) or 'QEMU timed out') from exc


def allowed_media_paths(state=STATE):
    paths = {'/media/windows11.iso'}
    for directory in (state, state / 'media'):
        paths.update('/vm-clean/' + str(p.relative_to(state))
                     for p in directory.glob('*.iso') if p.is_file() and not p.is_symlink())
    return paths


def private_app(app):
    sessions = app['console_sessions']
    manager = sessions.manager
    guests = {}
    guest_lock = asyncio.Lock()

    @web.middleware
    async def choose_session(request, handler):
        vm_id = request.match_info.get('vm_id')
        request[CONSOLE_SESSION] = sessions.get(vm_id) if vm_id else sessions.legacy()
        return await handler(request)

    async def guest_server(request, state, monitor):
        vm_id = request.match_info.get('vm_id')
        host = manager.transfer_network(manager.vm(vm_id))[1] if vm_id and state != manager.legacy_state else None
        async with guest_lock:
            entry = guests.get(state)
            if entry is None or entry[0] != host:
                if entry is not None:
                    await entry[1].close()
                guest = GuestServer(state, host) if host else GuestServer(state)
                guests[state] = (host, guest)
            else:
                guest = entry[1]
            return await guest.ensure(monitor)

    async def restore_guests(app):
        for vm in manager.read()['vms'].values():
            if vm.get('legacy_container') or vm.get('deleting'):
                continue
            state = manager.vm_dir(vm['id'])
            if (state / 'transfers').is_dir():
                host = manager.transfer_network(vm)[1]
                guest = GuestServer(state, host)
                await guest.listen()
                guests[state] = (host, guest)
        if (manager.legacy_state / 'transfers').is_dir():
            guest = GuestServer(manager.legacy_state)
            await guest.listen()
            guests[manager.legacy_state] = (None, guest)
        try:
            yield
        finally:
            for _, guest in guests.values():
                await guest.close()

    async def walkthrough(request):
        return await request[CONSOLE_SESSION]['demo'].events(request)

    private = web.Application(middlewares=[choose_session])
    private['admin'] = True
    private['port'] = app['port']
    private['guest_server'] = guest_server
    private.cleanup_ctx.append(restore_guests)
    prefix = r'/vms/{vm_id:[0-9a-f]{12}}'
    for scope in ('', prefix):
        private.router.add_post(scope + '/control/{op}', administrative)
        private.router.add_get(scope + '/input', input_session)
        private.router.add_get(scope + '/stream-input', batch_input)
        private.router.add_get(scope + '/walkthrough', walkthrough)
    return private


async def admin_site(app):
    runner = web.AppRunner(private_app(app), access_log=None, shutdown_timeout=2)
    SERVICE_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    await runner.setup()
    try:
        for path in (CONTROL, LEGACY_CONTROL):
            if path == LEGACY_CONTROL and not path.parent.is_dir():
                continue
            await web.UnixSite(runner, str(path)).start()
            path.chmod(0o600)
        yield
    finally:
        await runner.cleanup()


def session(demo, state, identity=None):
    return {'demo': demo, 'monitor': Monitor(demo, state / 'qmp.sock', identity), 'spice_owner': None,
            'input_lock': asyncio.Lock(), 'held': None, 'state': state}


class ManagedConsoleSessions:
    """Keep input ownership and the monitor together for each QEMU process."""

    def __init__(self, manager):
        self.manager = manager
        self.sessions = {}
        self.legacy_demo = Walkthrough()
        self.legacy_shared = None

    def legacy(self):
        state = self.manager.legacy_state
        identity = socket_identity(state / 'qmp.sock')
        shared = self.legacy_shared
        if shared is not None and shared['monitor'].identity != identity:
            shared['monitor'].close()
            shared = None
        if shared is None:
            shared = self.legacy_shared = session(self.legacy_demo, state, identity)
            shared['legacy_rfb'] = True
        return shared

    def get(self, vm_id):
        vms = self.manager.read()['vms']
        for removed in self.sessions.keys() - vms.keys():
            self.sessions.pop(removed)['monitor'].close()
        if vm_id not in vms or vms[vm_id].get('deleting'):
            raise web.HTTPNotFound()
        if vms[vm_id].get('legacy_container'):
            return self.legacy()
        state = self.manager.vm_dir(vm_id)
        identity = socket_identity(state / 'qmp.sock')
        shared = self.sessions.get(vm_id)
        if shared is not None and shared['monitor'].identity != identity:
            shared['monitor'].close()
            shared = None
        if shared is None:
            shared = self.sessions[vm_id] = session(Walkthrough(), state, identity)
        return shared

    def close(self):
        for shared in self.sessions.values():
            shared['monitor'].close()
        self.sessions.clear()
        if self.legacy_shared is not None:
            self.legacy_shared['monitor'].close()


def create_app(manager=None):
    manager = manager or VMManager()
    app = web.Application(middlewares=[local_only])
    app['port'] = configured_port()
    sessions = ManagedConsoleSessions(manager)
    app['console_sessions'] = sessions
    app.cleanup_ctx.append(admin_site)
    app.router.add_get('/walkthrough', sessions.legacy_demo.events)

    def for_legacy(handler):
        async def scoped(request):
            request[CONSOLE_SESSION] = sessions.legacy()
            return await handler(request)
        return scoped

    app.router.add_get('/input', for_legacy(input_session))
    app.router.add_get('/display', for_legacy(display))
    app.router.add_get('/display-frames', for_legacy(display_frames))

    async def index(request):
        return web.FileResponse(ROOT / 'web/index.html')

    async def manager_index(request):
        return web.FileResponse(ROOT / 'web/manager.html')

    async def home(request):
        raise web.HTTPFound('/vms/')

    def for_vm(handler):
        async def scoped(request):
            shared = sessions.get(request.match_info['vm_id'])
            if handler is input_session and shared['monitor'].identity is None:
                raise web.HTTPServiceUnavailable(text='VM に接続できません')
            request[CONSOLE_SESSION] = shared
            return await handler(request)
        return scoped

    async def managed_walkthrough(request):
        return await request[CONSOLE_SESSION]['demo'].events(request)

    async def close_sessions(app):
        sessions.close()

    app.on_cleanup.append(close_sessions)
    app.router.add_get('/', home)
    app.router.add_get('/vms/', manager_index)
    prefix = r'/vms/{vm_id:[0-9a-f]{12}}/console/'
    for path, handler in (('', index), ('input', input_session), ('display', display),
                          ('display-frames', display_frames), ('walkthrough', managed_walkthrough)):
        app.router.add_get(prefix + path, for_vm(handler))
    install_management(app, manager)
    app.router.add_static('/ui/', ROOT / 'web')
    app.router.add_static('/spice/', SPICE_JS)
    return app


def main():
    app = create_app()
    # asyncio removes an existing Unix socket on bind. Protect the running
    # service when someone also launches the console manually.
    SERVICE_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (SERVICE_STATE / 'console-service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Windows console service already running')
        web.run_app(app, host='127.0.0.1', port=app['port'], access_log=None, shutdown_timeout=2)


if __name__ == '__main__':
    main()
