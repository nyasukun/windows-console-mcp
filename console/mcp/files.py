"""Stream local files through the Windows console stdio MCP tools."""
import argparse
import asyncio
import base64
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args):
    if args.ssh:
        params = StdioServerParameters(command='ssh', args=[
            '-T', '-o', 'BatchMode=yes', '--', args.ssh, shlex.quote(args.remote_command)])
    else:
        params = StdioServerParameters(command=str(Path(__file__).with_name('start.sh').resolve()))
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        await session.initialize()

        async def call(name, **kwargs):
            reply = await session.call_tool(name, kwargs)
            if reply.isError:
                raise RuntimeError(reply.content[0].text)
            return reply.structuredContent or json.loads(reply.content[0].text)

        if args.action == 'send':
            with args.path.open('rb') as file:
                result = await call('file_begin_download', filename=args.path.name,
                                    size=os.fstat(file.fileno()).st_size)
                token = result['transfer_id']
                print('transfer_id=' + token, file=sys.stderr)
                offset = 0
                while chunk := file.read(result['max_chunk']):
                    ack = await call('file_write_chunk', transfer_id=token, offset=offset,
                                     data_base64=base64.b64encode(chunk).decode('ascii'))
                    offset += len(chunk)
                    if ack['next_offset'] != offset:
                        raise RuntimeError('Unexpected write offset')
            result = await call('file_publish', transfer_id=token)
        elif args.action == 'request-upload':
            result = await call('file_request_upload')
        elif args.action == 'receive':
            result = await call('file_status', transfer_id=args.id)
            if result['direction'] != 'from_vm' or not result['ready']:
                raise ValueError('VM upload has not completed')
            if args.path.exists():
                raise FileExistsError(args.path)
            temporary = tempfile.NamedTemporaryFile(dir=args.path.parent, delete=False)
            try:
                with temporary as file:
                    offset = 0
                    while True:
                        chunk = await call('file_read_chunk', transfer_id=args.id, offset=offset,
                                           length=result['max_chunk'])
                        block = base64.b64decode(chunk['data_base64'], validate=True)
                        offset += len(block)
                        if offset != chunk['next_offset'] or (not block and not chunk['eof']):
                            raise ValueError('Unexpected read offset')
                        file.write(block)
                        if chunk['eof']:
                            break
                    if offset != result['size']:
                        raise ValueError('Incomplete file')
                # Reserve the final name without clobbering another receiver.
                # Renaming on the same volume also works on filesystems without hard links.
                with args.path.open('xb'):
                    pass
                try:
                    os.replace(temporary.name, args.path)
                except OSError:
                    args.path.unlink()
                    raise
            finally:
                Path(temporary.name).unlink(missing_ok=True)
            result = {'path':str(args.path), 'bytes':offset}
        elif args.action == 'status':
            result = await call('file_status', transfer_id=args.id)
        else:
            result = await call('file_discard', transfer_id=args.id)
        print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh', help='Run the MCP server on this SSH host')
    parser.add_argument('--remote-command', default='.local/bin/windows-console-mcp',
                        help='Path to the MCP launcher on the SSH host')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('send').add_argument('path', type=Path)
    commands.add_parser('request-upload')
    for name in ('receive', 'status', 'discard'):
        sub = commands.add_parser(name)
        sub.add_argument('id')
        if name == 'receive':
            sub.add_argument('path', type=Path)
    asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    main()
