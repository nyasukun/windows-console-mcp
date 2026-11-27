"""Web sessions for container Chrome and an SSH PTY."""
import asyncio
import base64
import json
from contextlib import suppress

import aiohttp
import asyncssh
from aiohttp import web

import endpoint_access as access
from control_config import ROOT


async def until_closed(send, receive):
    tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def install(app, manager):
    async def console(request):
        try:
            item = manager.endpoint(request.match_info['endpoint_id'])
        except ValueError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
        name = 'browser' if item['kind'] == 'browser' else 'terminal'
        return web.FileResponse(ROOT / 'web' / (name + '.html'))

    async def tabs(request):
        endpoint_id = request.match_info['endpoint_id']
        tab_id = request.match_info.get('tab_id')
        try:
            if request.method == 'GET':
                result = await access.tabs(manager, endpoint_id)
            elif request.method == 'DELETE':
                result = await access.close_tab(manager, endpoint_id, tab_id)
            else:
                body = await request.json()
                if not isinstance(body, dict) or not isinstance(body.get('url', ''), str):
                    raise ValueError('URLを指定してください')
                if tab_id:
                    result = await access.navigate(manager, endpoint_id, tab_id, body.get('url', ''))
                else:
                    result = await access.new_tab(manager, endpoint_id, body.get('url', 'about:blank'))
        except (ValueError, KeyError) as exc:
            return web.json_response({'error': str(exc)}, status=400)
        except (OSError, aiohttp.ClientError, TimeoutError) as exc:
            return web.json_response({'error': str(exc) or 'Chromeに接続できません'}, status=503)
        return web.json_response(result)

    async def display(request):
        endpoint_id, tab_id = request.match_info['endpoint_id'], request.match_info['tab_id']
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16384)
        try:
            async with access.devtools(manager, endpoint_id, tab_id, activate=True) as browser:
                start = await browser.send('Page.startScreencast', {'format': 'jpeg', 'quality': 78})
                await ws.prepare(request)

                async def send():
                    async for message in browser.websocket:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        event = message.json()
                        if event.get('id') == start and 'error' in event:
                            raise ValueError(event['error']['message'])
                        if event.get('method') != 'Page.screencastFrame':
                            continue
                        frame = event['params']
                        await ws.send_bytes(base64.b64decode(frame['data']))
                        await browser.send('Page.screencastFrameAck', {'sessionId': frame['sessionId']})

                async def receive():
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            command, params = access.browser_event(message.json())
                            await browser.send(command, params)
                        except ValueError as exc:
                            await ws.send_json({'error': str(exc)})

                await until_closed(send, receive)
        except (ValueError, OSError, aiohttp.ClientError, TimeoutError) as exc:
            if not ws.prepared:
                raise web.HTTPServiceUnavailable(text=str(exc) or 'Chromeに接続できません') from exc
            await ws.close(code=1011)
        return ws

    async def terminal(request):
        endpoint_id = request.match_info['endpoint_id']
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=65536)
        try:
            async with access.linux_ssh(manager, endpoint_id) as connection:
                async with connection.create_process(term_type='xterm-256color', term_size=(100, 30),
                                                     encoding=None) as process:
                    await ws.prepare(request)

                    async def send():
                        while data := await process.stdout.read(16384):
                            await ws.send_bytes(data)

                    async def receive():
                        async for message in ws:
                            if message.type == aiohttp.WSMsgType.BINARY:
                                process.stdin.write(message.data)
                                await process.stdin.drain()
                            elif message.type == aiohttp.WSMsgType.TEXT:
                                with suppress(ValueError, KeyError, TypeError):
                                    size = json.loads(message.data)
                                    cols, rows = size['cols'], size['rows']
                                    if (type(cols) is int and type(rows) is int
                                            and 1 <= cols <= 500 and 1 <= rows <= 300):
                                        process.change_terminal_size(cols, rows)

                    await until_closed(send, receive)
        except (ValueError, OSError, asyncssh.Error, TimeoutError) as exc:
            if not ws.prepared:
                raise web.HTTPServiceUnavailable(text=str(exc) or 'SSHに接続できません') from exc
            await ws.close(code=1011)
        return ws

    app.router.add_get('/endpoints/{endpoint_id}/console/', console)
    base = '/api/endpoints/{endpoint_id}'
    app.router.add_route('GET', base + '/browser/tabs', tabs)
    app.router.add_route('POST', base + '/browser/tabs', tabs)
    app.router.add_route('PUT', base + '/browser/tabs/{tab_id}', tabs)
    app.router.add_route('DELETE', base + '/browser/tabs/{tab_id}', tabs)
    app.router.add_get(base + '/browser/display/{tab_id}', display)
    app.router.add_get(base + '/terminal', terminal)
