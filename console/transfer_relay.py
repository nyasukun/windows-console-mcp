"""QEMU guestfwd child: bridge one guest TCP stream to the transfer-only socket."""
import os
import socket
import threading


def forward_request(peer):
    try:
        while block := os.read(0, 65536):
            peer.sendall(block)
    except OSError:
        pass
    finally:
        try:
            peer.shutdown(socket.SHUT_WR)
        except OSError:
            pass


if __name__ == '__main__':
    with socket.socket(socket.AF_UNIX) as peer:
        peer.connect('/vm-clean/transfer-http.sock')
        threading.Thread(target=forward_request, args=(peer,), daemon=True).start()
        while block := peer.recv(65536):
            remaining = memoryview(block)
            while remaining:
                remaining = remaining[os.write(1, remaining):]
