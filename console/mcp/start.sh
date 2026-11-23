#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
uv_exec=$(command -v uv || true)
exec "${uv_exec:-$HOME/.local/bin/uv}" run --locked --project . python service.py
