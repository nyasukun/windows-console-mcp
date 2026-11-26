import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT.parent / 'state-clean'
SERVICE_STATE = Path(os.environ.get('WINDOWS_CONSOLE_SERVICE_STATE', ROOT.parent / 'state-console'))
CONTROL = SERVICE_STATE / 'console-admin.sock'
CONFIG = SERVICE_STATE / 'console-server.json'
LEGACY_CONTROL = STATE / 'console-admin.sock'
LEGACY_CONFIG = STATE / 'console-server.json'
DEFAULT_PORT = 6083


@contextmanager
def legacy_lock(state):
    with (state / 'console-management.lock').open('a') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another management operation is in progress') from exc
        yield


def validate_port(value):
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError('port must be between 1024 and 65535')
    return value


def configured_port():
    for path in (CONFIG, LEGACY_CONFIG):
        try:
            return validate_port(json.loads(path.read_text())['port'])
        except FileNotFoundError:
            continue
    return DEFAULT_PORT


def store_port(port):
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=CONFIG.parent, delete=False) as out:
        try:
            json.dump({'port': validate_port(port)}, out)
            out.flush()
            os.fsync(out.fileno())
            os.replace(out.name, CONFIG)
        finally:
            Path(out.name).unlink(missing_ok=True)


KEYS = set('abcdefghijklmnopqrstuvwxyz0123456789')
KEYS.update('esc tab ret backspace spc shift shift_r ctrl ctrl_r alt alt_r meta_l meta_r caps_lock minus equal bracket_left bracket_right backslash semicolon apostrophe grave comma dot slash left right up down home end pgup pgdn delete insert menu print_screen scroll_lock pause num_lock kp_enter kp_add kp_subtract kp_multiply kp_divide kp_decimal henkan muhenkan katakanahiragana yen ro'.split())
KEYS.update(f'f{i}' for i in range(1, 13))
KEYS.update(f'kp_{i}' for i in range(10))
