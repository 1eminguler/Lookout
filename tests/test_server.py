"""Integration tests for lookout.server HTTP API & security boundaries."""
import csv
import http.client
import io
import json
import os
import re
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.request
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from unittest.mock import Mock, patch

from lookout import agents, db, notify, scanner, server, tasks
import run
from tests.helpers import TempLookoutTestCase

_REAL_AGENTS_DETECT = agents.detect
_REAL_AGENTS_WHICH = agents.which
_REAL_AGENTS_WHICH = agents.which


class ExportTests(TempLookoutTestCase):
    def test_deadline_text_in_csv_and_ics(self):
        sid = db.add_source("Board", "https://board.example")
        src = db.source_by_id(sid)
        scan_id = db.start_scan("manual", "claude", "", [])
        deadline = (date.today() + timedelta(days=5)).isoformat()
        reminder = (date.today() + timedelta(days=2)).isoformat()
        deadline_text = "15 Mart 17.00, Türkiye\r\nYerel saat"
        db.save_item(scan_id, src, {"id": "one", "title": "Intern", "url": src["url"] + "/1",
                                    "deadline": deadline, "deadline_raw": deadline_text})
        db.save_item(scan_id, src, {"id": "two", "title": "Engineer", "url": src["url"] + "/2",
                                    "deadline": deadline})
        db.set_item("one", action="saved", remind_on=reminder)

        handler = Mock()
        server.Handler._get(handler, "/api/export.csv")
        status, body, _ = handler.send.call_args.args
        self.assertEqual(status, 200)
        rows = {row["title"]: row for row in csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline=""))}
        self.assertEqual(rows["Intern"]["deadline"], deadline)
        self.assertEqual(rows["Intern"]["deadline_text"], deadline_text)
        self.assertEqual(rows["Engineer"]["deadline_text"], "")

        server.Handler._get(handler, "/api/agenda.ics")
        status, body, _ = handler.send.call_args.args
        self.assertEqual(status, 200)
        unfolded = body.decode("utf-8").replace("\r\n ", "")
        self.assertEqual(unfolded.count(r"\nDeadline text: 15 Mart 17.00\, Türkiye\nYerel saat"), 2)


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

    def req_raw(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = dict(headers or {})
        raw_body = None
        if body is not None:
            if isinstance(body, (dict, list)):
                raw_body = json.dumps(body).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
            elif isinstance(body, str):
                raw_body = body.encode("utf-8")
            else:
                raw_body = body
        try:
            conn.request(method, path, body=raw_body, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, raw
        finally:
            conn.close()

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

    def _assert_security_headers(self, hdrs):
        self.assertEqual(
            hdrs.get("content-security-policy"),
            "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        )
        self.assertEqual(hdrs.get("x-frame-options"), "DENY")
        self.assertEqual(hdrs.get("referrer-policy"), "no-referrer")
        self.assertEqual(hdrs.get("x-content-type-options"), "nosniff")
        self.assertEqual(hdrs.get("cache-control"), "no-store")

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

    def test_launch_secret_cookie_auth_and_security_headers(self):
        secret_file = self.data_dir / ".launch_secret"
        self.assertTrue(secret_file.is_file())
        self.assertEqual(secret_file.read_text(encoding="utf-8").strip(), self.srv.launch_secret)

        # Unauthenticated GET / and /index.html render gate page without embedding any token
        for p in ("/", "/index.html"):
            st, hdrs, raw = self.req_raw("GET", p)
            html = raw.decode("utf-8")
            self.assertEqual(st, 200)
            self._assert_security_headers(hdrs)
            self.assertIn("Open Lookout from the app", html)
            self.assertIn("Open Lookout from the app or its shortcut, then use this link again.", html)
            self.assertNotIn("lookout-token", html)
            self.assertNotIn(self.token, html)

        for static_p in ("/app.js", "/app.css"):
            st_s, hdrs_s, _ = self.req_raw("GET", static_p)
            self.assertEqual(st_s, 200)
            self._assert_security_headers(hdrs_s)

        # Exchanging one-time launch secret A sets HttpOnly SameSite=Strict cookie, redirects to /,
        # invalidates A, and immediately issues fresh one-time secret B into data/.launch_secret
        sec = self.srv.launch_secret
        st_302, hdrs_302, _ = self.req_raw("GET", f"/?k={sec}")
        self.assertEqual(st_302, 302)
        self.assertEqual(hdrs_302.get("location"), "/")
        self._assert_security_headers(hdrs_302)
        cookie_hdr = hdrs_302.get("set-cookie", "")
        self.assertIn("lookout_session=", cookie_hdr)
        self.assertIn("HttpOnly", cookie_hdr)
        self.assertIn("SameSite=Strict", cookie_hdr)
        self.assertIn("Path=/", cookie_hdr)
        self.assertTrue(secret_file.is_file())
        sec_b = secret_file.read_text(encoding="utf-8").strip()
        self.assertTrue(sec_b)
        self.assertNotEqual(sec_b, sec)
        self.assertEqual(self.srv.launch_secret, sec_b)
        with patch.object(server, "PORT", self.port):
            self.assertEqual(run._launch_url(), f"http://127.0.0.1:{self.port}/?k={sec_b}")

        sess_val = cookie_hdr.split("lookout_session=", 1)[1].split(";", 1)[0]

        # Replaying secret A without cookie fails with 401; bogus secret also 401
        for bad_sec in (sec, "wrong-secret"):
            st_bad, hdrs_bad, raw_bad = self.req_raw("GET", f"/?k={bad_sec}")
            self.assertEqual(st_bad, 401)
            self._assert_security_headers(hdrs_bad)
            self.assertIn("Open Lookout from the app", raw_bad.decode("utf-8"))

        # Fresh secret B works once, rotates file to C != B, and B fails on replay
        st_302_b, hdrs_302_b, _ = self.req_raw("GET", f"/?k={sec_b}")
        self.assertEqual(st_302_b, 302)
        self.assertIn("lookout_session=", hdrs_302_b.get("set-cookie", ""))
        self.assertTrue(secret_file.is_file())
        sec_c = secret_file.read_text(encoding="utf-8").strip()
        self.assertTrue(sec_c)
        self.assertNotEqual(sec_c, sec_b)
        self.assertEqual(self.req_raw("GET", f"/?k={sec_b}")[0], 401)

        # Valid session cookie accesses index.html (no token meta tag) and /api/* without X-Lookout-Token
        st_idx, hdrs_idx, raw_idx = self.req_raw("GET", "/", headers={"Cookie": f"lookout_session={sess_val}"})
        self.assertEqual(st_idx, 200)
        self._assert_security_headers(hdrs_idx)
        idx_html = raw_idx.decode("utf-8")
        self.assertIn("id=\"main\"", idx_html)
        self.assertNotIn("lookout-token", idx_html)
        self.assertNotIn(self.token, idx_html)

        # Already-signed-in browser visiting stale ?k= redirects cleanly to /
        st_re, hdrs_re, _ = self.req_raw("GET", "/?k=stale", headers={"Cookie": f"lookout_session={sess_val}"})
        self.assertEqual(st_re, 302)
        self.assertEqual(hdrs_re.get("location"), "/")

        st_api, _, raw_api = self.req_raw("GET", "/api/state", headers={"Cookie": f"lookout_session={sess_val}"})
        self.assertEqual(st_api, 200)
        self.assertIn("watches", json.loads(raw_api.decode("utf-8")))
        st_post, _, _ = self.req_raw(
            "POST", "/api/watches", body={"title": "Cookie Watch"},
            headers={"Cookie": f"lookout_session={sess_val}"},
        )
        self.assertEqual(st_post, 200)

        # issue_launch_secret() mints another working one-time secret and keeps file rotated on consumption
        sec2 = self.srv.issue_launch_secret()
        self.assertTrue(secret_file.is_file())
        st_302_2, _, _ = self.req_raw("GET", f"/index.html?k={sec2}")
        self.assertEqual(st_302_2, 302)
        self.assertTrue(secret_file.is_file())
        self.assertNotEqual(secret_file.read_text(encoding="utf-8").strip(), sec2)

        # Every /api/* route rejects missing or wrong tokens with 401 + security headers
        for method, path, body in (
            ("GET", "/api/state", None),
            ("GET", "/api/counts", None),
            ("GET", "/api/progress", None),
            ("GET", "/api/items", None),
            ("GET", "/api/sources/test", None),
            ("GET", "/api/sources/1/code", None),
            ("GET", "/api/agents", None),
            ("GET", "/api/export.csv", None),
            ("GET", "/api/agenda.ics", None),
            ("GET", "/api/backup.json", None),
            ("POST", "/api/sources", None),
            ("POST", "/api/settings", None),
            ("POST", "/api/scan", None),
            ("POST", "/api/notifications/dismiss", None),
            ("POST", "/api/schedule/test", None),
        ):
            with self.subTest(method=method, path=path):
                st_m, hdrs_m, _ = self.req_raw(method, path, body=body)
                self.assertEqual(st_m, 401)
                self._assert_security_headers(hdrs_m)
                self.assertEqual(self.req(method, path, body, token="wrong-token")[0], 401)
                self.assertEqual(
                    self.req_raw(method, path, body=body, headers={"Cookie": "lookout_session=wrong"})[0], 401
                )

        # 403 and 404 responses also carry security headers
        st_403, hdrs_403, _ = self.req_raw("GET", "/api/state", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(st_403, 403)
        self._assert_security_headers(hdrs_403)
        st_404, hdrs_404, _ = self.req_raw("GET", "/missing.txt")
        self.assertEqual(st_404, 404)
        self._assert_security_headers(hdrs_404)

        # Static index.html contains no inline style= attributes
        disk_html = (server.WEB / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("style=", disk_html.lower())
        self.assertNotIn("lookout-token", disk_html)

    def test_host_header_and_origin_security_and_bad_payloads(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", "/api/state", skip_host=True)
        conn.putheader("Host", f"evil.example:{self.port}")
        conn.putheader("X-Lookout-Token", self.token)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

        # Sec-Fetch-Site: cross-site blocked on GET and POST
        self.assertEqual(
            self.req_raw("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})[0],
            403,
        )
        self.assertEqual(
            self.req_raw("POST", "/api/sources", body={}, headers={
                "X-Lookout-Token": self.token,
                "Sec-Fetch-Site": "cross-site",
            })[0],
            403,
        )

        # Untrusted Origin and untrusted Referer blocked on POST
        for bad_origin in ("https://evil.com", f"https://evil.com/http://127.0.0.1:{self.port}"):
            st, _, _ = self.req_raw("POST", "/api/sources", body="", headers={
                "Host": f"127.0.0.1:{self.port}",
                "Origin": bad_origin,
                "X-Lookout-Token": self.token,
            })
            self.assertEqual(st, 403)

        self.assertEqual(
            self.req_raw("POST", "/api/watches", body={"title": "BadRef"}, headers={
                "Host": f"127.0.0.1:{self.port}",
                "Referer": "https://evil.com/phish",
                "X-Lookout-Token": self.token,
            })[0],
            403,
        )
        self.assertEqual(
            self.req_raw("POST", "/api/watches", body={"title": "GoodRef"}, headers={
                "Host": f"127.0.0.1:{self.port}",
                "Referer": f"http://127.0.0.1:{self.port}/#/home",
                "X-Lookout-Token": self.token,
            })[0],
            200,
        )

        # LOOKOUT_BIND env var binds to specified interface while preserving loopback Host validation
        with patch.dict(os.environ, {"LOOKOUT_BIND": "127.0.0.1"}):
            extra_srv = server.make_server(port=0)
            try:
                self.assertEqual(extra_srv.server_address[0], "127.0.0.1")
            finally:
                extra_srv.server_close()

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

    def test_custom_command_rejected_over_http_and_local_metadata_exposed(self):
        # custom_command is completely rejected over HTTP regardless of confirm flag
        for payload in (
            {"custom_command": "mycli run --yes {prompt}"},
            {"custom_command": "mycli run --yes {prompt}", "confirm_custom_command": True},
            {"custom_command": "mycli --yes && id", "confirm_custom_command": True},
        ):
            with self.subTest(payload=payload):
                code, res = self.req("POST", "/api/settings", payload)
                self.assertEqual(code, 403)
                self.assertIn("custom_command", res["error"])
                self.assertEqual(db.get_settings()["custom_command"], "")

        # public_settings() omits raw custom_command and exposes configured/path flags
        code_s1, st1 = self.req("GET", "/api/state")
        self.assertEqual(code_s1, 200)
        self.assertNotIn("custom_command", st1["settings"])
        self.assertFalse(st1["settings"]["custom_command_configured"])
        self.assertEqual(st1["settings"]["custom_command_path"], str(self.data_dir / "agent_command.txt"))

        (self.data_dir / "agent_command.txt").write_text("filecli run --yes {prompt}\n", encoding="utf-8")
        code_s2, st2 = self.req("GET", "/api/state")
        self.assertEqual(code_s2, 200)
        self.assertNotIn("custom_command", st2["settings"])
        self.assertTrue(st2["settings"]["custom_command_configured"])

        # /api/agents exposes configured/config_path/source on custom without raw command string
        with patch.object(agents, "detect", _REAL_AGENTS_DETECT), \
             patch.object(agents, "which", _REAL_AGENTS_WHICH), \
             patch("shutil.which", side_effect=lambda exe: "/bin/filecli" if exe == "filecli" else None), \
             patch.object(agents, "_run", return_value=(1, "")):
            code_a, ag = self.req("GET", "/api/agents")
        self.assertEqual(code_a, 200)
        custom_entry = next(a for a in ag["agents"] if a["id"] == "custom")
        self.assertNotIn("command", custom_entry)
        self.assertTrue(custom_entry["configured"])
        self.assertTrue(custom_entry["installed"])
        self.assertEqual(custom_entry["source"], "file")
        self.assertEqual(custom_entry["config_path"], str(self.data_dir / "agent_command.txt"))

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
        # Test ICS CR/LF injection, line folding, and raw unescaped URL with commas/semicolons + control chars
        long_title = "=SUM(A1:A2) " + ("LongTitleWord " * 8) + "\r\nATTENDEE:evil"
        db.save_item(scan_id, src, {
            "id": "ics-inj",
            "title": long_title,
            "org": "Acme; Corp, Inc\\Team",
            "url": "https://sec.example.com/jobs?tags=a,b;c\x07",
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
        # Unfolded URL: line keeps commas and semicolons raw without backslash-escaping and strips control chars
        unfolded_lines = ics_txt.replace("\r\n ", "").split("\r\n")
        url_lines = [ln for ln in unfolded_lines if ln.startswith("URL:")]
        self.assertIn("URL:https://sec.example.com/jobs?tags=a,b;c", url_lines)

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
        self.assertNotIn("items", st)
        self.assertNotIn("smtp_password", st["settings"])
        self.assertNotIn("custom_command", st["settings"])
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

    def _poll_test_job(self):
        for _ in range(100):
            code, tj = self.req("GET", "/api/sources/test")
            self.assertEqual(code, 200)
            if not tj["running"]:
                return tj
            time.sleep(0.05)
        self.fail("test_job did not finish in time")

    def test_backup_restores_tracked_items_and_history(self):
        sid = db.add_source("Tracked Backup", "https://backup-tracked.example.com")
        scan = db.start_scan("manual", "claude", "", [])
        db.save_item(scan, db.source_by_id(sid), {"id": "backup-tracked", "title": "Saved item", "url": "https://backup-tracked.example.com/1"})
        db.save_item(scan, db.source_by_id(sid), {"id": "backup-tracked", "title": "Saved item updated", "url": "https://backup-tracked.example.com/1"})
        db.set_item("backup-tracked", action="saved", note="Interview prep", remind_on="2027-02-03")
        self.assertTrue(db.item_history("backup-tracked"))
        code, backup = self.req("GET", "/api/backup.json")
        self.assertEqual(code, 200)
        self.assertEqual(len(backup["items"]), 1)
        db._exec("DELETE FROM item_history WHERE item_id=?", ("backup-tracked",))
        db._exec("DELETE FROM items WHERE id=?", ("backup-tracked",))
        code, restored = self.req("POST", "/api/import", backup)
        self.assertEqual(code, 200)
        item = db.item_by_id("backup-tracked")
        self.assertEqual(item["action"], "saved")
        self.assertEqual(item["note"], "Interview prep")
        self.assertEqual(item["remind_on"], "2027-02-03")
        self.assertTrue(db.item_history("backup-tracked"))
        self.assertEqual(restored["added"]["items"], 1)

    def test_eligibility_profile_and_items_filter(self):
        wid = db.add_watch("Internships")
        source = {"id": db.add_source("Board", "https://example.com/jobs", watch_id=wid),
                  "name": "Board", "watch_id": wid}
        scan_id = db.start_scan("manual", "", "", [])
        items = [(name, {"id": name, "title": name, "category": name, "url": f"https://example.com/{name}"})
                 for name in ("eligible", "unclear", "ineligible", "pending")]
        for _, item in items:
            db.save_item(scan_id, source, item, match=1)
        url = f"/api/items?watch_id={wid}&facets=1"
        self.assertEqual(self.req("GET", url + "&eligibility=ineligible")[1]["total"], 4)
        self.assertEqual(self.req("POST", "/api/settings", {"eligibility_profile": {"class_year": "n/a"}})[0], 400)
        profile = {"class_year": "4", "remote_ok": "no", "other": "  Summer term  "}
        self.assertEqual(self.req("POST", "/api/settings", {"eligibility_profile": profile})[0], 200)
        self.assertEqual(self.req("GET", "/api/state")[1]["settings"]["eligibility_profile"],
                         {**profile, "other": "Summer term"})
        db.store_eligibility(db.get_settings()["eligibility_profile"], [
            (item, f"hash-{name}", {"verdict": name, "checks": []}) for name, item in items if name != "pending"
        ])
        for path in ("/api/state", "/api/progress"):
            _, state = self.req("GET", path + "?eligibility=hide_ineligible")
            self.assertEqual(state["counts"]["feed_open"], 4)
            self.assertEqual(state["eligibility_counts"]["feed_open"], 3)
            self.assertEqual(state["eligibility_counts"]["watches"][str(wid)]["open"], 3)
        db.set_item("ineligible", snoozed_until=(date.today() + timedelta(days=1)).isoformat())
        for value in ("eligible", "unclear", "ineligible"):
            code, body = self.req("GET", url + f"&eligibility={value}")
            self.assertEqual(code, 200)
            self.assertEqual(body["total"], 1)
            self.assertEqual(body["facets"]["categories"], {value: 1})
            self.assertEqual(body["items"][0]["eligibility"]["verdict"], value)
        _, filtered = self.req("GET", url + "&eligibility=hide_ineligible&limit=2")
        self.assertEqual(filtered["total"], 3)
        self.assertEqual(filtered["facets"]["snoozed_count"], 0)
        self.assertTrue(filtered["has_more"])
        self.assertEqual(filtered["facets"]["categories"], {"eligible": 1, "unclear": 1, "pending": 1})
        _, unpaged = self.req("GET", url + "&eligibility=hide_ineligible")
        self.assertIsNone(next(i for i in unpaged["items"] if i["id"] == "pending")["eligibility"])
        self.assertEqual(self.req("POST", "/api/settings", {"eligibility_profile": {}})[0], 200)
        _, cleared = self.req("GET", url + "&eligibility=eligible")
        self.assertEqual(cleared["total"], 4)
        self.assertTrue(all(i["eligibility"] is None for i in cleared["items"]))

    def test_async_test_source_cancel_rss_webhook_schedule_counts_progress_and_items_api(self):
        wid = db.add_watch("AI Grants", "ai grants")
        sid = db.add_source("GrantBoard", "https://grants.example.com", watch_id=wid)
        reader_code = "def fetch(): return [{'title': 'Grant 1', 'url': 'https://grants.example.com/1'}]"
        db.update_source(sid, extractor=reader_code, extractor_ok=1, extractor_updated=db.now())

        # Async test-source preview endpoints + cancel + clear
        self.assertEqual(self.req("POST", "/api/sources/test", {"url": "ftp://bad"})[0], 400)
        code_t1, start_t1 = self.req("POST", "/api/sources/test", {"name": "GrantBoard", "url": "https://grants.example.com"})
        self.assertEqual(code_t1, 200)
        self.assertTrue(start_t1["started"])
        tj1 = self._poll_test_job()
        self.assertTrue(tj1["result"]["ok"])
        self.assertEqual(tj1["result"]["count"], 1)
        self.assertEqual(self.req("POST", "/api/sources/test/clear", {})[0], 200)

        code_t2, start_t2 = self.req("POST", f"/api/sources/{sid}/test", {"notes": "check grants"})
        self.assertEqual(code_t2, 200)
        self.assertTrue(start_t2["started"])
        tj2 = self._poll_test_job()
        self.assertTrue(tj2["result"]["ok"])
        self.assertEqual(self.req("POST", "/api/sources/test/clear", {})[0], 200)
        self.assertEqual(self.req("POST", "/api/sources/9999/test", {})[0], 404)

        # Cancel running async test job
        slow_sid = db.add_source("SlowTest", "https://slowtest.example.com", watch_id=wid)
        db.update_source(
            slow_sid,
            extractor="import time\ndef fetch():\n    time.sleep(30)\n    return []\n",
            extractor_ok=1,
            extractor_updated=db.now(),
        )
        with patch.object(agents, "close_browser_session"):
            code_c, start_c = self.req("POST", f"/api/sources/{slow_sid}/test", {})
            self.assertEqual(code_c, 200)
            self.assertTrue(start_c["started"])
            for _ in range(50):
                with agents._proc_lock:
                    if agents._running:
                        break
                time.sleep(0.05)
            # Clearing while running returns 409; cancelling stops promptly
            self.assertEqual(self.req("POST", "/api/sources/test/clear", {})[0], 409)
            self.assertEqual(self.req("POST", "/api/sources/test/cancel", {})[0], 200)
            tj_c = self._poll_test_job()
        self.assertFalse(tj_c["result"]["ok"])
        self.assertTrue(tj_c["result"]["cancelled"])
        self.assertEqual(tj_c["result"]["status"], "cancelled")
        self.assertEqual(tj_c["result"]["items"], [])
        self.assertEqual(self.req("POST", "/api/sources/test/clear", {})[0], 200)

        # Even if a fast plugin/reader returns items after _test_cancel is set, start_test_job discards them
        plug_started = threading.Event()
        plug_release = threading.Event()

        def fake_test_source(src, cancel_event=None):
            plug_started.set()
            plug_release.wait(timeout=3.0)
            return {
                "ok": True,
                "status": "done",
                "method": "plugin",
                "count": 53,
                "matching": 53,
                "items": [{"id": "x1", "title": "Should Be Discarded", "url": "https://baykar.example.com/1"}],
            }

        with patch.object(scanner, "test_source", side_effect=fake_test_source), \
             patch.object(agents, "close_browser_session"):
            code_p, start_p = self.req("POST", f"/api/sources/{sid}/test", {})
            self.assertEqual(code_p, 200)
            self.assertTrue(start_p["started"])
            self.assertTrue(plug_started.wait(timeout=2.0))
            self.assertEqual(self.req("POST", "/api/sources/test/cancel", {})[0], 200)
            plug_release.set()
            tj_p = self._poll_test_job()
        self.assertEqual(tj_p["status"], "cancelled")
        self.assertFalse(tj_p["result"]["ok"])
        self.assertTrue(tj_p["result"]["cancelled"])
        self.assertEqual(tj_p["result"]["status"], "cancelled")
        self.assertEqual(tj_p["result"]["items"], [])
        self.assertEqual(tj_p["result"]["count"], 0)
        self.assertEqual(self.req("POST", "/api/sources/test/clear", {})[0], 200)

        # /api/state omits items and reader code; /api/sources/<id>/code loads code on demand
        code_st, st = self.req("GET", "/api/state")
        self.assertEqual(code_st, 200)
        self.assertNotIn("items", st)
        src_entry = next(s for s in st["sources"] if s["id"] == sid)
        self.assertNotIn("extractor", src_entry)
        self.assertNotIn("last_good_extractor", src_entry)
        self.assertTrue(src_entry["has_extractor"])

        code_sc, sc_code = self.req("GET", f"/api/sources/{sid}/code")
        self.assertEqual(code_sc, 200)
        self.assertEqual(sc_code["extractor"], reader_code)
        self.assertEqual(self.req("GET", "/api/sources/99999/code")[0], 404)

        # Seed 8 items + 1 excluded + 1 snoozed + 1 hidden + 1 applied + 1 deadline move for history/paging/counts
        src = db.source_by_id(sid)
        sc = db.start_scan("manual", "claude", "", [], watch_id=wid)
        soon = (date.today() + timedelta(days=3)).isoformat()
        later = (date.today() + timedelta(days=20)).isoformat()
        for k in range(8):
            db.save_item(sc, src, {
                "id": f"g-{k}",
                "title": f"İSTANBUL Grant #{k}" if k == 0 else f"Grant #{k}",
                "org": "TechOrg",
                "category": "grant" if k % 2 == 0 else "fellowship",
                "url": f"https://grants.example.com/item/{k}",
                "deadline": soon if k < 2 else later,
                "summary": "Summary\x00with\x07ctrl\x1fchars\ufffe" if k == 0 else "Regular summary",
            }, match=1, match_key="k")
        db.save_item(sc, src, {
            "id": "g-0",
            "title": "İSTANBUL Grant #0",
            "org": "TechOrg",
            "category": "grant",
            "url": "https://grants.example.com/item/0",
            "deadline": soon,
            "summary": "Summary\x00with\x07ctrl\x1fchars\ufffe",
        })
        db.save_item(sc, src, {
            "id": "g-exc", "title": "Excluded Grant", "url": "https://grants.example.com/exc", "deadline": later
        }, match=0, match_key="det_excluded")
        db.set_item("g-7", snoozed_until=later)
        db.set_item("g-6", action="hidden")
        db.set_item("g-5", action="applied")
        db.finish_scan(sc, "done")
        # Record a deadline move on g-0 in a second scan
        sc2 = db.start_scan("manual", "claude", "", [], watch_id=wid)
        db.save_item(sc2, src, {
            "id": "g-0",
            "title": "İSTANBUL Grant #0",
            "org": "TechOrg",
            "category": "grant",
            "url": "https://grants.example.com/item/0",
            "deadline": (date.today() + timedelta(days=4)).isoformat(),
            "summary": "Summary\x00with\x07ctrl\x1fchars\ufffe",
        })
        db.finish_scan(sc2, "done")

        # Item history endpoint
        code_h, hist = self.req("GET", "/api/items/g-0/history")
        self.assertEqual(code_h, 200)
        self.assertEqual(hist["history"][0]["kind"], "deadline_moved")
        self.assertEqual(self.req("GET", "/api/items/missing/history")[0], 404)

        # /api/counts and /api/progress
        code_cnt, cnt = self.req("GET", "/api/counts")
        self.assertEqual(code_cnt, 200)
        self.assertEqual(cnt["feed_open"], 5)  # g-0..g-4 (g-5 applied, g-6 hidden, g-7 snoozed, g-exc match=0)
        self.assertEqual(cnt["feed_snoozed"], 1)
        self.assertEqual(cnt["feed_updated"], 1)
        self.assertEqual(cnt["watches"][str(wid)]["open"], 5)

        code_prog, prog = self.req("GET", "/api/progress")
        self.assertEqual(code_prog, 200)
        self.assertEqual(set(prog.keys()), {"progress", "discovery", "test_job", "counts"})

        # /api/items filtering, facets, Turkish search, and paging
        code_i1, st1 = self.req(
            "GET",
            f"/api/items?watch_id={wid}&status=open&match=1&exclude_hidden=1&exclude_applied=1&exclude_expired=1&snoozed=exclude&sort=deadline&limit=3&offset=0&facets=1",
        )
        self.assertEqual(code_i1, 200)
        self.assertEqual(len(st1["items"]), 3)
        self.assertEqual(st1["total"], 5)
        self.assertTrue(st1["has_more"])
        self.assertEqual(st1["facets"]["snoozed_count"], 1)
        self.assertEqual(st1["facets"]["updated_count"], 1)

        code_i2, st2 = self.req(
            "GET",
            f"/api/items?watch_id={wid}&status=open&match=1&exclude_hidden=1&exclude_applied=1&exclude_expired=1&snoozed=exclude&limit=3&offset=3",
        )
        self.assertEqual(code_i2, 200)
        self.assertEqual(len(st2["items"]), 2)
        self.assertFalse(st2["has_more"])

        code_tr, tr_res = self.req("GET", "/api/items?q=istanbul")
        self.assertEqual(code_tr, 200)
        self.assertEqual([i["id"] for i in tr_res["items"]], ["g-0"])
        self.assertEqual(self.req("GET", "/api/items?match=0")[1]["total"], 1)
        self.assertEqual(self.req("GET", "/api/items?snoozed=only")[1]["total"], 1)
        self.assertEqual(self.req("GET", "/api/items?updated=1")[1]["total"], 1)

        # Per-watch RSS/Atom: blocked without token when rss_public=0, valid XML & ctrl-char stripped when rss_public=1
        self.assertEqual(self.req("GET", f"/api/watches/{wid}/feed.xml", token=None)[0], 401)
        self.assertEqual(self.req("GET", f"/api/watches/{wid}/atom.xml", token=None)[0], 401)
        self.assertEqual(self.req("POST", "/api/settings", {"rss_public": "1"})[0], 200)

        code_r2, rss_pub = self.req("GET", f"/api/watches/{wid}/feed.xml", token=None)
        self.assertEqual(code_r2, 200)
        self.assertNotIn("\x00", rss_pub)
        self.assertNotIn("\x07", rss_pub)
        self.assertNotIn("\ufffe", rss_pub)
        ET.fromstring(rss_pub)

        code_a2, atom_pub = self.req("GET", f"/api/watches/{wid}/atom.xml", token=None)
        self.assertEqual(code_a2, 200)
        self.assertNotIn("\x00", atom_pub)
        ET.fromstring(atom_pub)
        self.assertEqual(self.req("GET", "/api/watches/9999/feed.xml", token=None)[0], 404)

        # Schedule preview + self-test endpoints
        code_p1, prev = self.req("GET", "/api/schedule/preview?at=07:15")
        self.assertEqual(code_p1, 200)
        self.assertIn("15 7 * * *", prev["cron"])
        self.assertIn("OnCalendar=*-*-* 07:15:00", prev["systemd_timer"])
        self.assertIn("com.lookout.dailyscan", prev["launchd_plist"])
        self.assertTrue(prev["test_command"])
        self.assertEqual(self.req("GET", "/api/schedule/preview?at=bad")[0], 400)
        self.assertEqual(self.req("POST", "/api/schedule/preview", {"at": "09:45"})[0], 200)

        with patch.object(notify, "run_schedule_now", return_value=(True, "Triggered ok")) as m_now:
            code_st_t, res_st_t = self.req("POST", "/api/schedule/test", {})
            self.assertEqual(code_st_t, 200)
            self.assertEqual(res_st_t, {"ok": True, "message": "Triggered ok"})
            m_now.assert_called_once()

        with patch.object(notify, "set_schedule", autospec=True, return_value=(True, "cron ok")) as m_sched:
            code_sc_s, res_sc_s = self.req("POST", "/api/schedule", {"enabled": True, "at": "07:30", "backend": "crontab"})
            self.assertEqual(code_sc_s, 200)
            self.assertTrue(res_sc_s["ok"])
            self.assertEqual(m_sched.call_args.kwargs.get("backend"), "crontab")

        # Webhook & mail settings validation + webhook test logging
        self.assertEqual(self.req("POST", "/api/settings", {"webhook_url": "ftp://bad"})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"webhook_format": "bogus"})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"smtp_starttls": "bogus"})[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {
            "webhook_url": "https://api.telegram.org/bot123:ABC/sendMessage",
            "webhook_format": "telegram",
            "telegram_chat_id": "998877",
            "webhook_allow_local": "1",
            "mail_from": "alerts@example.com",
            "smtp_starttls": "always",
        })[0], 200)
        with patch.object(notify, "webhook", autospec=True, return_value=(True, "Webhook delivered")) as m_wh:
            code_w, res_w = self.req("POST", "/api/webhook/test", {})
            self.assertEqual(code_w, 200)
            self.assertTrue(res_w["ok"])
            self.assertEqual(m_wh.call_args.args[0], "https://api.telegram.org/bot123:ABC/sendMessage")
            self.assertEqual(m_wh.call_args.args[1]["chat_id"], "998877")
            self.assertEqual(m_wh.call_args.kwargs.get("fmt"), "telegram")
            self.assertTrue(m_wh.call_args.kwargs.get("allow_local"))

    def test_source_resume_filters_changed_reevaluate_notify_dismiss_and_watch_discover(self):
        w1 = db.add_watch("W1", "old prompt")
        w2 = db.add_watch("W2", "other")
        sid = db.add_source("Src1", "https://src1.example.com/jobs", notes="tab A", watch_id=w1)
        db.update_source(
            sid,
            extractor="def fetch(): return [{'title': 'A', 'url': 'https://src1.example.com/1'}]",
            extractor_ok=1,
            paused=1,
            learn_fail_count=3,
            learn_retry_after=(datetime.now() + timedelta(hours=1)).strftime(db.TS),
            struct_fail_count=2,
            fail_count=2,
            last_error_kind="structural",
            extractor_error="paused after repeated failures",
        )

        # Resume via /resume endpoint resets pause and counters
        self.assertEqual(self.req("POST", "/api/sources/99999/resume", {})[0], 404)
        self.assertEqual(self.req("POST", f"/api/sources/{sid}/resume", {})[0], 200)
        s_resumed = db.source_by_id(sid)
        self.assertEqual(s_resumed["paused"], 0)
        self.assertEqual(s_resumed["learn_fail_count"], 0)
        self.assertEqual(s_resumed["struct_fail_count"], 0)
        self.assertIsNone(s_resumed["extractor_error"])

        # Resume via POST /api/sources/<id> {"resume": true}
        db.update_source(sid, paused=1, learn_fail_count=3)
        self.assertEqual(self.req("POST", f"/api/sources/{sid}", {"resume": True})[0], 200)
        self.assertEqual(db.source_by_id(sid)["paused"], 0)

        # Editing source notes/watch_id/url sets filters_changed=1; relearn clears it
        self.assertEqual(self.req("POST", f"/api/sources/{sid}", {"name": "Src1 Edited", "notes": "tab B"})[0], 200)
        self.assertEqual(db.source_by_id(sid)["name"], "Src1 Edited")
        self.assertEqual(db.source_by_id(sid)["filters_changed"], 1)
        self.assertEqual(self.req("POST", f"/api/sources/{sid}", {"relearn": True})[0], 200)
        self.assertEqual(db.source_by_id(sid)["filters_changed"], 0)

        db.update_source(sid, extractor_ok=1, filters_changed=0)
        self.assertEqual(self.req("POST", f"/api/sources/{sid}", {"watch_id": w2})[0], 200)
        self.assertEqual(db.source_by_id(sid)["watch_id"], w2)
        self.assertEqual(db.source_by_id(sid)["filters_changed"], 1)

        # Filter add/edit/delete triggers immediate reevaluate_open_items on open items
        src_obj = db.source_by_id(sid)
        sc = db.start_scan("manual", "claude", "", [], watch_id=w2)
        db.save_item(sc, src_obj, {"id": "re-1", "title": "Senior Architect", "url": "https://src1.example.com/re1"}, match=1, match_key="none")
        db.finish_scan(sc, "done")

        code_f, f_res = self.req("POST", "/api/filters", {"kind": "exclude", "value": "junior", "watch_id": w2})
        self.assertEqual(code_f, 200)
        fid = f_res["id"]
        self.assertEqual(db.item_by_id("re-1")["match"], 1)
        self.assertEqual(self.req("POST", f"/api/filters/{fid}", {"value": "senior"})[0], 200)
        self.assertEqual(db.item_by_id("re-1")["match"], 0)
        self.assertEqual(db.item_by_id("re-1")["match_key"], "det_excluded")
        self.assertEqual(self.req("POST", f"/api/filters/{fid}/delete", {})[0], 200)
        self.assertEqual(db.item_by_id("re-1")["match"], 1)

        # Notification error dismissal clears notify_errors in /api/state
        db.log_notification(sc, "webhook", False, "502 Bad Gateway")
        self.assertEqual(len(self.req("GET", "/api/state")[1]["notify_errors"]), 1)
        self.assertEqual(self.req("POST", "/api/notifications/dismiss", {})[0], 200)
        self.assertEqual(self.req("GET", "/api/state")[1]["notify_errors"], [])

        # Re-discovering an existing watch updates request, holds discover lock, and heartbeats while running
        saw_lock = {"active": False, "heartbeats": 0}
        orig_update_lock = db.update_scan_lock

        def counting_update_lock(token, scan_id=None, name="scan"):
            if name == "discover":
                saw_lock["heartbeats"] += 1
            return orig_update_lock(token, scan_id=scan_id, name=name)

        def fake_disc(req, cancel_event=None, session=None, on_progress=None):
            saw_lock["active"] = db.active_scan_lock("discover") is not None
            time.sleep(0.08)
            return {"title": "W1 Updated", "sources": [], "filters": []}

        with patch.object(server, "DISCOVERY_HEARTBEAT_SEC", 0.02), \
             patch.object(db, "update_scan_lock", side_effect=counting_update_lock), \
             patch.object(tasks, "discover_sources", side_effect=fake_disc):
            code_d, res_d = self.req("POST", "/api/discover", {"watch_id": w1, "request": "updated search query"})
            self.assertEqual(code_d, 200)
            self.assertTrue(res_d["started"])
            self.assertEqual(res_d["watch_id"], w1)
            for _ in range(50):
                if not server.discovery["running"]:
                    break
                time.sleep(0.05)
        self.assertTrue(saw_lock["active"])
        self.assertGreaterEqual(saw_lock["heartbeats"], 2)
        self.assertIsNone(db.active_scan_lock("discover"))
        self.assertEqual(db.watch_detail(w1)["request"], "updated search query")

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
