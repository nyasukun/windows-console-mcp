#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /vm-clean

if [[ ! -f disk.qcow2 ]]; then
  qemu-img create -f qcow2 disk.qcow2 "${WINDOWS_DISK_GIB:-80}G"
fi
if [[ ! -f OVMF_VARS.fd ]]; then
  cp /usr/share/OVMF/OVMF_VARS_4M.ms.fd OVMF_VARS.fd
fi

mkdir -p tpm
rm -f qmp.sock tpm.sock
swtpm socket --tpm2 --tpmstate dir=tpm \
  --ctrl type=unixio,path=tpm.sock --daemon

spice_options=()
if [[ ${WINDOWS_SPICE:-0} == 1 ]]; then
  mkdir -p -m 700 spice
  rm -f spice/console.sock
  spice_options=(-spice unix=on,addr=/vm-clean/spice/console.sock,disable-ticketing=on)
fi

install_iso=/media/windows11.iso
if [[ -L selected-install.iso ]]; then
  install_iso=$(readlink -e /vm-clean/selected-install.iso)
  # The -drive option uses doubled commas for a literal comma in the path.
  install_iso=${install_iso//,/,,}
fi

# The state directory records whether this VM is allowed outbound networking.
network_restrict=on
if [[ -f internet-enabled ]]; then
  network_restrict=off
fi

setup_media=()
if [[ -f setup/console-tools.iso ]]; then
  setup_media=(
    -drive if=none,id=consoletools,media=cdrom,format=raw,readonly=on,file=/vm-clean/setup/console-tools.iso
    -device ide-cd,drive=consoletools,bus=ide.2
  )
fi

transfer_net=10.77.0.0/24
transfer_host=10.77.0.4
if [[ ${WINDOWS_MANAGED:-0} == 1 ]]; then
  transfer_net=${WINDOWS_TRANSFER_NET:-$transfer_net}
  transfer_host=${WINDOWS_TRANSFER_HOST:-$transfer_host}
  network_restrict=on
  if [[ ${WINDOWS_INTERNET:-0} == 1 && -z ${WINDOWS_TAP:-} ]]; then
    network_restrict=off
  fi
fi

network=(
  -netdev "user,id=transfernet,net=$transfer_net,ipv6=off,restrict=$network_restrict,guestfwd=tcp:$transfer_host:80-cmd:/usr/bin/python3 /vm-clean/transfer-relay.py"
  -device e1000e,id=transfernic,netdev=transfernet,mac=52:54:00:77:00:04
)
display=0.0.0.0:12
monitor=()
if [[ ${WINDOWS_MANAGED:-0} == 1 ]]; then
  if [[ -n ${WINDOWS_TAP:-} ]]; then
    network+=(
      -netdev "tap,id=lan,ifname=$WINDOWS_TAP,script=no,downscript=no"
      -device "e1000e,netdev=lan,mac=$WINDOWS_MAC"
    )
  fi
  rm -f vnc.sock manage.sock
  display=unix:/vm-clean/vnc.sock
  monitor=(-qmp unix:manage.sock,server=on,wait=off)
fi

exec qemu-system-x86_64 \
  -name 'Windows 11 clean setup' -machine q35,smm=on,accel=kvm -cpu host \
  -smp "${WINDOWS_VCPUS:-4}" -m "${WINDOWS_RAM_MIB:-8192}M" \
  -global driver=cfi.pflash01,property=secure,value=on \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd \
  -drive if=pflash,format=raw,file=OVMF_VARS.fd \
  -chardev socket,id=chrtpm,path=tpm.sock \
  -tpmdev emulator,id=tpm0,chardev=chrtpm -device tpm-tis,tpmdev=tpm0 \
  -drive if=none,id=osdisk,format=qcow2,file=disk.qcow2 \
  -device ide-hd,drive=osdisk,bus=ide.0,bootindex=1 \
  -drive if=none,id=install,media=cdrom,format=raw,readonly=on,file="$install_iso" \
  -device ide-cd,drive=install,bus=ide.1,bootindex=2 \
  "${setup_media[@]}" \
  -chardev socket,id=consoletext,path=/vm-clean/console-text.sock,server=on,wait=off \
  -serial chardev:consoletext \
  -boot menu=on -device qemu-xhci \
  -device usb-ehci,id=ehci -device usb-tablet,bus=ehci.0 \
  "${network[@]}" \
  "${spice_options[@]}" \
  -object secret,id=vncpass,file=/vm-clean/vnc-password \
  -display none -vnc "$display,password-secret=vncpass" -qmp unix:qmp.sock,server=on,wait=off "${monitor[@]}"
