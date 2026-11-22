"""Render docs/banner-2026.png (1600x640) from tools/banner.html with Playwright Chromium."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 640}, device_scale_factor=1)
        page.goto((ROOT / "tools" / "banner.html").as_uri())
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(ROOT / "docs" / "banner-2026.png"))
        browser.close()


if __name__ == "__main__":
    main()
