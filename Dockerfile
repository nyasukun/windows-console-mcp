FROM ubuntu:26.04
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        dnsmasq-base iproute2 iptables ovmf python3 qemu-system-x86 qemu-system-modules-spice qemu-utils \
        seabios spice-html5 swtpm \
    && rm -rf /var/lib/apt/lists/*

COPY console/network/ /opt/windows-network/
