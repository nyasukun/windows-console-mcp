"""stdio MCP tools for the existing Windows VM and its HTML5 console."""
import asyncio
import fcntl
import os
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Literal

import aiohttp
from mcp.server.fastmcp import FastMCP, Image
from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import transfers
from live import install as install_live_stream
from control_config import CONTROL, KEYS, ROOT, STATE, configured_port, store_port, validate_port
from setup_media import build_setup_iso

VM = os.environ.get('WINDOWS_CONSOLE_VM', 'windows11-vm-clean')
UNIT = 'windows-html-console.service'
PROJECT = ROOT.parent
SELECTION = STATE / 'selected-install.iso'
Coord = Annotated[int, Field(ge=0, le=32767)]
tools = []


def export_tool(func):
    tools.append(func)
    return func


async def host(*argv, timeout=30):
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise RuntimeError(stderr.decode(errors='replace').strip() or f'{argv[0]} failed')
    return stdout.decode().strip()


async def container_state():
    return await host('docker', 'inspect', '--format', '{{.State.Status}}', VM)


@contextmanager
def management_lock():
    with (STATE / 'console-management.lock').open('a') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another management operation is in progress') from exc
        yield


def connection():
    return aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(CONTROL)),
                                 timeout=aiohttp.ClientTimeout(total=25))


async def control(op, **body):
    async with connection() as session:
        async with session.post('http://localhost/control/' + op, json=body) as response:
            if not response.ok:
                raise RuntimeError(await response.text())
            if op == 'screen':
                return await response.read()
            return await response.json()


@asynccontextmanager
async def console_input():
    async with connection() as session:
        async with session.ws_connect('http://localhost/input', max_msg_size=400000) as ws:
            if not (await ws.receive_json(timeout=8)).get('ready'):
                raise RuntimeError('Console did not become ready')

            async def send(op, **args):
                ident = uuid.uuid4().hex
                await ws.send_json(dict(id=ident, op=op, **args))
                async with asyncio.timeout(23):
                    while True:
                        reply = await ws.receive_json()
                        if reply.get('id') == ident:
                            if 'error' in reply:
                                raise RuntimeError(reply['error'])
                            return reply.get('result')
            yield send


async def chord(send, keys):
    for key in keys:
        await send('key', key=key, down=True)
    for key in reversed(keys):
        await send('key', key=key, down=False)


@export_tool
async def vm_status() -> dict:
    """Get the configured VM container and QEMU run state."""
    state = await container_state()
    result = {'vm': VM, 'container': state}
    if state == 'running':
        try:
            result['qemu'] = await control('status')
        except (aiohttp.ClientError, OSError, RuntimeError, TimeoutError) as exc:
            result['qemu_error'] = str(exc) or 'Monitor timed out'
    return result


