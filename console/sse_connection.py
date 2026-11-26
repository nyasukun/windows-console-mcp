"""Publish the active SSE endpoint to the local management console."""
import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from control_config import SERVICE_STATE


@contextmanager
def publish_sse_connection(url, token_file, token):
    SERVICE_STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (SERVICE_STATE / 'sse-connection.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Separate SSE instances may use distinct ports. Show the first one.
            yield
            return
        destination = SERVICE_STATE / 'sse-connection.json'
        with tempfile.NamedTemporaryFile('w', dir=SERVICE_STATE, delete=False) as file:
            try:
                json.dump({'url': url, 'pid': os.getpid(), 'token_file': str(token_file.resolve()),
                           'token_hash': hashlib.sha256(token).hexdigest()}, file)
                file.flush()
                os.replace(file.name, destination)
            finally:
                Path(file.name).unlink(missing_ok=True)
        try:
            yield
        finally:
            destination.unlink(missing_ok=True)


def sse_connection(reveal=False):
    try:
        connection = json.loads((SERVICE_STATE / 'sse-connection.json').read_text())
        if type(connection['pid']) is not int or connection['pid'] <= 0:
            return None
        os.kill(connection['pid'], 0)
        result = {'url': connection['url']}
        if reveal:
            token = Path(connection['token_file']).read_bytes().strip()
            if hashlib.sha256(token).hexdigest() != connection['token_hash']:
                raise ValueError('トークンが変更されています。SSEサービスを再起動してください。')
            result['token'] = token.decode('ascii')
        return result
    except (OSError, KeyError, json.JSONDecodeError):
        return None
