"""Chrome and MCP E2E tests. Set WINDOWS_E2E_ISO to also exercise real Docker/QEMU."""
import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from playwright.async_api import async_playwright, expect

CONSOLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONSOLE))
import server
from vm_manager import VMManager


class ManagementE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='windows-e2e-')))
        media = root / 'iso'
        media.mkdir()
        legacy = root / 'legacy'
        iso = os.getenv('WINDOWS_E2E_ISO')
        self.image_id = 'Windows, 日本語.iso'
        self.manager = VMManager(state=root / 'state', media=media, legacy_state=legacy)
        if iso and self._testMethodName in ('test_real_vm_lifecycle', 'test_fresh_standalone_vm_nat',
                                          'test_force_delete_after_shutdown_failure'):
            # Point the catalog at the existing ISO without copying a multi-GiB installer.
            original = Path(iso).resolve(strict=True)
            self.manager = VMManager(state=root / 'state', media=original.parent, legacy_state=legacy)
            self.image_id = original.name
            self.addAsyncCleanup(self.remove_test_containers)
        else:
            (media / self.image_id).touch()
        app = server.create_app(self.manager)
        app.cleanup_ctx.remove(server.admin_site)
        self.site = await self.enterAsyncContext(TestServer(app))
        self.base = str(self.site.make_url('/')).rstrip('/')
        service_state = root / 'service'
        service_state.mkdir()
        admin = web.AppRunner(server.private_app(app), shutdown_timeout=2)
        await admin.setup()
        self.addAsyncCleanup(admin.cleanup)
        await web.UnixSite(admin, str(service_state / 'console-admin.sock')).start()
        params = StdioServerParameters(
            command=sys.executable, args=[str(CONSOLE / 'mcp/service.py')],
            env=dict(os.environ, WINDOWS_MANAGER_STATE=str(self.manager.state),
                     WINDOWS_MANAGER_ISO_DIR=str(self.manager.media),
                     WINDOWS_CONSOLE_SERVICE_STATE=str(service_state)),
        )
        ready = asyncio.get_running_loop().create_future()
        done = asyncio.Event()

        async def run_mcp():
            async with stdio_client(params) as streams, ClientSession(*streams) as client:
                await client.initialize()
                ready.set_result(client)
                await done.wait()

        task = asyncio.create_task(run_mcp())

        async def finish_mcp():
            done.set()
            await task

        self.addAsyncCleanup(finish_mcp)
        await asyncio.wait_for(asyncio.wait({ready, task}, return_when=asyncio.FIRST_COMPLETED), 8)
        if task.done():
            await task
        self.mcp = ready.result()
        playwright = await self.enterAsyncContext(async_playwright())
        self.browser = await playwright.chromium.launch(
            executable_path=os.getenv('WINDOWS_E2E_CHROME') or shutil.which('google-chrome'))
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        self.page.set_default_timeout(8000)
        self.page.on('dialog', lambda dialog: dialog.accept())
        await self.page.goto(self.base + '/vms/')
        await expect(self.page.locator('#new-template')).to_be_enabled()

    async def remove_test_containers(self):
        def remove():
            names = self.manager.statuses()
            for name in sorted(names, key=lambda name: '-router-' in name):
                self.manager.docker('container', 'rm', '-f', name)
        await asyncio.to_thread(remove)

    async def call(self, tool, **args):
        result = await self.mcp.call_tool(tool, args)
        data = result.content[0].text
        self.assertFalse(result.isError, data)
        return json.loads(data)

    async def make_template(self, name='Windows E2E'):
        page = self.page
        await page.locator('#new-template').click()
        await page.get_by_label('名前', exact=True).fill(name)
        await page.get_by_label('インストール ISO', exact=True).select_option(self.image_id)
        await page.get_by_label('vCPU', exact=True).fill('2')
        await page.get_by_label('RAM (MiB)', exact=True).fill('3072')
        await page.get_by_role('button', name='保存', exact=True).click()
        await expect(page.get_by_text(name, exact=True)).to_be_visible()

    async def test_old_inventory_response_cannot_overwrite_a_newer_save(self):
        await self.save_with_delayed_inventory()

    async def test_old_inventory_failure_cannot_replace_a_newer_success(self):
        await self.save_with_delayed_inventory(fail=True)
        await expect(self.page.get_by_text('delayed inventory failure', exact=True)).to_have_count(0)

    async def save_with_delayed_inventory(self, fail=False):
        page = self.page
        captured = asyncio.Event()
        release = asyncio.Event()
        delayed = False

        async def capture(route):
            nonlocal delayed
            if delayed:
                await route.continue_()
                return
            delayed = True
            response = await route.fetch()
            captured.set()
            await release.wait()
            if fail:
                await route.fulfill(status=503, json={'error': 'delayed inventory failure'})
            else:
                await route.fulfill(response=response)

        await page.route('**/api/management', capture)
        await page.locator('#refresh').click()
        try:
            await asyncio.wait_for(captured.wait(), 8)
            await self.make_template()
            self.assertEqual((await self.call('vm_inventory'))['templates'][0]['name'], 'Windows E2E')
            async with page.expect_response('**/api/management') as pending:
                release.set()
            self.assertIsNone(await (await pending.value).finished())
            await page.evaluate('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            await expect(page.get_by_text('Windows E2E', exact=True)).to_be_visible(timeout=500)
        finally:
            release.set()
            await page.unroute_all(behavior='wait')

    async def test_web_edits_inventory_created_by_mcp(self):
        await self.make_template()
        template = (await self.call('vm_inventory'))['templates'][0]
        topology = await self.call('vm_topology_save', name='E2E network',
                                   segments=['10.113.60.0/24', '10.113.61.0/24'])
        vm = await self.call('vm_create', name='E2E endpoint', template_id=template['id'],
                             topology_id=topology['id'], segment=topology['segments'][0])
        page = self.page
        await page.locator('#refresh').click()
        row = page.get_by_role('row', name='E2E endpoint', exact=False)
        await expect(row.get_by_text(topology['segments'][0], exact=True)).to_be_visible()
        await row.get_by_role('button', name='接続先を変更').click()
        await page.get_by_label('セグメント', exact=True).select_option(topology['segments'][1])
        await page.get_by_role('button', name='保存', exact=True).click()
        await expect(row.get_by_text(topology['segments'][1], exact=True)).to_be_visible()
        await page.locator('#templates').get_by_role('button', name='削除', exact=True).click()
        await expect(page.get_by_text(template['name'], exact=True)).to_have_count(0)
        snapshot = await self.call('vm_inventory')
        self.assertEqual(snapshot['vms'], [dict(vm, segment=topology['segments'][1])])
        await row.get_by_role('button', name='削除', exact=True).click()
        await expect(row).to_have_count(0)
        self.assertEqual((await self.call('vm_inventory'))['vms'], [])

    async def test_first_vm_creation_follows_template_setup(self):
        page = self.page
        await expect(page.locator('#new-vm')).to_contain_text('テンプレートを作成')
        await page.locator('#new-vm').click()
        await page.get_by_label('名前', exact=True).fill('First template')
        await page.get_by_role('button', name='保存', exact=True).click()
        await expect(page.get_by_role('dialog')).to_have_accessible_name('VM を作成')
        await page.get_by_label('VM 名').fill('First VM')
        await page.get_by_label('インターネット接続を有効にする（個別NAT）').check()
        await page.get_by_role('button', name='作成', exact=True).click()
        row = page.get_by_role('row', name='First VM', exact=False)
        await expect(row.get_by_text('個別NAT（有効に設定）')).to_be_visible()
        vm, = (await self.call('vm_inventory'))['vms']
        self.assertEqual(vm['name'], 'First VM')
        self.assertTrue(vm['internet_enabled'])
        await row.get_by_role('button', name='インターネット設定').click()
        await page.get_by_label('インターネット接続を有効にする（個別NAT）').uncheck()
        await page.get_by_role('button', name='保存', exact=True).click()
        await expect(row.get_by_text('外部接続なし')).to_be_visible()
        self.assertFalse((await self.call('vm_inventory'))['vms'][0]['internet_enabled'])

    async def test_agent_guide_from_empty_inventory(self):
        page = self.page
        await page.context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=self.base)
        self.assertFalse((await self.call('vm_inventory'))['vms'])
        await page.get_by_role('button', name='AIエージェントで操作', exact=True).click(timeout=3000)
        guide = page.get_by_role('dialog', name='AIエージェントで操作')
        await expect(guide.get_by_text('VM作成前', exact=False)).to_be_visible()
        await guide.get_by_role('button', name='最初のVMを作成する指示をコピー').click()
        prompt = await page.evaluate('navigator.clipboard.readText()')
        for tool in ('vm_inventory', 'vm_template_save', 'vm_create', 'vm_start', 'console_screen'):
            self.assertIn(tool, prompt)
        await guide.get_by_text('OpenCode', exact=True).click()
        await guide.get_by_role('button', name='OpenCodeの設定をコピー').click()
        config = json.loads(await page.evaluate('navigator.clipboard.readText()'))
        project_config = json.loads((CONSOLE.parent / 'opencode.json').read_text())
        self.assertEqual(config['mcp'], project_config['mcp'])
        await guide.get_by_role('button', name='OpenCodeのSSH設定をコピー').click()
        config = json.loads(await page.evaluate('navigator.clipboard.readText()'))
        self.assertEqual(config['mcp']['servers']['windows-console']['command'][-2:],
                         ['vm-host', '.local/bin/windows-console-mcp'])

    async def test_agent_guide_during_creation_and_with_vm_id(self):
        await self.make_template()
        page = self.page
        await page.context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=self.base)
        await page.locator('#new-vm').click()
        editor = page.get_by_role('dialog', name='VM を作成')
        await editor.get_by_label('VM 名').fill('Guided edition')
        await editor.get_by_role('button', name='AIエージェントで操作').click()
        guide = page.get_by_role('dialog', name='AIエージェントで操作')
        await expect(guide.locator('#prompt-setup')).to_contain_text('Guided edition')
        template, = (await self.call('vm_inventory'))['templates']
        await expect(guide.locator('#prompt-setup')).to_contain_text(template['id'])
        await guide.get_by_role('button', name='ローカル接続コマンドをコピー').click()
        await expect(guide.get_by_role('status')).to_have_text('コピーしました。')
        self.assertIn('codex mcp add', await page.evaluate('navigator.clipboard.readText()'))
        await guide.get_by_role('button', name='閉じる').click()
        await expect(editor.get_by_label('VM 名')).to_have_value('Guided edition')
        await editor.get_by_role('button', name='作成', exact=True).click()
        row = page.get_by_role('row', name='Guided edition', exact=False)
        await row.get_by_role('button', name='AIエージェントで操作').click()
        vm, = (await self.call('vm_inventory'))['vms']
        await expect(guide.locator('#prompt-setup')).to_contain_text(vm['id'])
        await guide.get_by_role('button', name='画面確認の指示をコピー').click()
        await expect(guide.get_by_role('status')).to_have_text('コピーしました。')
        prompt = await page.evaluate('navigator.clipboard.readText()')
        self.assertIn(vm['id'], prompt)
        self.assertIn('console_screen', prompt)

    @unittest.skipUnless(os.getenv('WINDOWS_E2E_ISO'), 'set WINDOWS_E2E_ISO for Docker/KVM')
    async def test_force_delete_after_shutdown_failure(self):
        await self.make_template()
        template = (await self.call('vm_inventory'))['templates'][0]
        vm = await self.call('vm_create', name='Shutdown recovery', template_id=template['id'])
        await self.call('vm_start', vm_id=vm['id'])
        page = self.page
        await page.locator('#refresh').click()
        row = page.get_by_role('row', name='Shutdown recovery', exact=False)
        await page.route('**/stop', lambda route: route.fulfill(
            status=409, json={'error': 'Windowsの終了を確認できません'}))
        await row.get_by_role('button', name='停止', exact=True).click()
        await expect(page.locator('#message')).to_have_text('Windowsの終了を確認できません')
        await expect(page.locator('#recovery').get_by_role('button', name='強制停止', exact=True)).to_be_visible()
        recovery = page.locator('#recovery').get_by_role('button', name='強制停止して削除')
        self.assertEqual((await self.call('vm_inventory'))['vms'][0]['status'], 'running')
        await recovery.click()
        await expect(row).to_have_count(0, timeout=20000)
        self.assertFalse(self.manager.vm_dir(vm['id']).exists())
        self.assertFalse((await self.call('vm_inventory'))['vms'])

    async def test_topology_graph_builds_and_moves_connections(self):
        await self.make_template()
        template = (await self.call('vm_inventory'))['templates'][0]
        await self.call('vm_create', name='Graph endpoint', template_id=template['id'])
        page = self.page
        await page.locator('#new-topology').click()
        dialog = page.get_by_role('dialog')
        await dialog.get_by_label('トポロジー名').fill('Graph lab')
        cidrs = dialog.get_by_label('セグメントの IPv4 CIDR')
        await cidrs.nth(0).fill('10.113.60.0/24')
        await dialog.get_by_role('button', name='＋ セグメントを追加').click()
        await cidrs.nth(1).fill('10.113.61.0/24')
        await dialog.get_by_role('checkbox').check()
        await expect(dialog.get_by_text('透過プロキシ', exact=True)).to_be_visible()
        await dialog.get_by_role('button', name='保存', exact=True).click()
        board = page.get_by_role('region', name='Graph lab')
        segment = board.get_by_role('region', name='10.113.61.0/24')
        detached = page.get_by_role('region', name='未接続の VM')
        await expect(board.get_by_text('透過プロキシ', exact=True)).to_be_visible()
        await detached.get_by_role('listitem').drag_to(segment)
        await expect(segment.get_by_text('Graph endpoint', exact=True)).to_be_visible()
        snapshot = await self.call('vm_inventory')
        self.assertEqual(snapshot['vms'][0]['segment'], '10.113.61.0/24')
        self.assertTrue(snapshot['topologies'][0]['proxy'])

        await board.get_by_role('button', name='構成を編集').click()
        await dialog.get_by_role('button', name='セグメントを削除').nth(1).click()
        await expect(dialog.get_by_role('status')).to_contain_text('Graph endpoint')
        await expect(dialog.get_by_role('button', name='保存', exact=True)).to_be_disabled()
        await dialog.get_by_role('button', name='キャンセル').click()

        await page.set_viewport_size({'width': 390, 'height': 844})
        await segment.get_by_role('button', name='Graph endpoint の接続先を変更').press('Enter')
        await dialog.get_by_label('セグメント', exact=True).select_option('10.113.60.0/24')
        await dialog.get_by_role('button', name='保存', exact=True).click()
        await expect(board.get_by_role('region', name='10.113.60.0/24')
                     .get_by_text('Graph endpoint', exact=True)).to_be_visible()
        self.assertEqual(await page.locator('#vms .scroll').evaluate('e => e.scrollWidth <= e.clientWidth'), True)

    @unittest.skipUnless(os.getenv('WINDOWS_E2E_ISO'), 'set WINDOWS_E2E_ISO for Docker/KVM')
    async def test_real_vm_lifecycle(self):
        await self.make_template()
        template = (await self.call('vm_inventory'))['templates'][0]
        topology = await self.call('vm_topology_save', name='E2E router',
                                   segments=['10.113.60.0/24', '10.113.61.0/24'])
        vm = await self.call('vm_create', name='E2E VM', template_id=template['id'],
                             topology_id=topology['id'], segment=topology['segments'][1])
        vm_id = vm['id']
        page = self.page
        await page.locator('#refresh').click()
        invoked = asyncio.Event()
        proceed = asyncio.Event()

        async def delay_start(route):
            invoked.set()
            await proceed.wait()
            await route.continue_()

        await page.route('**/start', delay_start)
        start = page.get_by_role('button', name='起動', exact=True)
        await start.wait_for()
        await page.evaluate("""() => {
            window.vmListUpdates = 0;
            new MutationObserver(() => window.vmListUpdates++)
                .observe(document.querySelector('#vms'), {childList: true});
        }""")
        await start.click()
        try:
            await asyncio.wait_for(invoked.wait(), 5)
            await expect(page.locator('#operation')).to_contain_text('を起動しています')
            await self.assert_list_updates(page, 0)
        finally:
            proceed.set()
        link = page.get_by_role('link', name='コンソール', exact=True)
        await expect(link).to_be_visible(timeout=30000)
        await self.assert_list_updates(page, 1)
        board = page.get_by_role('region', name='E2E router')
        await expect(board.locator('.router-node')).to_contain_text('稼働中')
        await board.get_by_role('button', name='構成を編集').click()
        editor = page.get_by_role('dialog')
        await editor.get_by_role('checkbox').check()
        await expect(editor.get_by_role('status')).to_contain_text('先に停止してください · E2E VM')
        await expect(editor.get_by_role('button', name='保存', exact=True)).to_be_disabled()
        await editor.get_by_role('button', name='キャンセル').click()
        router = self.manager.name('router', topology['id'])
        link_state = await asyncio.to_thread(self.manager.docker, 'exec', router,
                                             'ip', '-j', 'link', 'show', 'dev', 'vm' + vm_id)
        self.assertEqual(json.loads(link_state)[0]['master'], 'lan1')
        url = self.base + await link.get_attribute('href')
        fallback = await self.browser.new_page()
        await fallback.route_web_socket('**/display', lambda socket: socket.close())
        async with fallback.expect_websocket(predicate=lambda socket: socket.url.endswith('/display-frames')):
            await fallback.goto(url)
        await expect(fallback.locator('#display canvas')).to_be_visible(timeout=15000)
        await fallback.close()
        first = await self.browser.new_page()
        messages = []
        first.on('console', lambda event: messages.append(event.text) if event.type == 'error' else None)
        first.on('pageerror', lambda event: messages.append(str(event)))
        await first.goto(url)
        try:
            await expect(first.locator('#display canvas')).to_be_visible(timeout=15000)
        except AssertionError as exc:
            raise AssertionError(f'{exc}\nBrowser errors: {messages}') from exc
        reads = await first.evaluate("""async () => {
            const stage = document.querySelector('#stage');
            const getWidth = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth').get;
            let reads = 0;
            Object.defineProperty(stage, 'clientWidth', {get() { reads++; return getWidth.call(this); }});
            const probe = document.createElement('span');
            document.querySelector('#display').append(probe);
            for (let i = 0; i < 24; i++) {
                probe.setAttribute('width', i);
                await Promise.resolve();
            }
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            delete stage.clientWidth;
            probe.remove();
            return reads;
        }""")
        self.assertLessEqual(reads, 2, f'24 display mutations triggered {reads} layout reads')
        second = await self.browser.new_page()
        await second.goto(url)
        await expect(second.locator('#display canvas')).to_be_visible(timeout=15000)
        await self.exercise_mcp_on_fresh_vm(vm, first)
        await first.close()
        await second.close()
        # Keep a client connected during a power cycle. Its input belongs to the old QEMU process.
        async with aiohttp.ClientSession(headers={'Origin': self.base}) as http:
            async with http.ws_connect(url + 'input') as old:
                self.assertEqual((await old.receive_json(timeout=5))['display'], 'spice')
                await old.send_json({'op': 'key', 'key': 'shift', 'down': True, 'id': 'held'})
                self.assertEqual(await old.receive_json(timeout=5), {'id': 'held', 'result': None})
                await self.call('vm_stop', vm_id=vm_id, force=True)
                state = self.manager.vm_dir(vm_id)
                shutil.copyfile(state / 'setup/console-tools.iso', state / 'alternate.iso')
                await self.call('select_image', vm_id=vm_id, image_id='guest-tools/alternate.iso')
                await self.call('vm_start', vm_id=vm_id)
                media = await self.call('list_images', vm_id=vm_id)
                self.assertEqual(media['current'], '/vm-clean/alternate.iso')
                try:
                    current = await http.ws_connect(url + 'input')
                except aiohttp.WSServerHandshakeError as exc:
                    self.fail(f'New console failed to connect after VM restart: HTTP {exc.status}')
                async with current:
                    self.assertEqual((await current.receive_json(timeout=5))['display'], 'spice')
                    await current.send_json({'op': 'key', 'key': 'shift', 'down': True, 'id': 'fresh'})
                    self.assertEqual(await current.receive_json(timeout=5), {'id': 'fresh', 'result': None})
                    await old.send_json({'op': 'key', 'key': 'ctrl', 'down': True, 'id': 'stale'})
                    closed = await old.receive(timeout=8)
                    self.assertEqual((closed.type, closed.data), (aiohttp.WSMsgType.CLOSE, 1012))
        await page.locator('#refresh').click()
        force = page.get_by_role('button', name='強制停止', exact=True)
        await expect(force).not_to_be_visible()
        await page.get_by_label('E2E VM のその他の操作').click()
        await force.click()
        await expect(page.locator('#vms').get_by_text('停止中', exact=True)).to_be_visible(timeout=15000)
        await page.locator('#vms').get_by_role('button', name='削除', exact=True).click()
        await expect(page.get_by_text('E2E VM', exact=True)).to_have_count(0)
        await self.call('vm_topology_delete', topology_id=topology['id'])
        self.assertFalse(self.manager.vm_dir(vm_id).exists())

    @unittest.skipUnless(os.getenv('WINDOWS_E2E_ISO'), 'set WINDOWS_E2E_ISO for Docker/KVM')
    async def test_fresh_standalone_vm_nat(self):
        self.assertFalse(self.manager.legacy_state.exists())
        await self.make_template()
        template = (await self.call('vm_inventory'))['templates'][0]
        vm = await self.call('vm_create', name='Standalone Windows', template_id=template['id'])
        self.assertFalse((await self.call('vm_internet_status'))['next_boot_enabled'])
        self.assertTrue((await self.call('vm_set_internet', enabled=True))['next_boot_enabled'])
        page = self.page
        await page.locator('#refresh').click()
        await page.get_by_role('button', name='起動してコンソールを開く').click()
        await expect(page).to_have_url(self.base + f'/vms/{vm["id"]}/console/', timeout=30000)
        await expect(page.locator('#display canvas')).to_be_visible(timeout=15000)
        status = await self.call('vm_internet_status', vm_id=vm['id'])
        self.assertTrue(status['current_enabled'])
        self.assertFalse(status['restart_required'])
        status = await self.call('vm_set_internet', vm_id=vm['id'], enabled=False)
        self.assertTrue(status['restart_required'])
        await self.call('vm_stop', vm_id=vm['id'], force=True)
        await self.call('vm_start', vm_id=vm['id'])
        self.assertFalse((await self.call('vm_internet_status'))['current_enabled'])
        await self.call('vm_stop', vm_id=vm['id'], force=True)
        await self.call('vm_delete', vm_id=vm['id'])

    async def exercise_mcp_on_fresh_vm(self, vm, page):
        vm_id = vm['id']
        status = await self.call('vm_status', vm_id=vm_id)
        self.assertEqual(status['vm_id'], vm_id)
        self.assertTrue(status['qemu']['running'])
        screen = await self.mcp.call_tool('console_screen', {'vm_id': vm_id})
        self.assertFalse(screen.isError, screen.content)
        self.assertTrue(base64.b64decode(screen.content[0].data).startswith(b'\x89PNG'))
        await self.call('console_demo_show', vm_id=vm_id, x=4000, y=4000,
                        text='MCP scoped walkthrough', duration_seconds=8)
        await expect(page.get_by_text('MCP scoped walkthrough', exact=True)).to_be_visible()
        stream = await self.call('console_stream_open', vm_id=vm_id, views='both')
        await self.call('console_stream_close', stream_id=stream['stream_id'])
        await self.call('console_demo_clear', vm_id=vm_id)
        media = await self.call('list_images', vm_id=vm_id)
        self.assertIn('workspace/' + self.image_id, [item['id'] for item in media['images']])

        guest_socket = self.manager.vm_dir(vm_id) / 'transfer-http.sock'
        data = b'VM-scoped transfer'
        turn = await self.call('file_begin_download', vm_id=vm_id, filename='hello.txt', size=len(data))
        token = turn['transfer_id']
        await self.call('file_write_chunk', vm_id=vm_id, transfer_id=token, offset=0,
                        data_base64=base64.b64encode(data).decode())
        published = await self.call('file_publish', vm_id=vm_id, transfer_id=token)
        self.assertEqual(published['guest_network'], 'attached')
        upload = await self.call('file_request_upload', vm_id=vm_id)
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=guest_socket)) as guest:
            async with guest.get(published['url']) as response:
                self.assertEqual(response.status, 200, await response.text())
                self.assertEqual(await response.read(), data)
            form = aiohttp.FormData()
            form.add_field('file', data, filename='roundtrip.txt')
            async with guest.post(upload['url'], data=form) as response:
                self.assertEqual(response.status, 200, await response.text())
        chunk = await self.call('file_read_chunk', vm_id=vm_id, transfer_id=upload['transfer_id'], offset=0)
        self.assertEqual(base64.b64decode(chunk['data_base64']), data)

    async def assert_list_updates(self, page, expected):
        self.assertEqual(await page.evaluate('window.vmListUpdates'), expected)


if __name__ == '__main__':
    unittest.main()
