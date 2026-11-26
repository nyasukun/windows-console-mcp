"""Loopback JSON endpoints for the VM management page."""
import asyncio
import subprocess

from aiohttp import web

from vm_manager import Conflict


def install(app, manager):
    async def overview(request):
        return web.json_response(await asyncio.to_thread(manager.overview))

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
        ('POST', '/api/templates'): manager.save_template,
        ('PUT', '/api/templates/{template_id}'): manager.save_template,
        ('DELETE', '/api/templates/{template_id}'): manager.delete_template,
        ('POST', '/api/topologies'): manager.save_topology,
        ('PUT', '/api/topologies/{topology_id}'): manager.save_topology,
        ('DELETE', '/api/topologies/{topology_id}'): manager.delete_topology,
        ('POST', '/api/vms'): manager.create_vm,
        ('PUT', '/api/vms/{vm_id}/network'): manager.set_vm_network,
        ('PUT', '/api/vms/{vm_id}/internet'): manager.set_internet,
        ('POST', '/api/vms/{vm_id}/start'): manager.start_vm,
        ('POST', '/api/vms/{vm_id}/stop'): manager.stop_vm,
        ('DELETE', '/api/vms/{vm_id}'): manager.delete_vm,
    }
    app.router.add_get('/api/management', overview)
    for method, path in operations:
        app.router.add_route(method, path, mutate)
