"""Loopback JSON endpoints for the VM management page."""
import asyncio
import subprocess

from aiohttp import web

from sse_connection import sse_connection
from vm_manager import Conflict


def install(app, manager):
    async def overview(request):
        return web.json_response(await asyncio.to_thread(manager.overview))

    async def sse_details(request):
        reveal = request.method == 'POST'
        try:
            result = await asyncio.to_thread(sse_connection, reveal)
            data = result if result else {'error': '稼働中のSSEサーバーが見つかりません'}
            status = 200 if result else 404
        except ValueError as exc:
            data, status = {'error': str(exc)}, 409
        return web.json_response(data, status=status, headers={'Cache-Control': 'no-store'})

    async def upload_iso(request):
        if request.content_type != 'application/octet-stream':
            raise web.HTTPUnsupportedMediaType()
        try:
            with manager.iso_upload(request.match_info['image_id']) as (file, target):
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    await asyncio.to_thread(file.write, chunk)
                uploaded = await asyncio.to_thread(manager.publish_iso, file, target)
        except Conflict as exc:
            return web.json_response({'error': str(exc)}, status=409)
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=400)
        except OSError as exc:
            return web.json_response({'error': str(exc)}, status=503)
        return web.json_response(uploaded, status=201)

    async def mutate(request):
        if request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType()
        operation = operations[(request.method, request.match_info.route.resource.canonical)]
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('JSONオブジェクトを指定してください')
            args = dict(request.match_info)
            reserved = set(args)
            if operation in (manager.save_template, manager.save_topology) and request.method == 'POST':
                reserved.update(('template_id', 'topology_id'))
            if body.keys() & reserved:
                raise ValueError('IDはURLで指定してください')
            result = await asyncio.to_thread(operation, **args, **body)
        except KeyError:
            return web.json_response({'error': '指定した項目が見つかりません'}, status=404)
        except Conflict as exc:
            return web.json_response({'error': str(exc)}, status=409)
        except (ValueError, TypeError) as exc:
            return web.json_response({'error': str(exc)}, status=400)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            return web.json_response({'error': str(exc) or '操作がタイムアウトしました'}, status=503)
        return web.json_response(result)

    operations = {
        ('PUT', '/api/images/{image_id}'): manager.rename_iso,
        ('DELETE', '/api/images/{image_id}'): manager.delete_iso,
        ('POST', '/api/templates'): manager.save_template,
        ('PUT', '/api/templates/{template_id}'): manager.save_template,
        ('DELETE', '/api/templates/{template_id}'): manager.delete_template,
        ('POST', '/api/topologies'): manager.save_topology,
        ('PUT', '/api/topologies/{topology_id}'): manager.save_topology,
        ('DELETE', '/api/topologies/{topology_id}'): manager.delete_topology,
        ('PUT', '/api/topologies/{topology_id}/power'): manager.power_topology,
        ('PUT', '/api/topologies/{topology_id}/dns'): manager.set_topology_dns,
        ('POST', '/api/vms'): manager.create_vm,
        ('PUT', '/api/vms/{vm_id}/network'): manager.set_vm_network,
        ('PUT', '/api/vms/{vm_id}/internet'): manager.set_internet,
        ('POST', '/api/vms/{vm_id}/start'): manager.start_vm,
        ('POST', '/api/vms/{vm_id}/stop'): manager.stop_vm,
        ('DELETE', '/api/vms/{vm_id}'): manager.delete_vm,
        ('POST', '/api/endpoints'): manager.create_endpoint,
        ('PUT', '/api/endpoints/{endpoint_id}/network'): manager.set_endpoint_network,
        ('POST', '/api/endpoints/{endpoint_id}/start'): manager.start_endpoint,
        ('POST', '/api/endpoints/{endpoint_id}/stop'): manager.stop_endpoint,
        ('DELETE', '/api/endpoints/{endpoint_id}'): manager.delete_endpoint,
    }
    app.router.add_get('/api/management', overview)
    app.router.add_get('/api/mcp/sse', sse_details)
    app.router.add_post('/api/mcp/sse/token', sse_details)
    app.router.add_post('/api/images/{image_id}/upload', upload_iso)
    for method, path in operations:
        app.router.add_route(method, path, mutate)
