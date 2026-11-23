#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 1 || ! -f "$1" || ! -r "$1" ]]; then
  echo "Usage: $0 /path/to/windows-x64.iso" >&2
  exit 2
fi
iso=$(realpath -- "$1")
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
vm_name=${WINDOWS_CONSOLE_VM:-windows11-vm-clean}
rfb_port=${WINDOWS_CONSOLE_RFB_PORT:-5912}
if [[ ! $rfb_port =~ ^[0-9]{1,5}$ ]] || (( 10#$rfb_port < 1024 || 10#$rfb_port > 65535 )); then
  echo "WINDOWS_CONSOLE_RFB_PORT must be between 1024 and 65535" >&2
  exit 2
fi
if docker container inspect "$vm_name" >/dev/null 2>&1; then
  echo "$vm_name already exists. Use the vm_power MCP tool to start it." >&2
  exit 1
fi
mkdir -p -m 700 state-clean
(
  flock -x 9
  if [[ ! -e state-clean/vnc-password ]]; then
    # QEMU's VNC protocol uses eight password bytes. Keep the file on the host.
    secret=$(mktemp state-clean/.vnc-password.XXXXXXXX)
    trap 'rm -f -- "$secret"' EXIT
    python3 -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(6))' > "$secret"
    mv -- "$secret" state-clean/vnc-password
  fi
) 9>state-clean/console-management.lock
exec docker run -d --name "$vm_name" \
  --user "$(id -u):$(id -g)" --group-add "$(stat -c '%g' /dev/kvm)" \
  --device /dev/kvm --cap-drop ALL --security-opt no-new-privileges \
  -e WINDOWS_SPICE=1 -p "127.0.0.1:$rfb_port:5912" \
  -v "$PWD/state-clean:/vm-clean" -v "$iso:/media/windows11.iso:ro" \
  -v "$PWD/run-vm-clean.sh:/run-vm-clean.sh:ro" \
  windows-console-mcp:local /bin/bash /run-vm-clean.sh
