"""Offline maintenance commands for this checkout's VM inventory."""
import argparse

from vm_manager import VMManager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['stop-network-containers'])
    parser.parse_args()
    VMManager().stop_network_containers()
    print('Network containers will be rebuilt when a connected VM starts.')


if __name__ == '__main__':
    main()
