"""Guest-only HTTP routes and restricted QEMU network for the transfer server."""
import asyncio
import errno
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

from aiohttp import MultipartReader, web

import transfers
from control_config import ROOT, STATE

FORM = """<!doctype html><html lang="ja"><meta charset="utf-8"><title>ファイル転送</title>
<body><main><h1>VM からファイルを送信</h1>
<form method="post" enctype="multipart/form-data">
<label>ファイルを選択 <input type="file" name="file" required></label>
<button>アップロード</button></form></main></body></html>"""


def page(text):
    return web.Response(text=text, content_type='text/html', charset='utf-8',
                        headers={'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer',
                                 'Content-Security-Policy':"default-src 'none'; form-action 'self'; frame-ancestors 'none'"})


async def guest_file(request):
    token = request.match_info['token']
    if request.host not in (transfers.GUEST_HOST, transfers.GUEST_HOST + ':80'):
        raise web.HTTPNotFound()
    writing = request.method == 'POST'
    try:
        with transfers.locked(token, exclusive=writing) as (slot, meta):
            if meta['direction'] == 'to_vm':
                if writing:
                    raise web.HTTPMethodNotAllowed('POST', ['GET', 'HEAD'])
                if not (slot / 'payload').is_file():
                    raise web.HTTPNotFound()
                disposition = "attachment; filename*=UTF-8''" + quote(meta['name'], safe='')
                return web.FileResponse(slot / 'payload', headers={
                    'Content-Type':'application/octet-stream', 'Content-Disposition':disposition,
                    'Cache-Control':'private, no-store', 'X-Content-Type-Options':'nosniff'})
            if (slot / 'payload').exists():
                if writing:
                    raise web.HTTPConflict(text='この URL へのアップロードは完了しています')
                return page('<meta charset="utf-8"><p>アップロードが完了しました。</p>')
            if not writing:
                return page(FORM)
            if request.content_type != 'multipart/form-data':
                raise web.HTTPUnsupportedMediaType()
            partial = slot / 'partial'
            try:
                reader = await request.multipart()
                part = await reader.next()
                if part is None or isinstance(part, MultipartReader) or part.name != 'file' or not part.filename:
                    raise web.HTTPBadRequest(text='ファイルを1つ選択してください')
                name = transfers.filename(part.filename)
                size = 0
                with partial.open('wb') as out:
                    while block := await part.read_chunk(256 * 1024):
                        await asyncio.to_thread(out.write, block)
                        size += len(block)
                    if await reader.next() is not None:
                        raise web.HTTPBadRequest(text='1 URL につき1ファイルです')
                transfers.finish_upload(slot, meta, name, size)
            finally:
                if partial.exists():
                    with partial.open('wb'):
                        pass
            return page('<meta charset="utf-8"><p>アップロードが完了しました。</p>')
    except ValueError as exc:
        if str(exc) == 'Unknown transfer':
            raise web.HTTPNotFound() from exc
        raise web.HTTPBadRequest(text='無効なファイルまたはリクエストです') from exc
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG):
            return web.Response(status=507, text='ホストの空き容量またはファイルシステムの上限に達しました')
        raise
    except RuntimeError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc


class GuestServer:
    def __init__(self):
        self.runner = None
        self.lock = asyncio.Lock()

    async def listen(self):
        if self.runner:
            return
        guest = web.Application(client_max_size=0)
        guest.router.add_get('/f/{token}', guest_file)
        guest.router.add_post('/f/{token}', guest_file)
        runner = web.AppRunner(guest, access_log=None)
        await runner.setup()
        path = STATE / 'transfer-http.sock'
        try:
            await web.UnixSite(runner, str(path)).start()
            path.chmod(0o600)
        except BaseException:
            await runner.cleanup()
            raise
        self.runner = runner

    async def ensure(self, monitor):
        async with self.lock:
            await self.listen()
            # The relay must be visible in the QEMU container for guestfwd connections. Replace atomically across upgrades.
            relay = STATE / 'transfer-relay.py'
            source = (ROOT / 'transfer_relay.py').read_bytes()
            if not relay.exists() or relay.read_bytes() != source:
                with tempfile.NamedTemporaryFile(dir=STATE, delete=False) as out:
                    try:
                        out.write(source)
                        out.flush()
                        os.replace(out.name, relay)
                    finally:
                        Path(out.name).unlink(missing_ok=True)
            if monitor is None:
                return {'guest_network':'on_next_start'}
            devices = await monitor.command('qom-list', {'path':'/machine/peripheral'})
            if any(d['name'] == 'transfernic' and d['type'] == 'child<e1000e>' for d in devices):
                return {'guest_network':'attached'}
            return {'guest_network':'requires_cold_start',
                    'note':'Transfer staged. Shut down Windows normally, then start the VM to attach its Intel NIC.'}

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
