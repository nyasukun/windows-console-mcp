"""Build the topology inside the router container's network namespace."""
import ipaddress
import json
import os
import subprocess


def run(*args):
    subprocess.run(args, check=True)


def main():
    config = json.loads(os.environ['WINDOWS_TOPOLOGY'])
    networks = [ipaddress.IPv4Network(cidr) for cidr in config['segments']]
    interfaces = [f'lan{i}' for i in range(len(networks))]
    uplinks = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show']))
    for link in uplinks:
        for addr in link['addr_info']:
            prefix = ipaddress.IPv4Interface(f"{addr['local']}/{addr['prefixlen']}").network
            if any(prefix.overlaps(network) for network in networks):
                raise ValueError(f'Segment overlaps router interface {link["ifname"]}: {prefix}')

    dns = ['dnsmasq', '--keep-in-foreground', '--log-facility=-', '--bind-dynamic', '--no-resolv',
           '--except-interface=lo', '--dhcp-authoritative', '--dhcp-broadcast', '--dhcp-leasefile=/tmp/dnsmasq.leases']
    if config['proxy']:
        dns += ['--server=1.1.1.1', '--server=9.9.9.9']
    else:
        dns += ['--port=0', '--dhcp-option=option:dns-server']

    run('iptables', '-P', 'FORWARD', 'DROP')
    for interface, network in zip(interfaces, networks):
        gateway = network[1]
        run('ip', 'link', 'add', interface, 'type', 'bridge')
        run('ip', 'addr', 'add', f'{gateway}/{network.prefixlen}', 'broadcast', '+', 'dev', interface)
        run('ip', 'link', 'set', interface, 'up')
        dns += [f'--interface={interface}',
                f'--dhcp-range={network[2]},{network[-2]},{network.netmask},12h']
        run('iptables', '-A', 'FORWARD', '-i', interface, '-s', str(network), '-o', 'lan+', '-j', 'ACCEPT')
        run('iptables', '-A', 'INPUT', '-i', interface, '-p', 'udp', '--dport', '67', '-j', 'ACCEPT')
        if config['proxy']:
            for protocol in ('tcp', 'udp'):
                run('iptables', '-A', 'INPUT', '-i', interface, '-d', str(gateway),
                    '-p', protocol, '--dport', '53', '-j', 'ACCEPT')
            run('iptables', '-A', 'INPUT', '-i', interface, '-p', 'tcp', '--dport', '15001',
                '-m', 'conntrack', '--ctstate', 'DNAT', '-j', 'ACCEPT')
            # Keep traffic between the configured segments on the router.
            for local in networks:
                run('iptables', '-t', 'nat', '-A', 'PREROUTING', '-i', interface,
                    '-d', str(local), '-j', 'ACCEPT')
            run('iptables', '-t', 'nat', '-A', 'PREROUTING', '-i', interface, '-s', str(network),
                '-p', 'tcp', '-j', 'REDIRECT', '--to-ports', '15001')
        run('iptables', '-A', 'INPUT', '-i', interface, '-j', 'DROP')
    if config['proxy']:
        run('iptables', '-A', 'INPUT', '!', '-i', 'lo', '-p', 'tcp', '--dport', '15001', '-j', 'DROP')
    os.execvp(dns[0], dns)


if __name__ == '__main__':
    main()
