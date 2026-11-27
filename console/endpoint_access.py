"""Private Chrome and SSH clients shared by the browser UI and MCP."""
import asyncio
import base64
import logging
import re
import socket
from contextlib import asynccontextmanager
from urllib.parse import quote, urlsplit

import aiohttp
import asyncssh

logging.getLogger('asyncssh').setLevel(logging.WARNING)


def service_socket(manager, endpoint_id, kind):
    manager.endpoint(endpoint_id, kind)
    path = manager.endpoint_dir(endpoint_id) / 'socket/service.sock'
    if not path.is_socket():
        raise ValueError('先にコンテナを起動してください')
    return path


def page_url(url):
    parsed = urlsplit(url)
    if (url != 'about:blank' and (parsed.scheme not in ('http', 'https') or not parsed.hostname
                                  or parsed.username is not None or parsed.password is not None)):
        raise ValueError('HTTP または HTTPS のURLを指定してください')
    return url


def tab_path(tab_id):
    if not re.fullmatch(r'[a-fA-F0-9]{32}', tab_id):
        raise ValueError('一覧にあるタブIDを指定してください')
    return tab_id


@asynccontextmanager
async def chrome_client(manager, endpoint_id):
    path = service_socket(manager, endpoint_id, 'browser')
    async with aiohttp.ClientSession('http://localhost', connector=aiohttp.UnixConnector(path=str(path)),
                                     timeout=aiohttp.ClientTimeout(total=12)) as client:
        yield client


def tab_data(raw):
    return {'id': raw['id'], 'title': raw['title'], 'url': raw['url']}


async def tabs(manager, endpoint_id):
    async with chrome_client(manager, endpoint_id) as client:
        async with client.get('/json/list', raise_for_status=True) as response:
            return [tab_data(item) for item in await response.json() if item['type'] == 'page']


async def new_tab(manager, endpoint_id, url='about:blank'):
    async with chrome_client(manager, endpoint_id) as client:
        async with client.put('/json/new?' + quote(page_url(url), safe=''), raise_for_status=True) as response:
            return tab_data(await response.json())


async def close_tab(manager, endpoint_id, tab_id):
    async with chrome_client(manager, endpoint_id) as client:
        async with client.get('/json/close/' + tab_path(tab_id), raise_for_status=True):
            return {'closed': tab_id}


class DevTools:
    def __init__(self, websocket):
        self.websocket = websocket
        self.sequence = 0

    async def send(self, name, params):
        self.sequence += 1
        identifier = self.sequence
        await self.websocket.send_json({'id': identifier, 'method': name, 'params': params})
        return identifier

    async def call(self, name, params):
        identifier = await self.send(name, params)
        async with asyncio.timeout(10):
            async for event in self.websocket:
                if event.type != aiohttp.WSMsgType.TEXT:
                    continue
                answer = event.json()
                if answer.get('id') == identifier:
                    if 'error' in answer:
                        raise ValueError(answer['error']['message'])
                    return answer['result']
        raise OSError('Chromeから応答がありません')


@asynccontextmanager
async def devtools(manager, endpoint_id, tab_id, activate=False):
    tab_path(tab_id)
    async with chrome_client(manager, endpoint_id) as client:
        if activate:
            async with client.get('/json/activate/' + tab_id, raise_for_status=True):
                pass
        async with client.ws_connect('/devtools/page/' + tab_id, max_msg_size=8 * 1024 * 1024) as ws:
            yield DevTools(ws)


async def navigate(manager, endpoint_id, tab_id, url):
    url = page_url(url)
    async with devtools(manager, endpoint_id, tab_id, activate=True) as browser:
        result = await browser.call('Page.navigate', {'url': url})
    if result.get('errorText'):
        raise ValueError('Chrome: ' + result['errorText'])
    return {'url': url, 'tab_id': tab_id}


async def capture(manager, endpoint_id, tab_id):
    async with devtools(manager, endpoint_id, tab_id, activate=True) as browser:
        result = await browser.call('Page.captureScreenshot', {'format': 'png'})
    return base64.b64decode(result['data'])


