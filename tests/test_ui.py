"""Optional Playwright UI smoke test (skipped if playwright or Chromium is unavailable)."""
import threading
import time
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from lookout import agents, db, scanner, server
from tests.helpers import TempLookoutTestCase

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


class UiSmokeTests(TempLookoutTestCase):
    @unittest.skipIf(sync_playwright is None, "playwright is not installed")
    def test_views_model_picker_and_scroll_survival(self):
        db.set_settings({"agent": "claude", "model": "claude-sonnet-medium"})
        wid = db.add_watch("ML Internships", "machine learning internships in berlin")
        sid = db.add_source("EuroTech", "https://eurotech.example.com/jobs", watch_id=wid)
        db.add_source("GlobalBoard", "https://global.example.com/jobs")
        db.add_filter("include", "machine learning", watch_id=wid)
        db.add_filter("exclude", "unpaid")

        scan_id = db.start_scan("manual", "claude", "claude-sonnet-medium", [], label="ML Internships", watch_id=wid)
        src = db.source_by_id(sid)
        dl = (date.today() + timedelta(days=10)).isoformat()
        for k in range(36):
            db.save_item(scan_id, src, {
                "id": f"item-{k}",
                "title": f"ML Engineer Intern #{k}",
                "org": "EuroTech Labs",
                "category": "Internship",
                "url": f"https://eurotech.example.com/jobs/{k}",
                "deadline": dl,
                "location": "Berlin",
                "summary": "Work on applied machine learning systems.",
            })
        db.set_item("item-0", action="saved", remind_on=dl, note="Prepare CV")
        db.finish_scan(scan_id, "done")
        did = db.start_discovery("machine learning internships in berlin", "ML Internships",
                                 "claude", "claude-sonnet-medium", watch_id=wid)
        db.finish_discovery(did, "done", [
            {"name": "EuroTech", "url": "https://eurotech.example.com/jobs", "why": "Relevant listings"},
        ])

        p_cm = sync_playwright()
        pw = p_cm.start()
        try:
            browser = pw.chromium.launch()
        except Exception as e:
            pw.stop()
            raise unittest.SkipTest(f"Chromium unavailable: {e}")

        patches = [
            patch.object(agents, "detect", return_value=[
                {"id": "claude", "name": "Claude Code", "installed": True, "version": "1.0.0",
                 "verified": True, "lists_models": False, "usage": []},
            ]),
            patch.object(agents, "browser_tool", return_value={"installed": True, "version": "1.0.0", "install": ""}),
            patch.object(agents, "models", return_value=[
                ("claude-sonnet-low", "Claude Sonnet (Low)"),
                ("claude-sonnet-medium", "Claude Sonnet (Medium)"),
                ("claude-sonnet-high", "Claude Sonnet (High)"),
                ("claude-haiku", "Claude Haiku"),
            ]),
            patch.object(agents, "which", side_effect=lambda a: "/bin/claude" if a == "claude" else None),
        ]
        for p in patches:
            p.start()

        srv = server.make_server(port=0)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        console_errors = []
        page_errors = []

        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            base = f"http://127.0.0.1:{port}/"
            page.goto(base + "#/home", wait_until="networkidle")
            page.wait_for_selector("h1")

            # Model picker and effort submenu
            page.click('[data-pick="model"]')
            page.wait_for_selector(".pop button.fam")
            page.click(".pop button.fam")
            page.wait_for_function("document.querySelectorAll('.pop').length === 2")
            submenu_text = page.locator(".pop").nth(1).inner_text()
            self.assertIn("Low", submenu_text)
            self.assertIn("Medium", submenu_text)
            self.assertIn("High", submenu_text)
            page.keyboard.press("Escape")

            # Visit every view
            for route_hash, expected in (
                ("#/feed", "Feed"),
                ("#/agenda", "Agenda"),
                ("#/sources", "Sources"),
                ("#/filters", "Filters"),
                ("#/settings", "Settings"),
                (f"#/watch/{wid}", "ML Internships"),
                (f"#/scan/{scan_id}", "ML Internships"),
                (f"#/search/{did}", "ML Internships"),
            ):
                page.goto(base + route_hash, wait_until="networkidle")
                page.wait_for_function(
                    f"document.querySelector('h1') && document.querySelector('h1').textContent.includes({expected!r})"
                )
                if route_hash == "#/agenda":
                    page.click('[data-agview="month"]')
                    page.wait_for_selector(".cal")
                elif route_hash == "#/settings":
                    page.wait_for_selector("#webhookUrl")
                    page.wait_for_selector("#rssPublicIn")
                    page.click('[data-act="sched-preview"]')
                    page.wait_for_selector("pre")

            # Scroll position survives a background refresh during a fake running scan
            scanner.progress.update(running=True, scan_id=scan_id, done=1, total=5, current=["EuroTech"])
            page.goto(base + "#/feed", wait_until="networkidle")
            page.wait_for_function(
                "document.querySelector('.progress') && document.querySelector('.progress').textContent.includes('1 of 5')"
            )
            page.evaluate("document.querySelector('#main').scrollTop = 400")
            before_scroll = page.evaluate("document.querySelector('#main').scrollTop")
            self.assertGreaterEqual(before_scroll, 380)

            scanner.progress["done"] = 2
            page.wait_for_function(
                "document.querySelector('.progress') && document.querySelector('.progress').textContent.includes('2 of 5')",
                timeout=6000,
            )
            after_scroll = page.evaluate("document.querySelector('#main').scrollTop")
            self.assertGreaterEqual(after_scroll, 350)
            scanner.progress.update(running=False, scan_id=None, done=0, total=0, current=[])

            self.assertEqual(console_errors, [])
            self.assertEqual(page_errors, [])
        finally:
            scanner.progress.update(running=False, scan_id=None, done=0, total=0, current=[])
            browser.close()
            pw.stop()
            srv.shutdown()
            srv.server_close()
            th.join(timeout=3)
            for p in patches:
                p.stop()


if __name__ == "__main__":
    unittest.main()
