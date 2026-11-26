#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")"
app_data="${XDG_DATA_HOME:-$HOME/.local/share}/windows-console"
umask 077
mkdir -p "$app_data" ../state-clean

if [[ -z ${CONSOLE_SPICE_SRC:-} ]]; then
  image=${WINDOWS_MANAGER_IMAGE:-windows-console-mcp:local}
  if ! image_id=$(docker image inspect --format '{{.Id}}' "$image"); then
    echo "Build the Windows console image first: docker build -t $image ." >&2
    exit 1
  fi
  CONSOLE_SPICE_SRC="$app_data/spice-${image_id#sha256:}"
  (
    flock -x 9
    if [[ ! -r "$CONSOLE_SPICE_SRC/main.js" ]]; then
      stage=$(mktemp -d "$app_data/.spice.XXXXXXXX")
      container=
      cleanup() {
        if [[ -n $container ]]; then docker rm "$container" >/dev/null || true; fi
        rm -rf -- "$stage"
      }
      trap cleanup EXIT
      container=$(docker create "$image_id" /bin/true)
      docker cp "$container:/usr/share/spice-html5/src" "$stage/src"
      test -r "$stage/src/main.js"
      mv --no-target-directory "$stage/src" "$CONSOLE_SPICE_SRC"
    fi
  ) 9>"$app_data/spice.lock"
fi
if [[ ! -r "$CONSOLE_SPICE_SRC/main.js" ]]; then
  echo 'CONSOLE_SPICE_SRC must point to the spice-html5 src directory.' >&2
  exit 1
fi
export CONSOLE_SPICE_SRC
uv_exec=$(command -v uv || true)
exec "${uv_exec:-$HOME/.local/bin/uv}" run --locked --project mcp python server.py