def browser_event(event):
    """Map UI / MCP input to the supported DevTools input methods."""
    if not isinstance(event, dict):
        raise ValueError('入力はオブジェクトで指定してください')
    operation = event.get('type')
    if operation == 'text':
        text = event.get('text')
        if not isinstance(text, str) or len(text) > 4096:
            raise ValueError('入力は4096文字までです')
        return 'Input.insertText', {'text': text}
    if operation in ('mouseMoved', 'mousePressed', 'mouseReleased', 'mouseWheel'):
        data = {key: event[key] for key in ('x', 'y', 'deltaX', 'deltaY') if key in event}
        if (not {'x', 'y'} <= data.keys() or
                any(type(value) not in (int, float) or abs(value) > 32768 or value != value for value in data.values())):
            raise ValueError('無効なポインター座標です')
        button = event.get('button', 'none')
        if button not in ('none', 'left', 'middle', 'right'):
            raise ValueError('無効なマウスボタンです')
        return 'Input.dispatchMouseEvent', dict(
            data, type=operation, button=button,
            buttons=bounded_int(event, 'buttons', 0, 7),
            modifiers=bounded_int(event, 'modifiers', 0, 15),
            clickCount=bounded_int(event, 'clickCount', 1, 3),
        )
    if operation in ('rawKeyDown', 'keyUp'):
        key, code = event.get('key'), event.get('code')
        if not isinstance(key, str) or not isinstance(code, str) or max(len(key), len(code)) > 48:
            raise ValueError('無効なキーです')
        return 'Input.dispatchKeyEvent', {'type': operation, 'key': key, 'code': code,
                                          'windowsVirtualKeyCode': bounded_int(event, 'keyCode', 0, 255),
                                          'modifiers': bounded_int(event, 'modifiers', 0, 15)}
    raise ValueError('未対応の入力操作です')


def bounded_int(data, key, default, limit):
    value = data.get(key, default)
    if type(value) is not int or not 0 <= value <= limit:
        raise ValueError(f'{key} は 0〜{limit} の整数で指定してください')
    return value


async def send_input(manager, endpoint_id, tab_id, event):
    command, params = browser_event(event)
    async with devtools(manager, endpoint_id, tab_id) as browser:
        await browser.call(command, params)
    return {'sent': True}


@asynccontextmanager
async def linux_ssh(manager, endpoint_id):
    path = service_socket(manager, endpoint_id, 'linux')
    keys = manager.endpoint_dir(endpoint_id) / 'ssh'
    host_key = asyncssh.read_public_key(keys / 'server.pub')
    with socket.socket(socket.AF_UNIX) as sock:
        sock.setblocking(False)
        await asyncio.get_running_loop().sock_connect(sock, str(path))
        async with asyncssh.connect('endpoint', sock=sock, config=None, username='root',
                                    known_hosts=([host_key], [], []), client_keys=[keys / 'client'],
                                    agent_path=None, connect_timeout=8) as connection:
            yield connection


async def linux_exec(manager, endpoint_id, command, timeout=30):
    if not isinstance(command, str) or not command.strip() or '\0' in command or len(command) > 4096:
        raise ValueError('コマンドを4096文字まで指定してください')
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError('タイムアウトを1〜120秒で指定してください')

    async def collect(stream):
        result = bytearray()
        while block := await stream.read(8192):
            result.extend(block)
            if len(result) > 131072:
                raise ValueError('コマンドの出力が128 KiBを超えました')
        return result.decode(errors='replace')

    async with asyncio.timeout(timeout), linux_ssh(manager, endpoint_id) as connection:
        async with connection.create_process(command, encoding=None) as process:
            process.stdin.write_eof()
            output, errors = await asyncio.gather(collect(process.stdout), collect(process.stderr))
            result = await process.wait()
            return {'stdout': output, 'stderr': errors, 'exit_status': result.exit_status}
