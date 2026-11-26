"""Authenticated HTTPS SSE transport for a single IPv4 LAN subnet."""
import argparse
from hmac import compare_digest
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path

import uvicorn
from mcp.server.sse import SseServerTransport
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

from service import create_session
from sse_connection import publish_sse_connection


def application(address, port, token):
    transport = SseServerTransport('/messages/')
    authority = f'{address.ip}:{port}'
    lan = address.network
    authorization = b'Bearer ' + token

    async def app(scope, receive, send):
        if scope['type'] != 'http':
            return
        headers = Headers(scope=scope)
        host = headers.get('host')
        origin = headers.get('origin')
        if (IPv4Address(scope['client'][0]) not in lan or host != authority
                or origin not in (None, f'https://{authority}')):
            response = PlainTextResponse('Forbidden', status_code=403)
        elif not compare_digest(headers.get('authorization', '').encode('latin-1'), authorization):
            response = PlainTextResponse('Unauthorized', status_code=401,
                                         headers={'WWW-Authenticate': 'Bearer'})
        elif scope['path'] == '/sse' and scope['method'] == 'GET':
            async with transport.connect_sse(scope, receive, send) as streams:
                await create_session()(*streams)
            return
        elif scope['path'] == '/messages/' and scope['method'] == 'POST':
            await transport.handle_post_message(scope, receive, send)
            return
        else:
            response = PlainTextResponse('Not found', status_code=404)
        await response(scope, receive, send)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', required=True, type=IPv4Interface, help='IPv4 address and LAN prefix')
    parser.add_argument('--port', type=int, default=6084)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--cert', required=True)
    parser.add_argument('--key', required=True)
    args = parser.parse_args()
    private_ranges = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8')
    if not any(args.bind.network.subnet_of(IPv4Network(block)) for block in private_ranges):
        parser.error('--bind must be inside a private IPv4 or loopback subnet')
    token = args.token_file.read_bytes().strip()
    if len(token) < 32 or not token.isascii() or any(byte <= 32 or byte >= 127 for byte in token):
        parser.error('Use a random ASCII bearer token of at least 32 characters')
    url = f'https://{args.bind.ip}:{args.port}/sse'
    with publish_sse_connection(url, args.token_file, token):
        uvicorn.run(application(args.bind, args.port, token), host=str(args.bind.ip), port=args.port,
                    ssl_certfile=args.cert, ssl_keyfile=args.key, proxy_headers=False,
                    access_log=False, lifespan='off', ws='none', timeout_graceful_shutdown=5)


if __name__ == '__main__':
    main()
