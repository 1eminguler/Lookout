"""Build the application icons from web/icon.svg using Playwright Chromium."""
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 24, 32, 48, 64, 256)


def main():
    from playwright.sync_api import sync_playwright
    svg = (ROOT / "web/icon.svg").read_text(encoding="utf-8-sig")
    images = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for size in SIZES:
            page = browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
            page.set_content(f'<html><head><style>html,body{{margin:0}} svg{{display:block;width:{size}px;height:{size}px}}</style></head><body>{svg}</body></html>')
            images.append(page.screenshot(omit_background=True))
            page.close()
        browser.close()
    offset = 6 + 16 * len(images)
    entries = []
    for size, png in zip(SIZES, images):
        entries.append(struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(png), offset))
        offset += len(png)
    (ROOT / 'web/icon.ico').write_bytes(struct.pack('<HHH', 0, 1, len(images)) + b''.join(entries) + b''.join(images))


if __name__ == '__main__':
    main()

