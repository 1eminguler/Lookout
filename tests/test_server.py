"""Integration tests for lookout.server HTTP API & security boundaries."""
import http.client
import json
import re
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.request
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from lookout import agents, db, notify, scanner, server, tasks
from tests.helpers import TempLookoutTestCase


class ServerTests(TempLookoutTestCase):
    def setUp(self):
        super().setUp()
        self._agent_patches = [
            patch.object(agents, "detect", return_value=[
                {"id": "claude", "name": "Claude Code", "installed": True, "version": "1.0.0",
                 "verified": True, "lists_models": False, "usage": []},
            ]),
            patch.object(agents, "browser_tool", return_value={"installed": True, "version": "1.0.0", "install": ""}),
            patch.object(agents, "models", return_value=[("sonnet", "Claude Sonnet")]),
            patch.object(agents, "which", side_effect=lambda a: "/bin/claude" if a == "claude" else None),
        ]
        for p in self._agent_patches:
            p.start()
        self.srv = server.make_server(port=0)
        self.port = self.srv.server_address[1]
        self.token = self.srv.api_token
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=3)
        for p in self._agent_patches:
            p.stop()
        super().tearDown()

    def req(self, method: str, path: str, body=None, token="USE_DEFAULT", headers=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        hdrs = dict(headers or {})
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        if token == "USE_DEFAULT":
            hdrs["X-Lookout-Token"] = self.token
        elif token is not None:
            hdrs["X-Lookout-Token"] = token
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                raw = resp.read().decode("utf-8")
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(raw) if "json" in ctype else raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            code = e.code
            e.close()
            try:
                return code, json.loads(raw)
            except ValueError:
                return code, raw

    def test_startup_marks_running_as_interrupted(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=3)

        s_id = db.start_scan("manual", "claude", "", [])
        d_id = db.start_discovery("old search", "Old search", "claude", "")
        self.srv = server.make_server(port=0)
        self.port = self.srv.server_address[1]
        self.token = self.srv.api_token
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

        self.assertEqual(db.scan_detail(s_id)["status"], "interrupted")
        self.assertEqual(db.discovery_detail(d_id)["status"], "interrupted")

    def test_api_token_401s_and_public_static_files(self):
        # Static files remain public and index.html contains the per-launch token meta tag
        code, html = self.req("GET", "/", token=None)
        self.assertEqual(code, 200)
        m = re.search(r'<meta name="lookout-token" content="([^"]+)">', html)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), self.token)
        self.assertEqual(self.req("GET", "/app.js", token=None)[0], 200)
        self.assertEqual(self.req("GET", "/app.css", token=None)[0], 200)

        # Every /api/* route rejects missing or wrong tokens with 401
        for method, path, body in (
            ("GET", "/api/state", None),
            ("GET", "/api/items", None),
            ("GET", "/api/agents", None),
            ("GET", "/api/export.csv", None),
            ("GET", "/api/agenda.ics", None),
            ("GET", "/api/backup.json", None),
            ("POST", "/api/sources", None),
            ("POST", "/api/settings", None),
            ("POST", "/api/scan", None),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.req(method, path, body, token=None)[0], 401)
                self.assertEqual(self.req(method, path, body, token="wrong-token")[0], 401)

        # Valid token succeeds
        self.assertEqual(self.req("GET", "/api/state", token=m.group(1))[0], 200)

    def test_host_header_and_origin_security_and_bad_payloads(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", "/api/state", skip_host=True)
        conn.putheader("Host", f"evil.example:{self.port}")
        conn.putheader("X-Lookout-Token", self.token)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

        for bad_origin in ("https://evil.com", f"https://evil.com/http://127.0.0.1:{self.port}"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", "/api/sources", body="", headers={
                "Host": f"127.0.0.1:{self.port}",
                "Origin": bad_origin,
                "X-Lookout-Token": self.token,
            })
            self.assertEqual(conn.getresponse().status, 403)
            conn.close()

        # Invalid JSON and non-object JSON
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/sources", body="{not-json", headers={
            "Content-Type": "application/json",
            "X-Lookout-Token": self.token,
        })
        self.assertEqual(conn.getresponse().status, 400)
        conn.request("POST", "/api/sources", body="[1, 2]", headers={
            "Content-Type": "application/json",
            "X-Lookout-Token": self.token,
        })
        self.assertEqual(conn.getresponse().status, 400)
        # Negative Content-Length
        conn.putrequest("POST", "/api/sources")
        conn.putheader("X-Lookout-Token", self.token)
        conn.putheader("Content-Length", "-5")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_custom_command_confirm_and_metacharacter_rejection(self):
        # Without confirm_custom_command=True -> 403
        self.assertEqual(self.req("POST", "/api/settings", {"custom_command": "mycli --yes"})[0], 403)
        self.assertEqual(self.req("POST", "/api/settings", {
            "custom_command": "mycli --yes",
            "confirm_custom_command": False,
        })[0], 403)

        # Shell metacharacter chains rejected with 400 even when confirmed
        for bad in (
            "mycli --yes && id",
            "mycli --yes | cat",
            "mycli --yes; whoami",
            "mycli `id`",
            "mycli $(whoami)",
        ):
            with self.subTest(bad=bad):
                code, res = self.req("POST", "/api/settings", {
                    "custom_command": bad,
                    "confirm_custom_command": True,
                })
                self.assertEqual(code, 400)
                self.assertIn("metacharacter", res["error"].lower())

        # Clean command with confirmation succeeds
        code, _ = self.req("POST", "/api/settings", {
            "custom_command": "mycli run --yes {prompt}",
            "confirm_custom_command": True,
        })
        self.assertEqual(code, 200)
        self.assertEqual(db.get_settings()["custom_command"], "mycli run --yes {prompt}")

    def test_csv_formula_and_ics_injection_escaping(self):
        sid = db.add_source("SecBoard", "https://sec.example.com")
        src = db.source_by_id(sid)
        scan_id = db.start_scan("manual", "claude", "", [])
        dl = (date.today() + timedelta(days=3)).isoformat()
        rm = (date.today() + timedelta(days=1)).isoformat()

        for idx, prefix in enumerate(("=", "+", "-", "@", "\t", "\r")):
            db.save_item(scan_id, src, {
                "id": f"inj-{idx}",
                "title": f"{prefix}HYPERLINK(\"http://evil\",\"click\")",
                "org": f"{prefix}ORG",
                "url": f"https://sec.example.com/{idx}",
                "deadline": dl,
            })
        # Also test ICS CR/LF injection and line folding on a saved item
        long_title = "=SUM(A1:A2) " + ("LongTitleWord " * 8) + "\r\nATTENDEE:evil"
        db.save_item(scan_id, src, {
            "id": "ics-inj",
            "title": long_title,
            "org": "Acme; Corp, Inc\\Team",
            "url": "https://sec.example.com/ics",
            "deadline": dl,
        })
        db.set_item("ics-inj", action="saved", remind_on=rm, note="Line1\r\nFAKEPROP:1")
        db.set_matches({f"inj-{i}": True for i in range(6)} | {"ics-inj": True}, "k")
        db.finish_scan(scan_id, "done")

        code, csv_txt = self.req("GET", "/api/export.csv")
        self.assertEqual(code, 200)
        for prefix in ("=", "+", "-", "@", "\t", "\r"):
            self.assertIn(f"'{prefix}HYPERLINK", csv_txt)
            self.assertIn(f"'{prefix}ORG", csv_txt)

        code_ics, ics_txt = self.req("GET", "/api/agenda.ics")
        self.assertEqual(code_ics, 200)
        raw_lines = ics_txt.split("\r\n")
        # UTC DTSTAMP ends with Z
        stamps = [ln for ln in raw_lines if ln.startswith("DTSTAMP:")]
        self.assertTrue(stamps and all(ln.endswith("Z") for ln in stamps))
        # No injected property lines from CR/LF inside title or note
        self.assertFalse(any(ln.startswith("ATTENDEE:") for ln in raw_lines))
        self.assertFalse(any(ln.startswith("FAKEPROP:") for ln in raw_lines))
        # All lines obey RFC 5545 75-octet fold limit
        for ln in raw_lines:
            self.assertLessEqual(len(ln.encode("utf-8")), 75)

    def test_static_state_agents_watches_sources_filters_items_settings_exports(self):
        code, html = self.req("GET", "/", token=None)
        self.assertEqual(code, 200)
        self.assertIn("Lookout", html)
        code, js = self.req("GET", "/app.js", token=None)
        self.assertEqual(code, 200)
        self.assertIn("renderWatch", js)
        self.assertEqual(self.req("GET", "/missing.txt", token=None)[0], 404)

        code, ag = self.req("GET", "/api/agents")
        self.assertEqual(code, 200)
        self.assertEqual(ag["agents"][0]["id"], "claude")
        code, mods = self.req("GET", "/api/agents/claude/models")
        self.assertEqual(code, 200)
        self.assertEqual(mods["models"], [["sonnet", "Claude Sonnet"]])

        # Watches CRUD & validation
        self.assertEqual(self.req("POST", "/api/watches", {"title": ""})[0], 400)
        code, w = self.req("POST", "/api/watches", {"title": "Internships", "request": "internships"})
        self.assertEqual(code, 200)
        wid = w["id"]
        self.assertEqual(self.req("GET", f"/api/watches/{wid}")[0], 200)
        self.assertEqual(self.req("GET", "/api/watches/9999")[0], 404)
        self.assertEqual(self.req("POST", f"/api/watches/{wid}", {"title": "CS Internships"})[0], 200)

        # Sources CRUD & validation
        self.assertEqual(self.req("POST", "/api/sources", {"name": "X", "url": "ftp://bad"})[0], 400)
        code, sr = self.req("POST", "/api/sources", {"name": "Careers", "url": "https://careers.example.com", "watch_id": wid})
        self.assertEqual(code, 200)
        sid = sr["id"]
        self.assertEqual(self.req("POST", f"/api/sources/{sid}", {"notes": "use filter", "relearn": True})[0], 200)
        self.assertEqual(self.req("POST", "/api/sources/9999", {"notes": "x"})[0], 404)

        # Filters CRUD & validation
        self.assertEqual(self.req("POST", "/api/filters", {"kind": "deadline_within", "value": "abc"})[0], 400)
        code, fr = self.req("POST", "/api/filters", {"kind": "include", "value": "python", "watch_id": wid})
        self.assertEqual(code, 200)
        fid = fr["id"]
        self.assertEqual(self.req("POST", f"/api/filters/{fid}", {"active": False})[0], 200)

        # Items, exports, backup/import
        src = db.source_by_id(sid)
        scan_id = db.start_scan("manual", "claude", "", [], watch_id=wid)
        global_scan_id = db.start_scan("manual", "claude", "", [])
        db.finish_scan(global_scan_id, "done")
        dl = (date.today() + timedelta(days=5)).isoformat()
        rm = (date.today() + timedelta(days=2)).isoformat()
        db.save_item(scan_id, src, {"id": "it_ok", "title": "Python Intern", "org": "Acme",
                                     "url": "https://careers.example.com/1", "deadline": dl})
        db.save_item(scan_id, src, {"id": "it_no", "title": "Sales Rep",
                                     "url": "https://careers.example.com/2", "deadline": dl})
        db.set_matches({"it_ok": True, "it_no": False}, "k1")
        db.finish_scan(scan_id, "done")

        self.assertEqual(self.req("POST", "/api/items/it_ok", {"action": "bad"})[0], 400)
        self.assertEqual(self.req("POST", "/api/items/it_ok", {"remind_on": "not-a-date"})[0], 400)
        self.assertEqual(self.req("POST", "/api/items/it_ok", {"action": "saved", "remind_on": rm, "snoozed_until": rm})[0], 200)
        self.assertEqual(self.req("GET", f"/api/scans/{scan_id}")[0], 200)

        code, csv_txt = self.req("GET", "/api/export.csv")
        self.assertEqual(code, 200)
        self.assertIn("Python Intern", csv_txt)
        self.assertNotIn("Sales Rep", csv_txt)

        code, ics_txt = self.req("GET", "/api/agenda.ics")
        self.assertEqual(code, 200)
        self.assertIn("BEGIN:VCALENDAR", ics_txt)
        self.assertIn("Deadline: Python Intern", ics_txt)
        self.assertIn("Reminder: Python Intern", ics_txt)

        # Settings & secret handling
        self.assertEqual(self.req("POST", "/api/settings", {"agent": "unknown"})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"parallel": 99})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"agent": "claude", "smtp_password": "secret-pw"})[0], 200)
        self.assertEqual(self.req("POST", "/api/settings", {"smtp_password": "", "parallel": "3"})[0], 200)
        code, st = self.req("GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertNotIn("smtp_password", st["settings"])
        self.assertTrue(st["settings"]["smtp_password_set"])
        self.assertEqual(db.get_settings()["smtp_password"], "secret-pw")
        self.assertEqual(len(st["watches"]), 1)

        # Backup and idempotent import (no secrets)
        code, bkp = self.req("GET", "/api/backup.json")
        self.assertEqual(code, 200)
        self.assertNotIn("smtp_password", json.dumps(bkp))
        code, imp = self.req("POST", "/api/import", bkp)
        self.assertEqual(code, 200)
        self.assertEqual(imp["added"], {"watches": 0, "sources": 0, "filters": 0})
        self.assertEqual(self.req("POST", f"/api/watches/{wid}/delete", {})[0], 200)
        # Cascade delete removes the watch's scan as well
        self.assertEqual(self.req("GET", f"/api/scans/{scan_id}")[0], 404)
        code, imp2 = self.req("POST", "/api/import", bkp)
        self.assertEqual(code, 200)
        self.assertEqual(imp2["added"], {"watches": 1, "sources": 1, "filters": 1})

        # Schedule & mail test
        with patch.object(notify, "set_schedule", return_value=(True, "ok")), \
             patch.object(smtplib, "SMTP", side_effect=OSError("no smtp")):
            self.assertEqual(self.req("POST", "/api/schedule", {"enabled": True, "at": "09:00"})[0], 200)
            self.assertEqual(self.req("POST", "/api/schedule", {"enabled": True, "at": "bad"})[0], 400)
            self.assertEqual(self.req("POST", "/api/mail/test", {})[0], 200)

        # Cleanup global scan
        self.assertEqual(self.req("GET", f"/api/scans/{global_scan_id}")[0], 200)
        self.assertEqual(self.req("POST", f"/api/scans/{global_scan_id}/delete", {})[0], 200)

    def test_test_source_rss_webhook_schedule_preview_state_paging_and_item_history(self):
        wid = db.add_watch("AI Grants", "ai grants")
        sid = db.add_source("GrantBoard", "https://grants.example.com", watch_id=wid)
        db.update_source(sid, extractor="def fetch(): return [{'title': 'Grant 1', 'url': 'https://grants.example.com/1'}]",
                         extractor_ok=1, extractor_updated=db.now())

        # Test-source endpoints
        self.assertEqual(self.req("POST", "/api/sources/test", {"url": "ftp://bad"})[0], 400)
        code_t1, res_t1 = self.req("POST", "/api/sources/test", {"name": "GrantBoard", "url": "https://grants.example.com"})
        self.assertEqual(code_t1, 200)
        self.assertTrue(res_t1["ok"])
        self.assertEqual(res_t1["count"], 1)

        code_t2, res_t2 = self.req("POST", f"/api/sources/{sid}/test", {"notes": "check grants"})
        self.assertEqual(code_t2, 200)
        self.assertTrue(res_t2["ok"])
        self.assertEqual(self.req("POST", "/api/sources/9999/test", {})[0], 404)

        # Seed 8 items and record a deadline move for history endpoint
        src = db.source_by_id(sid)
        sc = db.start_scan("manual", "claude", "", [], watch_id=wid)
        for k in range(8):
            db.save_item(sc, src, {
                "id": f"g-{k}",
                "title": f"Grant #{k}",
                "url": f"https://grants.example.com/item/{k}",
                "deadline": "2027-04-01",
            })
        db.save_item(sc, src, {
            "id": "g-0",
            "title": "Grant #0",
            "url": "https://grants.example.com/item/0",
            "deadline": "2027-04-20",
        })
        db.set_matches({f"g-{k}": True for k in range(8)}, "k")
        db.finish_scan(sc, "done")

        # Item history endpoint
        code_h, hist = self.req("GET", "/api/items/g-0/history")
        self.assertEqual(code_h, 200)
        self.assertEqual(hist["history"][0]["kind"], "deadline_moved")
        self.assertEqual(self.req("GET", "/api/items/missing/history")[0], 404)

        # /api/state and /api/items pagination
        code_s1, st1 = self.req("GET", "/api/state?limit=3&offset=0")
        self.assertEqual(code_s1, 200)
        self.assertEqual(len(st1["items"]), 3)
        self.assertEqual(st1["items_total"], 8)
        self.assertTrue(st1["items_has_more"])

        code_s2, st2 = self.req("GET", "/api/items?limit=5&offset=5")
        self.assertEqual(code_s2, 200)
        self.assertEqual(len(st2["items"]), 3)
        self.assertEqual(st2["total"], 8)
        self.assertFalse(st2["has_more"])

        # Per-watch RSS/Atom: blocked without token when rss_public=0, public when rss_public=1
        self.assertEqual(self.req("GET", f"/api/watches/{wid}/feed.xml", token=None)[0], 401)
        self.assertEqual(self.req("GET", f"/api/watches/{wid}/atom.xml", token=None)[0], 401)
        code_r1, rss_xml = self.req("GET", f"/api/watches/{wid}/feed.xml")
        self.assertEqual(code_r1, 200)
        self.assertIn("<rss version=\"2.0\">", rss_xml)
        self.assertIn("Grant #0", rss_xml)

        self.assertEqual(self.req("POST", "/api/settings", {"rss_public": "1"})[0], 200)
        code_r2, rss_pub = self.req("GET", f"/api/watches/{wid}/feed.xml", token=None)
        self.assertEqual(code_r2, 200)
        self.assertIn("Grant #0", rss_pub)
        code_a2, atom_pub = self.req("GET", f"/api/watches/{wid}/atom.xml", token=None)
        self.assertEqual(code_a2, 200)
        self.assertIn("<feed xmlns=\"http://www.w3.org/2005/Atom\">", atom_pub)
        self.assertEqual(self.req("GET", "/api/watches/9999/feed.xml", token=None)[0], 404)

        # Schedule preview & backend forwarding
        code_p1, prev = self.req("GET", "/api/schedule/preview?at=07:15")
        self.assertEqual(code_p1, 200)
        self.assertIn("15 7 * * *", prev["cron"])
        self.assertIn("OnCalendar=*-*-* 07:15:00", prev["systemd_timer"])
        self.assertIn("com.lookout.dailyscan", prev["launchd_plist"])
        self.assertEqual(self.req("GET", "/api/schedule/preview?at=bad")[0], 400)
        self.assertEqual(self.req("POST", "/api/schedule/preview", {"at": "09:45"})[0], 200)
        with patch.object(notify, "set_schedule", autospec=True, return_value=(True, "cron ok")) as m_sched:
            code_sc, res_sc = self.req("POST", "/api/schedule", {"enabled": True, "at": "07:30", "backend": "crontab"})
            self.assertEqual(code_sc, 200)
            self.assertTrue(res_sc["ok"])
            self.assertEqual(m_sched.call_args.kwargs.get("backend"), "crontab")

        # Webhook settings & test endpoint
        self.assertEqual(self.req("POST", "/api/settings", {"webhook_url": "ftp://bad"})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {
            "webhook_url": "https://ntfy.sh/lookout-test",
            "webhook_allow_local": "1",
        })[0], 200)
        with patch.object(notify, "webhook", autospec=True, return_value=(True, "Webhook delivered")) as m_wh:
            code_w, res_w = self.req("POST", "/api/webhook/test", {})
            self.assertEqual(code_w, 200)
            self.assertTrue(res_w["ok"])
            self.assertEqual(m_wh.call_args.args[0], "https://ntfy.sh/lookout-test")
            self.assertTrue(m_wh.call_args.kwargs.get("allow_local"))

    def test_null_optional_fields_not_stored_as_none_string(self):
        code, w = self.req("POST", "/api/watches", {"title": "Null Req", "request": None})
        self.assertEqual(code, 200)
        watch = db.watch_detail(w["id"])
        self.assertEqual(watch["request"], "")

        code, sr = self.req("POST", "/api/sources", {
            "name": "Null Notes",
            "url": "https://nullnotes.example.com",
            "notes": None,
            "watch_id": w["id"],
        })
        self.assertEqual(code, 200)
        src = db.source_by_id(sr["id"])
        self.assertEqual(src["notes"], "")

    def test_scan_and_discover_endpoints_including_cancel(self):
        # 1. Discover creates watch + auto-adds filters & sources + triggers watch scan
        fake_disc = {
            "title": "TR CS Internships",
            "sources": [{"name": "TechCareers", "url": "https://tech.example.com/interns", "notes": "intern"}],
            "filters": [{"kind": "type", "value": "internship"}, {"kind": "exclude", "value": "panel"}],
        }
        with patch.object(tasks, "discover_sources", return_value=fake_disc), \
             patch.object(scanner, "start_scan_async", return_value=True) as m_scan:
            code, res = self.req("POST", "/api/discover", {"request": "computer engineering internships in turkiye"})
            self.assertEqual(code, 200)
            self.assertTrue(res["started"])
            did, wid = res["id"], res["watch_id"]
            for _ in range(50):
                if not server.discovery["running"] and m_scan.called:
                    break
                time.sleep(0.05)

        self.assertFalse(server.discovery["running"])
        w = db.watch_detail(wid)
        self.assertEqual(w["title"], "TR CS Internships")
        self.assertEqual(len(w["sources"]), 1)
        self.assertEqual(len(w["filters"]), 2)
        self.assertTrue(m_scan.called)
        self.assertEqual(m_scan.call_args.kwargs.get("watch_id"), wid)
        self.assertEqual(self.req("GET", f"/api/discoveries/{did}")[0], 200)
        self.assertEqual(self.req("POST", "/api/discover/clear", {})[0], 200)
        self.assertEqual(self.req("POST", f"/api/discoveries/{did}/delete", {})[0], 200)

        # 2. Discover cancel kills running agent process
        def slow_discover(req, cancel_event=None, session=None, on_progress=None):
            agents.run_tracked([sys.executable, "-c", "import time; time.sleep(30)"],
                               timeout=30, tag="discover", session=session, cancel_event=cancel_event)
            return fake_disc

        with patch.object(tasks, "discover_sources", side_effect=slow_discover), \
             patch.object(agents, "close_browser_session"):
            code, res = self.req("POST", "/api/discover", {"request": "slow search request"})
            self.assertEqual(code, 200)
            did2 = res["id"]
            for _ in range(50):
                with agents._proc_lock:
                    if agents._running:
                        break
                time.sleep(0.05)
            self.assertEqual(self.req("POST", "/api/discover/cancel", {})[0], 200)
            for _ in range(50):
                if not server.discovery["running"]:
                    break
                time.sleep(0.05)
        self.assertEqual(db.discovery_detail(did2)["status"], "cancelled")

        # 3. Scan & scan cancel
        db.set_settings({"agent": "claude"})
        sid = db.add_source("SlowBoard", "https://slowboard.example.com", watch_id=wid)
        db.update_source(sid, extractor="import time\ndef fetch():\n    time.sleep(30)\n    return []\n",
                         extractor_ok=1, extractor_updated=db.now())
        with patch.object(agents, "close_browser_session"):
            code, sc = self.req("POST", f"/api/watches/{wid}/scan", {})
            self.assertEqual(code, 200)
            self.assertTrue(sc["started"])
            for _ in range(50):
                with agents._proc_lock:
                    if agents._running:
                        break
                time.sleep(0.05)
            self.assertEqual(self.req("POST", "/api/scan/cancel", {})[0], 200)
            for _ in range(50):
                if not scanner.progress["running"]:
                    break
                time.sleep(0.05)
        self.assertEqual(db.scans()[0]["status"], "cancelled")

    def test_discovery_streams_partial_sources_into_scan_early(self):
        wid = db.add_watch("Early Watch", "early grants")
        did = db.start_discovery("early grants", "Early Watch", "claude", "", watch_id=wid)
        seen_early_scan = {"ok": False}

        def fake_disc(req, cancel_event=None, session=None, on_progress=None):
            self.assertIsNotNone(on_progress)
            on_progress({
                "title": "Early Grants",
                "sources": [{"name": "EarlySrc", "url": "https://early.example.com/list", "notes": ""}],
                "filters": [{"kind": "type", "value": "grant"}],
            })
            # Partial source should already be in DB and kicked off for scanning BEFORE discover_sources finishes
            seen_early_scan["ok"] = len(db.sources(watch_id=wid)) == 1 and len(db.filters(watch_id=wid)) == 1
            return {
                "title": "Early Grants",
                "sources": [
                    {"name": "EarlySrc", "url": "https://early.example.com/list", "notes": ""},
                    {"name": "SecondSrc", "url": "https://second.example.com/list", "notes": ""},
                ],
                "filters": [{"kind": "type", "value": "grant"}],
            }

        with patch.object(tasks, "discover_sources", new=fake_disc), \
             patch.object(scanner, "start_scan_async", return_value=True) as m_start, \
             patch.object(scanner, "enqueue_scan_sources", return_value=False) as m_enq:
            server.run_discovery(did, wid, "early grants")

        self.assertTrue(seen_early_scan["ok"])
        self.assertEqual(len(db.sources(watch_id=wid)), 2)
        self.assertGreaterEqual(m_start.call_count + m_enq.call_count, 2)


if __name__ == "__main__":
    unittest.main()
