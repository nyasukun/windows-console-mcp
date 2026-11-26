"""Durable transfer slots shared by the stdio MCP processes and guest HTTP server."""
import base64
import binascii
import fcntl
import json
import os
import re
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from control_config import STATE

STORE = STATE / 'transfers'
GUEST_HOST = '10.77.0.4'
CHUNK = 512 * 1024


def filename(value):
    if not isinstance(value, str) or value != value.split('/')[-1] or value != value.split('\\')[-1]:
        raise ValueError('Use a filename without directories')
    if not value.strip() or value in ('.', '..') or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid filename')
    return value


def directory(token, *, store=None):
    if not re.fullmatch('[0-9a-f]{48}', token):
        raise ValueError('Unknown transfer')
    return (STORE if store is None else store) / token


def create(direction, name=None, size=None, *, store=None):
    if direction == 'to_vm':
        filename(name)
        if type(size) is not int or size < 0:
            raise ValueError('size must be nonnegative')
    store = STORE if store is None else store
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_hex(24)
    slot = directory(token, store=store)
    slot.mkdir(mode=0o700)
    try:
        (slot / 'lock').touch(mode=0o600)
        (slot / 'partial').touch(mode=0o600)
        (slot / 'meta').write_text(json.dumps(dict(direction=direction, name=name, size=size)))
    except BaseException:
        shutil.rmtree(slot)
        raise
    return token


@contextmanager
def locked(token, exclusive=False, *, store=None):
    slot = directory(token, store=store)
    try:
        lock = (slot / 'lock').open('rb')
    except FileNotFoundError as exc:
        raise ValueError('Unknown transfer') from exc
    with lock:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock, mode | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Transfer busy; retry after this operation') from exc
        if not (slot / 'meta').exists():
            raise ValueError('Unknown transfer')
        yield slot, json.loads((slot / 'meta').read_text())


def describe(token, *, store=None, host=GUEST_HOST):
    slot = directory(token, store=store)
    # Polling a multi-hour browser upload must not wait for its writer lock.
    # Publishing renames partial to payload atomically; retry that one race.
    for _ in range(2):
        ready = (slot / 'payload').is_file()
        try:
            current = (slot / ('payload' if ready else 'partial')).stat().st_size
            meta = json.loads((slot / 'meta').read_text())
            break
        except FileNotFoundError:
            continue
    else:
        raise ValueError('Unknown transfer')
    result = dict(transfer_id=token, **meta, ready=ready, bytes=current, max_chunk=CHUNK)
    if ready or meta['direction'] == 'from_vm':
        result['url'] = f'http://{host}/f/{token}'
    return result


def write_chunk(token, offset, encoded, *, store=None):
    if len(encoded) > 4 * ((CHUNK + 2) // 3):
        raise ValueError('Chunk too large')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('Invalid base64') from exc
    if not raw or len(raw) > CHUNK or offset < 0:
        raise ValueError('Invalid chunk size or offset')
    with locked(token, True, store=store) as (slot, meta):
        if meta['direction'] != 'to_vm' or (slot / 'payload').exists():
            raise ValueError('Transfer is not accepting MCP writes')
        if offset + len(raw) > meta['size']:
            raise ValueError('Chunk exceeds declared file size')
        with (slot / 'partial').open('r+b', buffering=0) as file:
            size = file.seek(0, os.SEEK_END)
            if offset < size:
                file.seek(offset)
                if file.read(len(raw)) != raw:
                    raise ValueError(f'Conflicting chunk; next offset is {size}')
            elif offset == size:
                try:
                    if file.write(raw) != len(raw):
                        raise OSError('Incomplete disk write')
                except OSError:
                    file.truncate(size)
                    raise
                size += len(raw)
            else:
                raise ValueError(f'Missing bytes; next offset is {size}')
            return {'next_offset':size}


def publish(token, *, store=None):
    with locked(token, True, store=store) as (slot, meta):
        if meta['direction'] != 'to_vm':
            raise ValueError('Expected a download transfer')
        if not (slot / 'payload').exists():
            partial = slot / 'partial'
            if partial.stat().st_size != meta['size']:
                raise ValueError('More file bytes are required')
            partial.replace(slot / 'payload')


def read_chunk(token, offset, length, *, store=None):
    if not 0 < length <= CHUNK or offset < 0:
        raise ValueError('Invalid chunk size or offset')
    with locked(token, store=store) as (slot, meta):
        if meta['direction'] != 'from_vm' or not (slot / 'payload').is_file():
            raise ValueError('No completed VM upload')
        with (slot / 'payload').open('rb') as file:
            size = os.fstat(file.fileno()).st_size
            if offset > size:
                raise ValueError('Offset past end of file')
            file.seek(offset)
            raw = file.read(length)
            end = offset + len(raw)
            return dict(data_base64=base64.b64encode(raw).decode('ascii'), next_offset=end, eof=end==size)


def finish_upload(slot, meta, name, size):
    with tempfile.NamedTemporaryFile('w', dir=slot, delete=False) as out:
        try:
            json.dump(dict(meta, name=name, size=size), out)
            out.flush()
            os.replace(out.name, slot / 'meta')
        finally:
            Path(out.name).unlink(missing_ok=True)
    (slot / 'partial').replace(slot / 'payload')


def discard(token, *, store=None):
    with locked(token, True, store=store) as (slot, _):
        shutil.rmtree(slot)
