"""Live MCP-to-Chrome smoke test. Shows brief captions on the running console.
Run: uv run --project console/mcp --with playwright python console/tests/walkthrough_browser.py
Guest display and input sockets in the test tab are intercepted.
Pass --move-guest to additionally verify one click-free MCP move against the real VM.
"""
import argparse
import asyncio
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from control_config import configured_port


async def exercise(client, browser, move_guest):
    page = await browser.new_page(viewport={'width': 1280, 'height': 800})
    await page.route_web_socket('**/display', lambda ws: None)
    await page.route_web_socket('**/input', lambda ws: ws.send('{"ready":true}'))
    await page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
        const canvas = document.createElement('canvas');
        canvas.id = 'spice_surface_0';
        canvas.width = 1024;
        canvas.height = 768;
        document.getElementById('display').append(canvas);
    })""")
    await page.goto(f'http://127.0.0.1:{configured_port()}/')
    await expect(page.locator('#status')).to_have_text('接続済み')
    await page.locator('#surface').focus()
    layer = page.locator('#walkthrough')
    caption = page.locator('#demo-caption')
    text = page.locator('#demo-text')
    progress = page.locator('#demo-time')

    async def show(message, seconds, **position):
        args = dict(x=32767, y=32767, pointer_x=8192, pointer_y=16384,
                    text=message, duration_seconds=seconds)
        result = await client.call_tool('console_demo_show', {**args, **position})
        assert not result.isError, result.content
        if message:
            await expect(text).to_have_text(message)
        await expect(layer).to_be_visible()

    async def check_position():
        # Wait for the ResizeObserver after resizing or entering fullscreen.
        await page.wait_for_function("""() => {
            const rect = id => document.getElementById(id).getBoundingClientRect();
            const frame = rect('frame'), box = rect('demo-caption'), tip = rect('demo-pointer');
            return frame.width > 0 && Math.abs(frame.right - box.right) < 2
                && Math.abs(frame.bottom - box.bottom) < 2
                && Math.abs(tip.x - frame.x - frame.width * 8192/32767) < 1
                && Math.abs(tip.y - frame.y - frame.height * 16384/32767) < 1;
        }""")

    async def fraction():
        return await progress.evaluate("""el => el.getBoundingClientRect().width /
            el.parentElement.getBoundingClientRect().width""")

    try:
        await show('次の説明です', 1)
        message = '右下を確認 <em>保存</em>'
        await show(message, 4)
        assert await text.locator('em').count() == 0
        await check_position()
        assert await page.evaluate("document.activeElement.id") == 'ime'
        assert await caption.evaluate("""el => {
            const r = el.getBoundingClientRect();
            return document.elementFromPoint(r.x + 5, r.y + 5).id;
        }""") == 'surface'
        start = await fraction()
        assert 0.75 < start <= 1
        await page.set_viewport_size({'width': 760, 'height': 520})
        await check_position()
        await page.locator('#full').click()
        await page.wait_for_function('!!document.fullscreenElement')
        await check_position()
        await page.wait_for_function("""() => {
            const bar = document.getElementById('demo-time');
            return bar.getBoundingClientRect().width / bar.parentElement.clientWidth < 0.62;
        }""")
        assert await text.inner_text() == message
        before = await fraction()
        # A new real EventSource connection receives only the unexpired portion.
        await page.reload()
        await expect(text).to_have_text(message)
        assert 0 < await fraction() <= before + 0.02
        await expect(layer).to_be_hidden(timeout=3500)
        await expect(text).to_have_text('')
        await show('', 5)
        await expect(caption).to_be_hidden()
        assert await page.locator('#demo-pointer').evaluate(
            "el => getComputedStyle(el, '::after').clipPath !== 'none'")
        if move_guest:
            result = await client.call_tool('console_pointer', {'action': 'move', 'x': 11000, 'y': 13000})
            assert not result.isError, result.content
            await page.wait_for_function("""() => {
                const frame = document.getElementById('frame').getBoundingClientRect();
                const tip = document.getElementById('demo-pointer').getBoundingClientRect();
                return !document.getElementById('walkthrough').hidden
                    && Math.abs(tip.x - frame.x - frame.width * 11000/32767) < 1
                    && Math.abs(tip.y - frame.y - frame.height * 13000/32767) < 1;
            }""", timeout=3500)
    finally:
        result = await client.call_tool('console_demo_clear', {})
        assert not result.isError, result.content
        await expect(layer).to_be_hidden()
        await page.close()


async def main(move_guest):
    params = StdioServerParameters(command=str(ROOT/'mcp/start.sh'))
    async with stdio_client(params) as streams, ClientSession(*streams) as client:
        await client.initialize()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel='chrome')
            try:
                await exercise(client, browser, move_guest)
            finally:
                await browser.close()
    print('MCP → Chrome caption, position, input focus, countdown, resize/fullscreen, rejoin and clear passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--move-guest', action='store_true', help='move the real VM pointer once')
    asyncio.run(main(parser.parse_args().move_guest))
