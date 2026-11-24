#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /vm-clean

if [[ ! -f disk.qcow2 ]]; then
  qemu-img create -f qcow2 disk.qcow2 80G
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

exec qemu-system-x86_64 \
  -name 'Windows 11 clean setup' -machine q35,smm=on,accel=kvm -cpu host \
  -smp 4 -m 8G \
  -global driver=cfi.pflash01,property=secure,value=on \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd \
  -drive if=pflash,format=raw,file=OVMF_VARS.fd \
  -chardev socket,id=chrtpm,path=tpm.sock \
  -tpmdev emulator,id=tpm0,chardev=chrtpm -device tpm-tis,tpmdev=tpm0 \
  -drive if=none,id=osdisk,format=qcow2,file=disk.qcow2 \
  -device ide-hd,drive=osdisk,bus=ide.0,bootindex=1 \
  -drive if=none,id=install,media=cdrom,format=raw,readonly=on,file="$install_iso" \
  -device ide-cd,drive=install,bus=ide.1,bootindex=2 \
  -chardev socket,id=consoletext,path=/vm-clean/console-text.sock,server=on,wait=off \
  -serial chardev:consoletext \
  -boot menu=on -device qemu-xhci \
  -device usb-ehci,id=ehci -device usb-tablet,bus=ehci.0 \
  -netdev "user,id=transfernet,net=10.77.0.0/24,ipv6=off,restrict=$network_restrict,guestfwd=tcp:10.77.0.4:80-cmd:/usr/bin/python3 /vm-clean/transfer-relay.py" \
  -device e1000e,id=transfernic,netdev=transfernet,mac=52:54:00:77:00:04 \
  "${spice_options[@]}" \
  -object secret,id=vncpass,file=/vm-clean/vnc-password \
  -display none -vnc 0.0.0.0:12,password-secret=vncpass -qmp unix:qmp.sock,server=on,wait=off
