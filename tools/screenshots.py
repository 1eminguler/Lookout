"""Capture the classic README tour using a disposable fictional database."""
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT))


def main():
    from playwright.sync_api import sync_playwright
    from demo_data import DEMO_DATE, build_demo
    with tempfile.TemporaryDirectory(prefix='lookout-demo-') as directory:
        os.environ['LOOKOUT_DATA_DIR'] = directory
        os.environ['LOOKOUT_BIND'] = '127.0.0.1'
        build_demo()
        plugin_dir = Path(directory) / 'plugins'
        plugin_dir.mkdir()
        os.environ['LOOKOUT_PLUGIN_DIR'] = str(plugin_dir)
        from lookout import server
        srv = server.make_server(port=0)
        if srv.server_address[1] == 8780:
            srv.server_close()
            srv = server.make_server(port=0)
            if srv.server_address[1] == 8780:
                srv.server_close()
                raise RuntimeError('Could not obtain a non-8780 port')
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            output = ROOT / 'docs' / 'screenshots'
            output.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context(viewport={'width': 1280, 'height': 800}, device_scale_factor=2, color_scheme='dark')
                page = context.new_page()
                page.clock.install(time=DEMO_DATE + 'T10:00:00')
                base = f'http://127.0.0.1:{srv.server_address[1]}/'
                page.goto(base + '?k=' + srv.launch_secret, wait_until='networkidle')
                page.goto(base + '#/feed', wait_until='networkidle')
                page.get_by_text('Embedded Software Summer Internship', exact=True).first.wait_for()
                page.screenshot(path=str(output / 'feed-2026.png'))
                page.goto(base + '#/sources', wait_until='networkidle')
                page.get_by_role('heading', name='Kuzey Careers', exact=False).wait_for()
                page.screenshot(path=str(output / 'sources-2026.png'))
                page.goto(base + '#/agenda', wait_until='networkidle')
                page.get_by_text('Embedded Software Summer Internship', exact=True).first.click()
                page.locator('textarea').filter(has_text='Prepare the project summary').first.wait_for()
                page.screenshot(path=str(output / 'agenda-2026.png'))
                page.goto(base + '#/settings', wait_until='networkidle')
                page.locator('#themeSel').select_option('light')
                page.locator('html[data-theme="light"]').wait_for()
                page.locator('#themeSel').blur()
                page.screenshot(path=str(output / 'settings-2026.png'))
                browser.close()
        finally:
            srv.shutdown()
            thread.join()
            srv.server_close()


if __name__ == '__main__':
    main()
