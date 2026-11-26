#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")"
app_data="${XDG_DATA_HOME:-$HOME/.local/share}/windows-console"
umask 077
mkdir -p "$app_data" ../state-clean

if [[ -z ${CONSOLE_SPICE_SRC:-} ]]; then
  if [[ -r /usr/share/spice-html5/src/main.js ]]; then
    CONSOLE_SPICE_SRC=/usr/share/spice-html5/src
  else
    CONSOLE_SPICE_SRC="$app_data/spice-src"
    if [[ ! -r "$CONSOLE_SPICE_SRC/main.js" ]]; then
      stage=$(mktemp -d "$app_data/.spice.XXXXXXXX")
      trap 'rm -rf -- "$stage"' EXIT
      for container in "${WINDOWS_CONSOLE_VM:-windows11-vm-clean}" windows11-spice-web; do
        if docker cp "$container:/usr/share/spice-html5/src" "$stage/src" && [[ -r "$stage/src/main.js" ]]; then
          mkdir -p "$CONSOLE_SPICE_SRC"
          cp -R "$stage/src/." "$CONSOLE_SPICE_SRC/"
          break
        fi
      done
      if [[ ! -r "$CONSOLE_SPICE_SRC/main.js" ]]; then
        rm -rf -- "$stage/src"
        if assets=$(docker create "${WINDOWS_MANAGER_IMAGE:-windows-console-mcp:local}" /bin/true); then
          if docker cp "$assets:/usr/share/spice-html5/src" "$stage/src" &&
             [[ -r "$stage/src/main.js" ]]; then
            mkdir -p "$CONSOLE_SPICE_SRC"
            cp -R "$stage/src/." "$CONSOLE_SPICE_SRC/"
          fi
          docker rm "$assets" >/dev/null
        fi
      fi
      rm -rf -- "$stage"
    fi
  fi
fi
if [[ ! -r "$CONSOLE_SPICE_SRC/main.js" ]]; then
  echo 'Set CONSOLE_SPICE_SRC to the spice-html5 src directory or build the Windows console Docker image first.' >&2
  exit 1
fi
export CONSOLE_SPICE_SRC
uv_exec=$(command -v uv || true)
exec "${uv_exec:-$HOME/.local/bin/uv}" run --locked --project mcp python server.py
