"""Integration tests for lookout.server HTTP API & security boundaries."""
import http.client
import json
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.request
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

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
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=3)
        for p in self._agent_patches:
            p.stop()
        super().tearDown()

    def req(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method)
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
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

        self.assertEqual(db.scan_detail(s_id)["status"], "interrupted")
        self.assertEqual(db.discovery_detail(d_id)["status"], "interrupted")

    def test_host_header_and_origin_security_and_bad_payloads(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", "/api/state", skip_host=True)
        conn.putheader("Host", f"evil.example:{self.port}")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

        for bad_origin in ("https://evil.com", f"https://evil.com/http://127.0.0.1:{self.port}"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", "/api/sources", body="{}", headers={
                "Host": f"127.0.0.1:{self.port}",
                "Origin": bad_origin,
                "Content-Type": "application/json",
            })
            self.assertEqual(conn.getresponse().status, 403)
            conn.close()

        # Invalid JSON and non-object JSON
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/sources", body="{not-json", headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400)
        conn.request("POST", "/api/sources", body="[1, 2]", headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400)
        # Negative Content-Length
        conn.putrequest("POST", "/api/sources")
        conn.putheader("Content-Length", "-5")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_static_state_agents_watches_sources_filters_items_settings_exports(self):
        code, html = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("Lookout", html)
        code, js = self.req("GET", "/app.js")
        self.assertEqual(code, 200)
        self.assertIn("renderWatch", js)
        self.assertEqual(self.req("GET", "/missing.txt")[0], 404)

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
        code, imp2 = self.req("POST", "/api/import", bkp)
        self.assertEqual(code, 200)
        self.assertEqual(imp2["added"], {"watches": 1, "sources": 1, "filters": 1})

        # Schedule & mail test
        with patch.object(notify, "set_schedule", return_value=(True, "ok")), \
             patch.object(smtplib, "SMTP", side_effect=OSError("no smtp")):
            self.assertEqual(self.req("POST", "/api/schedule", {"enabled": True, "at": "09:00"})[0], 200)
            self.assertEqual(self.req("POST", "/api/schedule", {"enabled": True, "at": "bad"})[0], 400)
            self.assertEqual(self.req("POST", "/api/mail/test", {})[0], 200)

        # Cleanup remaining scan/filter/source delete endpoints
        self.assertEqual(self.req("GET", f"/api/scans/{scan_id}")[0], 200)
        self.assertEqual(self.req("POST", f"/api/scans/{scan_id}/delete", {})[0], 200)

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
                if not server.discovery["running"]:
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
        def slow_discover(req, cancel_event=None, session=None):
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


if __name__ == "__main__":
    unittest.main()
