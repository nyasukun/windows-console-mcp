"""Package the project-owned setup answer file and guest helper."""
import os
import tempfile
from pathlib import Path

import pycdlib

from control_config import ROOT, STATE


def build_setup_iso(state):
    directory = state / 'setup'
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / 'console-tools.iso'
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident='CONSOLE_TOOLS')
    try:
        for name in ('Autounattend.xml', 'bridge.ps1', 'install.cmd', 'stop-old.ps1'):
            iso_name = name.upper().replace('-', '_') + ';1'
            iso.add_file(str(ROOT / 'guest' / name), iso_path='/' + iso_name, joliet_path='/' + name)
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as out:
            temp = Path(out.name)
            try:
                iso.write_fp(out)
                out.flush()
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
    finally:
        iso.close()
    return target


if __name__ == '__main__':
    build_setup_iso(STATE)
