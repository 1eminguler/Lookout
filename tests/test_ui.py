"""Playwright UI smoke tests (enforced when LOOKOUT_REQUIRE_UI_TEST=1; skipped if unavailable otherwise)."""
import os
import shutil
import subprocess
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from lookout import agents, db, scanner, server
from tests.helpers import TempLookoutTestCase

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

_REQUIRE_UI = os.environ.get("LOOKOUT_REQUIRE_UI_TEST") == "1"


def _wait_js(page, expression, timeout=10000):
    """Poll a JS predicate with page.evaluate (wait_for_function is blocked by the strict CSP)."""
    import time
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        try:
            last = page.evaluate(expression)
            if last:
                return last
        except Exception:
            pass
        page.wait_for_timeout(100)
    raise AssertionError(f"timed out waiting for: {expression}; body={page.inner_text('body')[-400:]!r}; url={page.url}")


class FeedUnitTests(unittest.TestCase):
    def run_render_assertions(self, assertions):
        script = """
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const ignore = () => {};
const context = {
  localStorage: { getItem: () => null },
  document: { body: { classList: { add: ignore } }, addEventListener: ignore },
  window: { addEventListener: ignore },
  location: { hash: "#/feed" },
  clearTimeout: ignore,
  fetch: () => new Promise(() => {}),
};
vm.runInNewContext(fs.readFileSync("web/app.js", "utf8"), context);
""" + assertions
        result = subprocess.run(
            ["node", "-e", script], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_shared_url_programmes_render_as_separate_cards(self):
        self.run_render_assertions("""
const url = "https://example.com/careers";
const summer = {id: "one", title: "Summer Internship", url, source_name: "Careers"};
const talent = {id: "two", title: "Young Talent", url, source_name: "Careers"};
const cards = context.dedupeItems([summer, talent, {...summer}])
  .map((item) => context.itemCard(item, {isNew: false}));
assert.equal(cards.length, 2);
assert.match(cards[0], /Summer Internship/);
assert.match(cards[1], /Young Talent/);
""")

    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_deadline_text_and_guessed_year_render_in_cards_and_agenda(self):
        self.run_render_assertions(r"""
const item = {
  id: "one", title: "Internship", url: "https://example.com/one", source_name: "Careers",
  deadline: "2027-03-15", deadline_raw: '15 Mart 17.00 "local" <time>',
  extra: '{"deadline_inferred":true}', action: "saved",
};
for (const markup of [context.itemCard(item, {isNew: false}), context.agRow(item)]) {
  assert.match(markup, /title="15 Mart 17.00 &quot;local&quot; &lt;time&gt;"/);
  assert.match(markup, /<\/time> <small class="year-guessed">year guessed<\/small>/);
}
vm.runInContext('agOpen = "one"', context);
assert.match(context.agRow(item), /Deadline text: 15 Mart 17.00 &quot;local&quot; &lt;time&gt;/);
context.item = item;
vm.runInContext('S = {agenda_items: [item]}; ui.calMonth = "2027-03"; agDay = item.deadline', context);
assert.match(context.renderMonth(), /title="15 Mart 17.00 &quot;local&quot; &lt;time&gt;"/);
assert.match(context.renderMonth(), /year guessed/);

const explicit = {...item, extra: '{"deadline_inferred":false}'};
assert.doesNotMatch(context.itemCard(explicit, {isNew: false}), /year guessed/);
const legacy = {...item, deadline_raw: null, extra: '{"incomplete":'};
assert.doesNotMatch(context.agRow(legacy), /year guessed|Deadline text/);
const unparsed = {...item, deadline: null, deadline_raw: "To be announced", extra: null};
assert.match(context.itemCard(unparsed, {isNew: false}), /title="To be announced"/);
assert.match(context.agRow(unparsed), /Deadline text: To be announced/);
""")


class UiSmokeTests(TempLookoutTestCase):
    def test_auth_views_pagination_inline_edits_csp_and_401_reload(self):
        if sync_playwright is None:
            if _REQUIRE_UI:
                self.fail("playwright is required when LOOKOUT_REQUIRE_UI_TEST=1")
            raise unittest.SkipTest("playwright is not installed")

        db.set_settings({"agent": "claude", "model": "claude-sonnet-medium"})
        wid = db.add_watch("ML Internships", "machine learning internships in berlin")
        sid = db.add_source("EuroTech", "https://eurotech.example.com/jobs", watch_id=wid)
        db.update_source(
            sid,
            extractor="def fetch():\n    return [{'title': 'Sample', 'url': 'https://eurotech.example.com/0'}]\n",
            extractor_ok=1,
            extractor_updated=db.now(),
            filters_changed=1,
        )
        paused_sid = db.add_source("GlobalBoard", "https://global.example.com/jobs")
        db.update_source(paused_sid, paused=1, learn_fail_count=3, extractor_error="paused after repeated failures")
        fid = db.add_filter("include", "machine learning", watch_id=wid)
        db.add_filter("exclude", "unpaid")
        db.log_notification(None, "webhook", False, "HTTP 502 Bad Gateway")

        scan_id = db.start_scan("manual", "claude", "claude-sonnet-medium", [], label="ML Internships", watch_id=wid)
        src = db.source_by_id(sid)
        dl = (date.today() + timedelta(days=10)).isoformat()
        for k in range(55):
            db.save_item(scan_id, src, {
                "id": f"item-{k:02d}",
                "title": f"ML Engineer Intern #{k}",
                "org": "EuroTech Labs",
                "category": "Internship",
                "url": f"https://eurotech.example.com/jobs/{k}",
                "deadline": dl,
                "location": "Berlin",
                "summary": "Work on applied machine learning systems.",
            }, match=1, match_key="k1")
        db.set_item("item-00", action="saved", remind_on=dl, note="Prepare CV")
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
            if _REQUIRE_UI:
                self.fail(f"Chromium is required when LOOKOUT_REQUIRE_UI_TEST=1: {e}")
            raise unittest.SkipTest(f"Chromium unavailable: {e}")

        patches = [
            patch.object(agents, "detect", return_value=[
                {"id": "claude", "name": "Claude Code", "installed": True, "version": "1.0.0",
                 "verified": True, "lists_models": False, "usage": [], "configured": False},
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
        base = f"http://127.0.0.1:{port}/"

        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            # 1. Unauthenticated / shows gate page without lookout-token meta tag
            page.goto(base, wait_until="networkidle")
            self.assertIn("Open Lookout from the app", page.inner_text("body"))
            self.assertIn("Open Lookout from the app or its shortcut, then use this link again.", page.inner_text("body"))
            self.assertIsNone(page.query_selector('meta[name="lookout-token"]'))

            # 2. One-time launch secret exchanges for HttpOnly SameSite=Strict cookie and redirects to /
            secret = srv.launch_secret
            page.goto(f"{base}?k={secret}", wait_until="networkidle")
            _wait_js(page, "() => document.querySelector('h1') && document.querySelector('h1').textContent.includes('What should Lookout watch')")
            self.assertIsNone(page.query_selector('meta[name="lookout-token"]'))
            cookies = {c["name"]: c for c in ctx.cookies()}
            self.assertIn("lookout_session", cookies)
            self.assertTrue(cookies["lookout_session"]["httpOnly"])
            self.assertEqual(cookies["lookout_session"]["sameSite"], "Strict")

            # Replaying same secret in a fresh unauthenticated context fails with 401
            ctx_replay = browser.new_context()
            try:
                page_replay = ctx_replay.new_page()
                resp = page_replay.goto(f"{base}?k={secret}", wait_until="networkidle")
                self.assertEqual(resp.status, 401)
                self.assertIn("Open Lookout from the app", page_replay.inner_text("body"))
            finally:
                ctx_replay.close()

            # 3. Notification error banner + dismiss
            page.wait_for_selector('[data-act="dismiss-notify-errors"]')
            page.click('[data-act="dismiss-notify-errors"]')
            _wait_js(page, "() => !document.querySelector('[data-act=\"dismiss-notify-errors\"]')")

            # 4. Model picker and effort submenu
            page.click('[data-pick="model"]')
            page.wait_for_selector(".pop button.fam")
            page.click(".pop button.fam")
            _wait_js(page, "() => document.querySelectorAll('.pop').length === 2")
            submenu_text = page.locator(".pop").nth(1).inner_text()
            self.assertIn("Low", submenu_text)
            self.assertIn("Medium", submenu_text)
            self.assertIn("High", submenu_text)
            page.keyboard.press("Escape")

            # 5. Visit every view & verify round-3 features under strict CSP
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
                _wait_js(page, 
                    f"() => document.querySelector('h1') && document.querySelector('h1').textContent.includes({expected!r})"
                )
                if route_hash == "#/feed":
                    page.wait_for_selector('[data-act="load-more"]')
                    self.assertEqual(page.locator("article.item").count(), 50)
                    self.assertIn("50 of 55", page.locator(".load-more").inner_text())
                    page.click('[data-act="load-more"]')
                    _wait_js(page, "() => document.querySelectorAll('article.item').length === 55")
                elif route_hash == "#/agenda":
                    page.click('[data-agview="month"]')
                    page.wait_for_selector(".cal")
                elif route_hash == "#/sources":
                    page.wait_for_selector(f'[data-src-resume="{paused_sid}"]')
                    src_text = page.locator(".page").inner_text()
                    self.assertIn("paused", src_text)
                    self.assertIn("filters changed", src_text)
                    # On-demand reader code load
                    page.click(f'details[data-code-id="{sid}"] summary')
                    page.wait_for_selector(f'details[data-code-id="{sid}"] .code')
                    self.assertIn("def fetch()", page.locator(f'details[data-code-id="{sid}"] .code').inner_text())
                    # Inline edit source
                    page.click(f'[data-src-edit="{sid}"]')
                    page.wait_for_selector("#editSrcForm")
                    page.fill('#editSrcForm input[name="notes"]', "Updated notes")
                    page.click('#editSrcForm button.primary')
                    _wait_js(page, "() => !document.querySelector('#editSrcForm')")
                    # Cancelled source test shows "Test stopped" without preview items
                    with server._test_lock:
                        server.test_job.update(
                            id="ui-cancel-1",
                            running=False,
                            status="cancelled",
                            source_id=sid,
                            title="EuroTech",
                            result={"ok": False, "cancelled": True, "status": "cancelled", "method": "plugin",
                                    "count": 0, "matching": 0, "items": [], "error": "cancelled"},
                            error="cancelled",
                            finished=db.now(),
                        )
                    page.evaluate("() => refresh(false)")
                    _wait_js(page, "() => document.querySelector('.test-preview') && document.querySelector('.test-preview').textContent.includes('Test stopped')")
                    self.assertNotIn("Preview:", page.locator(".test-preview").inner_text())
                    page.click('[data-act="close-preview"]')
                    _wait_js(page, "() => !document.querySelector('.test-preview')")
                elif route_hash == "#/filters":
                    # Global filter row or navigate to watch for watch filter; verify add/inline edit on global filter
                    gf_edit = page.locator('[data-f-edit]').first
                    gf_edit.click()
                    page.wait_for_selector("#editFilterForm")
                    page.click('#editFilterForm [data-act="cancel-edit"]')
                    _wait_js(page, "() => !document.querySelector('#editFilterForm')")
                elif route_hash == "#/settings":
                    page.wait_for_selector("#webhookUrl")
                    page.wait_for_selector("#webhookFormat")
                    page.wait_for_selector("#mailFrom")
                    page.wait_for_selector("#smtpStarttls")
                    page.wait_for_selector("#rssPublicIn")
                    page.click('[data-act="sched-preview"]')
                    page.wait_for_selector("pre")
                elif route_hash == f"#/watch/{wid}":
                    rss_text = page.locator(".rss-bar").inner_text()
                    self.assertIn("localhost", rss_text.lower())
                    self.assertNotIn("local network", rss_text.lower())
                    page.click(f'[data-watch-edit="{wid}"]')
                    page.wait_for_selector("#editWatchForm")
                    page.wait_for_selector(f'#editWatchForm [data-watch-rediscover="{wid}"]')
                    page.click('#editWatchForm [data-act="cancel-edit"]')
                    _wait_js(page, "() => !document.querySelector('#editWatchForm')")
                    self.assertTrue(page.locator(f'[data-f-edit="{fid}"]').count() >= 1)

            # 5b. Opening #/feed on a fresh load does not issue duplicate GET requests
            feed_hits = []
            page_dedup = ctx.new_page()
            try:
                page_dedup.on("request", lambda r: feed_hits.append(r.url.split(f":{port}", 1)[-1]) if "/api/" in r.url else None)
                page_dedup.goto(base + "#/feed", wait_until="networkidle")
                _wait_js(page_dedup, "() => document.querySelectorAll('article.item').length === 50")
                self.assertEqual(feed_hits.count("/api/state"), 1)
                self.assertEqual(feed_hits.count("/api/agents"), 1)
                self.assertEqual(feed_hits.count("/api/agents/claude/models"), 1)
                items_hits = [u for u in feed_hits if u.startswith("/api/items?")]
                self.assertEqual(len(items_hits), 1)
            finally:
                page_dedup.close()

            # 6. Scroll position survives background /api/progress refresh during a fake running scan
            scanner.progress.update(running=True, scan_id=scan_id, done=1, total=5, current=["EuroTech"])
            page.goto(base + "#/feed", wait_until="networkidle")
            _wait_js(page, 
                "() => document.querySelector('.progress') && document.querySelector('.progress').textContent.includes('1 of 5')"
            )
            page.evaluate("() => { document.querySelector('#main').scrollTop = 400; }")
            before_scroll = page.evaluate("() => document.querySelector('#main').scrollTop")
            self.assertGreaterEqual(before_scroll, 380)

            scanner.progress["done"] = 2
            _wait_js(page, 
                "() => document.querySelector('.progress') && document.querySelector('.progress').textContent.includes('2 of 5')",
                timeout=6000,
            )
            after_scroll = page.evaluate("() => document.querySelector('#main').scrollTop")
            self.assertGreaterEqual(after_scroll, 350)
            scanner.progress.update(running=False, scan_id=None, done=0, total=0, current=[])

            # Zero console/page errors across all normal interactions & strict CSP
            self.assertEqual(console_errors, [])
            self.assertEqual(page_errors, [])

            # 7. 401 session expiry triggers clean reload to gate page (NOT "engine stopped"),
            #    and rapid repeat within 5s shows "Session expired"
            page.goto(base + "#/sources", wait_until="networkidle")
            page.wait_for_selector(f'[data-src-resume="{paused_sid}"]')
            with srv.auth_lock:
                srv.session_tokens.clear()
            page.click(f'[data-src-resume="{paused_sid}"]')
            _wait_js(page, "() => document.body && document.body.innerText.includes('Open Lookout from the app')")
            body_after_401 = page.inner_text("body")
            self.assertIn("Open Lookout from the app", body_after_401)
            self.assertNotIn("engine stopped", body_after_401.lower())

            sec2 = srv.issue_launch_secret()
            page.goto(f"{base}?k={sec2}", wait_until="networkidle")
            _wait_js(page, "() => document.querySelector('h1') && document.querySelector('h1').textContent.includes('What should Lookout watch')")
            page.goto(base + "#/sources", wait_until="networkidle")
            page.wait_for_selector(f'[data-src-resume="{paused_sid}"]')
            with srv.auth_lock:
                srv.session_tokens.clear()
            page.wait_for_timeout(300)
            page.evaluate('() => sessionStorage.setItem("lookout-reauth-ts", String(Date.now()))')
            page.click(f'[data-src-resume="{paused_sid}"]')
            _wait_js(page, 
                "() => document.querySelector('#main') && document.querySelector('#main').textContent.includes('Session expired')"
            )
            self.assertNotIn("engine stopped", page.locator("#main").inner_text().lower())
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
