"""Real Docker/Chrome/SSH/DNS browser workflow."""
import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pycdlib
from aiohttp.test_utils import TestServer
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from playwright.async_api import async_playwright, expect

CONSOLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONSOLE))
import server
from qmp import Monitor
from vm_manager import VMManager


@unittest.skipUnless(os.environ.get('WINDOWS_CONTAINERS_E2E') == '1',
                     'set WINDOWS_CONTAINERS_E2E=1 after building all three images')
class ContainerE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='wce-')))
        media = root
        if self._testMethodName == 'test_chrome_reaches_windows_http_across_segments':
            media = Path(os.environ['WINDOWS_E2E_ISO']).resolve(strict=True).parent
        self.manager = VMManager(state=root / 'state', media=media, legacy_state=root / 'legacy')
        self.addAsyncCleanup(asyncio.to_thread, self.cleanup_containers)
        app = server.create_app(self.manager)
        app.cleanup_ctx.remove(server.admin_site)
        site = await self.enterAsyncContext(TestServer(app))
        self.base = str(site.make_url('/')).rstrip('/')
        chrome = await self.enterAsyncContext(async_playwright())
        browser = await chrome.chromium.launch(executable_path=os.getenv('WINDOWS_E2E_CHROME')
                                                or shutil.which('google-chrome'))
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()
        self.page.set_default_timeout(12000)
        self.page.on('dialog', lambda dialog: dialog.accept())
        params = StdioServerParameters(command=sys.executable, args=[str(CONSOLE / 'mcp/service.py')],
                                       env=dict(os.environ, WINDOWS_MANAGER_STATE=str(self.manager.state),
                                                WINDOWS_MANAGER_ISO_DIR=str(media)))
        # Keep MCP's anyio contexts in one task across setup, calls and cleanup.
        ready = asyncio.get_running_loop().create_future()
        finish = asyncio.Event()

        async def mcp_session():
            async with stdio_client(params) as streams, ClientSession(*streams) as client:
                await client.initialize()
                ready.set_result(client)
                await finish.wait()

        task = asyncio.create_task(mcp_session())

        async def close_mcp():
            finish.set()
            await task

        self.addAsyncCleanup(close_mcp)
        self.mcp = await asyncio.wait_for(ready, 10)

    def cleanup_containers(self):
        inventory = self.manager.read()
        for item in inventory['endpoints'].values():
            self.manager.stop_endpoint(item['id'])
        for vm in inventory['vms'].values():
            self.manager.delete_vm(vm['id'], force=True)
        for topology in inventory['topologies'].values():
            self.manager.set_topology_dns(topology['id'], None)
        for item in inventory['endpoints'].values():
            self.manager.delete_endpoint(item['id'])
        for topology in inventory['topologies'].values():
            self.manager.delete_topology(topology['id'])

    async def tool(self, tool_name, **args):
        answer = await self.mcp.call_tool(tool_name, args)
        self.assertFalse(answer.isError, answer.content)
        if answer.content[0].type == 'image':
            return base64.b64decode(answer.content[0].data)
        if answer.structuredContent is not None:
            return answer.structuredContent.get('result', answer.structuredContent)
        return json.loads(answer.content[0].text)

    async def connect_ui(self, name, kind, topology, segment):
        page = self.page
        await page.goto(self.base + '/vms/')
        await page.get_by_role('button', name='コンテナを作成').click()
        dialog = page.get_by_role('dialog', name='コンテナを作成')
        await dialog.get_by_label('コンテナ名').fill(name)
        await dialog.get_by_label('種類').select_option(kind)
        await dialog.get_by_label('トポロジ', exact=True).select_option(topology)
        await dialog.get_by_label('セグメント', exact=True).select_option(segment)
        await dialog.get_by_role('button', name='作成', exact=True).click()
        await expect(dialog).not_to_be_visible()
        return next(item for item in (await self.tool('vm_inventory'))['endpoints'] if item['name'] == name)

    def card(self, name):
        return self.page.locator('#endpoints .card').filter(has_text=name)

    async def test_web_chrome_ssh_dns_and_mcp(self):
        page = self.page
        segments = ['10.95.1.0/28', '10.95.2.0/28']
        topology = await self.tool('vm_topology_save', name='DNS lab', segments=segments)
        ident = topology['id']
        linux = await self.connect_ui('Linux fixture', 'linux', ident, segments[0])
        browser = await self.connect_ui('Chrome fixture', 'browser', ident, segments[1])
        lid, bid = linux['id'], browser['id']
        board = page.get_by_role('region', name='DNS lab', exact=True)
        await expect(board.get_by_text('Linux fixture')).to_be_visible()
        await board.get_by_role('button', name='DNS 設定').click()
        editor = page.get_by_role('dialog', name='DNS lab の DNS 設定')
        await editor.get_by_label('DNS を有効にする').check()
        await editor.get_by_role('button', name='レコードを追加').click()
        await editor.get_by_label('ドメイン名').fill('app.endpoint.test')
        await editor.get_by_label('接続先 IPv4').fill(linux['address'])
        await editor.get_by_role('button', name='保存', exact=True).click()
        await expect(editor).not_to_be_visible()
        await board.get_by_role('button', name='ネットワークを起動').click()
        await expect(board.get_by_text('稼働中 · 1 件')).to_be_visible(timeout=20000)

        await self.card('Linux fixture').get_by_role('button', name='起動して開く').click()
        await expect(page.locator('#status')).to_have_text('SSH 接続済み', timeout=30000)
        await page.keyboard.type("printf '%s\n' terminal-through-ssh > /root/web-terminal.txt")
        await page.keyboard.press('Enter')
        result = await self.tool('linux_exec', endpoint_id=lid,
                                 command='for x in 1 2 3 4 5; do [ -f /root/web-terminal.txt ] && break; sleep 1; done; cat /root/web-terminal.txt')
        self.assertEqual(result['stdout'], 'terminal-through-ssh\n')
        result = await self.tool('linux_exec', endpoint_id=lid, command='getent ahostsv4 app.endpoint.test')
        self.assertIn(linux['address'], result['stdout'])

        await page.get_by_role('link', name='VM・コンテナ管理').click()
        await self.card('Linux fixture').get_by_role('button', name='停止').click()
        await expect(self.card('Linux fixture').get_by_role('button', name='起動して開く')).to_be_visible()
        await self.tool('container_start', endpoint_id=lid)
        result = await self.tool('linux_exec', endpoint_id=lid, command='cat /root/web-terminal.txt')
        self.assertEqual(result['stdout'], 'terminal-through-ssh\n')
        html = '<title>Linux DNS page</title><input autofocus oninput="document.title=this.value">'
        await self.tool('linux_exec', endpoint_id=lid, command=f"printf '%s' '{html}' >/tmp/index.html")
        await asyncio.to_thread(self.manager.docker, 'exec', '-d', self.manager.endpoint_name(lid),
                                'python3', '-m', 'http.server', '8080', '--directory', '/tmp')
        await self.tool('linux_exec', endpoint_id=lid,
                        command='curl --retry 4 --retry-connrefused -fsS http://127.0.0.1:8080/')

        await self.card('Chrome fixture').get_by_role('button', name='起動して開く').click()
        await expect(page.locator('#status')).to_have_text('接続済み', timeout=30000)
        await page.get_by_role('button', name='新しいタブ').click()
        await expect(page.get_by_role('tab')).to_have_count(2)
        await page.get_by_role('textbox', name='アドレス').fill('http://app.endpoint.test:8080/')
        await page.get_by_role('button', name='開く', exact=True).click()
        await expect(page.get_by_role('tab', name='Linux DNS page')).to_be_visible()
        await page.locator('#page-input').focus()
        await page.keyboard.type('browser-through-dns')
        await expect(page.get_by_role('tab', name='browser-through-dns')).to_be_visible()
        tabs = await self.tool('browser_tabs', endpoint_id=bid)
        selected = next(tab for tab in tabs if tab['title'] == 'browser-through-dns')
        image = await self.tool('browser_screen', endpoint_id=bid, tab_id=selected['id'])
        self.assertTrue(image.startswith(b'\x89PNG\r\n\x1a\n'))
        extra = await self.tool('browser_new_tab', endpoint_id=bid)
        await self.tool('browser_navigate', endpoint_id=bid, tab_id=extra['id'],
                        url='http://app.endpoint.test:8080/')
        await self.tool('browser_close_tab', endpoint_id=bid, tab_id=extra['id'])

    @unittest.skipUnless(os.getenv('WINDOWS_E2E_ISO') and os.getenv('WINDOWS_E2E_WINHTTP'),
                         'set WINDOWS_E2E_ISO and WINDOWS_E2E_WINHTTP for the Windows PE fixture')
    async def test_chrome_reaches_windows_http_across_segments(self):
        segments = ['10.96.1.0/28', '10.96.2.0/28']
        topology = await self.tool('vm_topology_save', name='Windows lab', segments=segments)
        tid = topology['id']
        linux = await self.tool('container_create', name='Linux', kind='linux', topology_id=tid, segment=segments[1])
        chrome = await self.tool('container_create', name='Chrome', kind='browser', topology_id=tid, segment=segments[1])
        await self.tool('vm_topology_dns_save', topology_id=tid,
                        records=[{'name': 'linux-peer.endpoint.test', 'address': linux['address']}])
        installer = Path(os.environ['WINDOWS_E2E_ISO']).resolve(strict=True)
        template = await self.tool('vm_template_save', name='WinPE', image_id=installer.name,
                                   vcpus=4, ram_mib=4096, disk_gib=40)
        vm = await self.tool('vm_create', name='Windows HTTP fixture', template_id=template['id'],
                             topology_id=tid, segment=segments[0])
        vm_id = vm['id']
        with patch('vm_manager.build_setup_iso', self.windows_fixture_iso):
            await asyncio.to_thread(self.manager.start_vm, vm_id)
        monitor = Monitor(path=self.manager.vm_dir(vm_id) / 'manage.sock')
        try:
            for _ in range(15):
                await monitor.command('human-monitor-command', {'command-line': 'sendkey ret'})
                await asyncio.sleep(.35)
        finally:
            monitor.close()
        router = self.manager.name('router', tid)
        # Launch the browser while Windows PE is starting.
        await self.tool('container_start', endpoint_id=chrome['id'])
        async with asyncio.timeout(120):
            while True:
                leases = await asyncio.to_thread(self.manager.docker, 'exec', router, 'sh', '-c',
                                                  'cat /tmp/dnsmasq.leases 2>/dev/null || true')
                if leases.strip():
                    address = leases.splitlines()[0].split()[2]
                    try:
                        output = await asyncio.to_thread(self.manager.docker, 'exec',
                            self.manager.endpoint_name(chrome['id']), 'curl', '--max-time', '2',
                            '-fsS', f'http://{address}:8080/')
                    except RuntimeError:
                        pass
                    else:
                        if 'Windows HTTP fixture' in output:
                            break
                await asyncio.sleep(2)
        page = self.page
        await page.goto(self.base + f"/endpoints/{chrome['id']}/console/")
        await expect(page.locator('#status')).to_have_text('接続済み', timeout=20000)
        await page.get_by_role('textbox', name='アドレス').fill(f'http://{address}:8080/')
        await page.get_by_role('button', name='開く', exact=True).click()
        await expect(page.get_by_role('tab', name='Windows HTTP fixture ' + linux['address'])).to_be_visible()

    @staticmethod
    def windows_fixture_iso(state):
        directory = state / 'setup'
        directory.mkdir(parents=True, exist_ok=True)
        reply = directory / 'Autounattend.xml'
        reply.write_text(
            '<unattend xmlns="urn:schemas-microsoft-com:unattend" '
            'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">'
            '<settings pass="windowsPE"><component name="Microsoft-Windows-Setup" '
            'processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" '
            'versionScope="nonSxS"><RunSynchronous><RunSynchronousCommand wcm:action="add">'
            '<Order>1</Order><Path>wpeutil InitializeNetwork</Path></RunSynchronousCommand>'
            '<RunSynchronousCommand wcm:action="add"><Order>2</Order>'
            '<Path>wpeutil DisableFirewall</Path></RunSynchronousCommand>'
            '<RunSynchronousCommand wcm:action="add"><Order>3</Order>'
            '<Path>cmd /c for %i in (D E F G H) do @if exist %i:\\WINHTTP.EXE start "" %i:\\WINHTTP.EXE</Path>'
            '</RunSynchronousCommand></RunSynchronous></component></settings></unattend>'
        )
        iso = pycdlib.PyCdlib()
        iso.new(interchange_level=3, joliet=3, vol_ident='WINDOWS_TEST')
        try:
            iso.add_file(str(reply), iso_path='/AUTOUNATTEND.XML;1', joliet_path='/Autounattend.xml')
            iso.add_file(os.environ['WINDOWS_E2E_WINHTTP'], iso_path='/WINHTTP.EXE;1', joliet_path='/WINHTTP.EXE')
            iso.write(str(directory / 'console-tools.iso'))
        finally:
            iso.close()


if __name__ == '__main__':
    unittest.main()
