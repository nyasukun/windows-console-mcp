"""DHCP guests and statically addressed containers share a segment."""


def reserved_addresses(network):
    count = min(32, network.num_addresses // 2 - 1)
    return tuple(network[-offset] for offset in range(2, count + 2))


def dhcp_end(network):
    return reserved_addresses(network)[-1] - 1