async def wait_for_vm(target, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await container_state()
        if target == 'exited' and state == target:
            return
        if target == 'running' and state == target:
            try:
                if (await control('status'))['running']:
                    return
            except (aiohttp.ClientError, OSError, RuntimeError, TimeoutError):
                pass
        await asyncio.sleep(0.5)
    raise RuntimeError(f'Timed out waiting for {target}. Check vm_status and the console.')


async def change_power(action, state, timeout_seconds):
    if state not in ('running', 'exited', 'created'):
        raise RuntimeError(f'Unsupported current container state: {state}')
    if action == 'restart' or (action == 'start' and state != 'running'):
        await asyncio.to_thread(build_setup_iso, STATE)
    if action != 'start' and state == 'running':
        await control('powerdown')
        await wait_for_vm('exited', timeout_seconds)
        state = 'exited'
    if action != 'shutdown':
        if state != 'running':
            await host('docker', 'start', VM)
        await wait_for_vm('running', timeout_seconds)


TimeoutSeconds = Annotated[int, Field(ge=5, le=180)]


@export_tool
async def vm_power(action: Literal['start', 'shutdown', 'restart'],
                   timeout_seconds: TimeoutSeconds = 60) -> dict:
    """Start the VM or request ACPI shutdown. Restart starts after confirmed shutdown.
    Windows may require a user to close applications. Timeouts do not force a stop.
    """
    with management_lock():
        await change_power(action, await container_state(), timeout_seconds)
        return await vm_status()


def running_internet_enabled(pid):
    try:
        args = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except OSError:
        return None
    for flag, spec in zip(args, args[1:]):
        if flag != b'-netdev':
            continue
        options = spec.split(b',')
        if options[0] == b'user' and b'id=transfernet' in options:
            restriction = next((option for option in options if option.startswith(b'restrict=')),
                               b'restrict=off')
            return {b'restrict=off': True, b'restrict=on': False}.get(restriction)
    return None


@export_tool
async def vm_internet_status() -> dict:
    """Report the running VM's outbound NAT setting and the saved setting for its next boot.
    A null current_enabled indicates the VM is stopped or its configuration could not be read.
    """
    state, pid = (await host('docker', 'inspect', '--format', '{{.State.Status}} {{.State.Pid}}', VM)).split()
    current = running_internet_enabled(int(pid)) if state == 'running' else None
    next_boot = (STATE / 'internet-enabled').is_file()
    return {'container': state, 'current_enabled': current, 'next_boot_enabled': next_boot,
            'restart_required': state == 'running' and current != next_boot}


@export_tool
async def vm_set_internet(enabled: bool, apply_now: bool = False,
                          timeout_seconds: TimeoutSeconds = 60) -> dict:
    """Enable or disable outbound IPv4 NAT. Saves the setting for the next start.
    apply_now gracefully restarts a running VM only if needed. A stopped VM stays stopped.
    If restart fails, the saved choice remains pending. File transfer remains available.
    """
    with management_lock():
        info = await vm_internet_status()
        state = info['container']
        if state not in ('running', 'exited', 'created'):
            raise RuntimeError(f'Unsupported current container state: {state}')
        marker = STATE / 'internet-enabled'
        if enabled:
            marker.touch(mode=0o600, exist_ok=True)
        else:
            marker.unlink(missing_ok=True)
        if apply_now and state == 'running' and info['current_enabled'] != enabled:
            await change_power('restart', state, timeout_seconds)
            info = await vm_internet_status()
            if info['current_enabled'] != enabled:
                raise RuntimeError('The VM restarted, but the requested internet setting could not be verified')
            return info
        return dict(info, next_boot_enabled=enabled,
                    restart_required=state == 'running' and info['current_enabled'] != enabled)


def catalog():
    files = {}
    for prefix, directory in [('workspace', PROJECT), ('guest-tools', STATE)]:
        for path in sorted(directory.glob('*.iso')):
            if path.is_file() and not path.is_symlink():
                files[f'{prefix}/{path.name}'] = path
    return files


def next_medium():
    if SELECTION.is_symlink():
        return '/vm-clean/' + os.readlink(SELECTION)
    return '/media/windows11.iso'


@export_tool
async def list_images() -> dict:
    """List selectable installation and tools ISOs with current and next-boot media."""
    result = {'images': [{'id': key, 'bytes': path.stat().st_size}
                         for key, path in catalog().items()], 'next_boot': next_medium()}
    state = await container_state()
    current = (await control('medium'))['path'] if state == 'running' else None
    result['current'] = result['next_boot'] if current == '/vm-clean/selected-install.iso' else current
    return result


@export_tool
async def select_image(image_id: str) -> dict:
    """Mount a listed ISO on the installation CD drive and select it for subsequent starts."""
    with management_lock():
        path = catalog().get(image_id)
        if path is None:
            raise ValueError('Use an image id from list_images')
        state = await container_state()
        if state not in ('running', 'exited', 'created'):
            raise RuntimeError(f'Unsupported current container state: {state}')
        if path.parent == PROJECT:
            directory = STATE / 'media'
            directory.mkdir(mode=0o700, exist_ok=True)
            staged = directory / path.name
            if not staged.exists():
                os.link(path, staged)
            elif not staged.samefile(path):
                raise RuntimeError('Staged ISO differs from the workspace ISO')
            path = staged
        relative = path.relative_to(STATE)
        selected = '/vm-clean/' + str(relative)
        previous = SELECTION.readlink() if SELECTION.is_symlink() else None
        if previous is None and SELECTION.exists():
            raise RuntimeError('Selection path is occupied')
        temp = STATE / ('.selected-' + uuid.uuid4().hex)
        try:
            temp.symlink_to(relative)
            os.replace(temp, SELECTION)
            if state == 'running':
                try:
                    await control('mount', path=selected)
                except BaseException:
                    if previous is None:
                        SELECTION.unlink()
                    else:
                        temp.symlink_to(previous)
                        os.replace(temp, SELECTION)
                    raise
        finally:
            temp.unlink(missing_ok=True)
        return {'image_id':image_id, 'next_boot':selected,
                'current':(await control('medium'))['path'] if state == 'running' else None}


@export_tool
async def console_screen() -> Image:
    """Capture the current guest framebuffer at native resolution as a PNG."""
    return Image(data=await control('screen'), format='png')


@export_tool
async def console_demo_show(x: Coord, y: Coord, text: str,
                            duration_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 5,
                            pointer_x: Coord | None = None, pointer_y: Coord | None = None) -> dict:
    """Show a timed walkthrough in the browser console. x/y anchor the caption's top-left.
    All coordinates use 0..32767. The highlighted pointer follows console mouse input.
    Set both pointer coordinates to point elsewhere without moving the guest mouse.
    Otherwise use the last console pointer or the caption anchor. Empty text shows only the pointer.
    Replaces the previous step. The countdown starts when the server accepts the step.
    """
    if (pointer_x is None) != (pointer_y is None):
        raise ValueError('Supply both pointer coordinates together')
    return await control('demo-show', x=x, y=y, text=text, duration_seconds=duration_seconds,
                         pointer_x=pointer_x, pointer_y=pointer_y)


@export_tool
async def console_demo_clear() -> dict:
    """Immediately dismiss the current walkthrough caption and pointer in the browser console."""
    return await control('demo-clear')


@export_tool
async def console_pointer(x: Coord, y: Coord,
                          action: Literal['move', 'click', 'double_click', 'wheel_up', 'wheel_down']='move',
                          button: Literal['left', 'middle', 'right']='left') -> str:
    """Move the absolute tablet or act at x/y. Both axes use 0..32767 edge-to-edge."""
    async with console_input() as send:
        await send('move', x=x, y=y)
        if action.startswith('wheel'):
            await send('wheel', delta=-1 if action == 'wheel_up' else 1)
        elif action in ('click', 'double_click'):
            number = ('left', 'middle', 'right').index(button)
            for _ in range(2 if action == 'double_click' else 1):
                for down in (True, False):
                    await send('button', x=x, y=y, button=number, down=down)
    return 'Pointer events acknowledged'


@export_tool
async def console_drag(start_x: Coord, start_y: Coord, end_x: Coord, end_y: Coord) -> str:
    """Drag with the left mouse button between two absolute tablet positions (0..32767)."""
    async with console_input() as send:
        await send('button', x=start_x, y=start_y, button=0, down=True)
        for step in range(1, 9):
            await send('move', x=round(start_x + (end_x-start_x)*step/8),
                       y=round(start_y + (end_y-start_y)*step/8))
        await send('button', x=end_x, y=end_y, button=0, down=False)
    return 'Drag acknowledged'


@export_tool
async def console_keys(keys: Annotated[list[str], Field(min_length=1, max_length=8)]) -> str:
    """Press and release a QEMU key chord, e.g. ["ctrl","a"], ["meta_l","r"], ["ret"]."""
    if len(set(keys)) != len(keys) or any(key not in KEYS for key in keys):
        raise ValueError('Use distinct supported QEMU key names')
    async with console_input() as send:
        await chord(send, keys)
    return 'Key events acknowledged'


@export_tool
async def console_text(text: str, method: Literal['type','paste']='type') -> str:
    """Enter Unicode at the Windows focus using the signed-in user's COM1 helper.
    Paste updates the guest clipboard. Limit 64 KiB UTF-8.
    """
    if len(text.encode()) > 65536:
        raise ValueError('Text exceeds 64 KiB')
    async with console_input() as send:
        await send(method, text=text)
    return 'Text acknowledged by Windows'


@export_tool
async def console_copy_selection() -> str:
    """Copy the selected text in the guest and return it via the console bridge."""
    async with console_input() as send:
        return await send('copy')


@export_tool
async def console_clipboard_read() -> dict:
    """Read plain text from the signed-in Windows user's clipboard without sending Ctrl+C.
    Returns an empty string if there is no text. Limit 64 KiB UTF-8.
    """
    return await control('clipboard-read')


@export_tool
async def console_clipboard_write(text: str) -> dict:
    """Replace the signed-in Windows user's clipboard text without pasting it.
    Pass an empty string to clear the clipboard. Limit 64 KiB UTF-8.
    """
    if len(text.encode()) > 65536:
        raise ValueError('Text exceeds 64 KiB')
    return await control('clipboard-write', text=text)


@export_tool
async def console_open_terminal(shell: Literal['powershell', 'cmd']='powershell') -> str:
    """Open a visible guest shell via Windows Run. Check console_screen for readiness."""
    async with console_input() as send:
        await chord(send, ['meta_l', 'r'])
        await asyncio.sleep(0.5)
        await send('type', text=shell + '.exe')
        await chord(send, ['ret'])
    return 'Terminal launch submitted'


@export_tool
async def console_cli(line: str) -> str:
    """Type one command and press Enter in the focused guest terminal.
    Inspect console_screen for output, or select output and call console_copy_selection.
    """
    if not line or any(ord(c) < 32 for c in line) or len(line.encode()) > 65536:
        raise ValueError('Use one nonempty command line of at most 64 KiB')
    async with console_input() as send:
        await send('type', text=line)
        await chord(send, ['ret'])
    return 'Command submitted to the focused guest terminal'


@export_tool
async def console_settings() -> dict:
    """Read the configured port, live port, service state and SSH forwarding arguments."""
    port = configured_port()
    active = await host('systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '--value')
    try:
        live = (await control('health'))['port']
    except (aiohttp.ClientError, OSError, RuntimeError, TimeoutError):
        live = None
    return {'host':'127.0.0.1', 'configured_port':port, 'live_port':live, 'service':active,
            'url':f'http://127.0.0.1:{port}/', 'ssh_forward':f'-L {port}:127.0.0.1:{port}'}


async def restart_console(port):
    await host('systemctl', '--user', 'restart', UNIT, timeout=100)
    async with asyncio.timeout(15):
        while True:
            try:
                if (await control('health'))['port'] == port:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(f'http://127.0.0.1:{port}/') as response:
                            response.raise_for_status()
                            return
            except (aiohttp.ClientError, OSError, RuntimeError):
                pass
            await asyncio.sleep(.2)


@export_tool
async def configure_console(port: Annotated[int, Field(ge=1024, le=65535)]) -> dict:
    """Set the loopback HTTP port and apply it. Browser clients reconnect after a change."""
    validate_port(port)
    with management_lock():
        old = configured_port()
        info = await console_settings()
        if info['live_port'] == port and old == port:
            return info
        if info['live_port'] != port:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
        store_port(port)
        try:
            await restart_console(port)
        except BaseException:
            store_port(old)
            await restart_console(old)
            raise
        return await console_settings()


async def prepare_file_server():
    with management_lock():
        state = await container_state()
        if state not in ('running', 'created', 'exited'):
            raise RuntimeError(f'Cannot prepare transfer in VM state: {state}')
        return await control('transfer-server', attach=state == 'running')


@export_tool
async def file_begin_download(filename: str, size: Annotated[int, Field(ge=0)]) -> dict:
    """Begin MCP client -> VM transfer. Supply exact file size in bytes, then write chunks."""
    token = await asyncio.to_thread(transfers.create, 'to_vm', filename, size)
    return transfers.describe(token)


@export_tool
async def file_write_chunk(transfer_id: str, offset: Annotated[int, Field(ge=0)],
                           data_base64: str) -> dict:
    """Append up to max_chunk raw bytes encoded as base64. Identical retries are accepted."""
    return await asyncio.to_thread(transfers.write_chunk, transfer_id, offset, data_base64)


@export_tool
async def file_publish(transfer_id: str) -> dict:
    """Finish the file, start the guest HTTP server, and return its passwordless download URL."""
    await asyncio.to_thread(transfers.publish, transfer_id)
    network = await prepare_file_server()
    return dict(transfers.describe(transfer_id), **network)


@export_tool
async def file_request_upload() -> dict:
    """Issue a passwordless URL for selecting and uploading one file in the VM browser."""
    network = await prepare_file_server()
    token = await asyncio.to_thread(transfers.create, 'from_vm')
    return dict(transfers.describe(token), **network)


@export_tool
async def file_status(transfer_id: str) -> dict:
    """Get progress and completion; ready VM uploads can be read by offset."""
    return await asyncio.to_thread(transfers.describe, transfer_id)


@export_tool
async def file_read_chunk(transfer_id: str, offset: Annotated[int, Field(ge=0)],
                          length: Annotated[int, Field(gt=0, le=transfers.CHUNK)] = 49152) -> dict:
    """Read uploaded bytes as base64. Continue from next_offset until eof."""
    return await asyncio.to_thread(transfers.read_chunk, transfer_id, offset, length)


@export_tool
async def file_discard(transfer_id: str) -> dict:
    """Revoke this URL and remove its staged bytes from the console host."""
    await asyncio.to_thread(transfers.discard, transfer_id)
    return {'removed':True}


def create_session():
    mcp = FastMCP('Windows VM Console')
    for tool in tools:
        mcp.add_tool(tool)
    return install_live_stream(mcp, connection)


if __name__ == '__main__':
    asyncio.run(create_session()())
