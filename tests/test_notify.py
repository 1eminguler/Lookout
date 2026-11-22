"""Unit tests for desktop notifications, TLS/SSL SMTP mail digest, webhooks and OS scheduling."""
import email
import http.server
import json
import os
import plistlib
import smtplib
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lookout import db, lookout_extract, notify
from tests.helpers import TempLookoutTestCase


class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.hits.append(("GET", self.path, dict(self.headers), b""))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        self.server.hits.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        port = self.server.server_address[1]
        if self.path == "/redir-local":
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{port}/secret")
            self.end_headers()
            return
        if self.path == "/fail":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"server error")
            return
        if self.path == "/slow":
            time.sleep(0.7)
            try:
                self.send_response(200)
                self.end_headers()
            except OSError:
                pass
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"received")


class NotifyTests(TempLookoutTestCase):
    def test_schedule_windows(self):
        with patch.object(sys, "platform", "win32"):
            with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run_mock:
                st = notify.schedule_status(use_cache=False)
                self.assertEqual(st, {"supported": True, "enabled": True, "backend": "task_scheduler"})
                ok, msg = notify.set_schedule(True, "09:15")
                self.assertTrue(ok)
                self.assertIn("09:15", msg)
                self.assertTrue(run_mock.called)

                ok_bad, _ = notify.set_schedule(True, "25:99")
                self.assertFalse(ok_bad)

                ok_off, _ = notify.set_schedule(False, "09:15")
                self.assertTrue(ok_off)

            with patch("subprocess.run", side_effect=OSError("missing")):
                self.assertEqual(
                    notify.schedule_status(use_cache=False),
                    {"supported": True, "enabled": False, "backend": "task_scheduler"},
                )
                ok_err, _ = notify.set_schedule(True, "08:30")
                self.assertFalse(ok_err)

    def test_schedule_linux_systemd_and_crontab_fallback_with_undo(self):
        sdir = self.tmp / "systemd_user"
        crontab_state = {"text": "0 6 * * * /usr/bin/backup\n"}
        systemd_ready = {"ok": True}

        def fake_which(cmd):
            if cmd == "systemctl" and systemd_ready["ok"]:
                return "/usr/bin/systemctl"
            if cmd == "crontab":
                return "/usr/bin/crontab"
            return None

        def fake_run(cmd, input=None, **kw):
            exe = cmd[0]
            if exe.endswith("systemctl"):
                if not systemd_ready["ok"]:
                    return subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus")
                if cmd[1:3] == ["--user", "is-enabled"]:
                    tmr = sdir / f"{notify.SYSTEMD_UNIT}.timer"
                    return subprocess.CompletedProcess(cmd, 0 if tmr.exists() else 1, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if exe.endswith("crontab"):
                if cmd[1] == "-l":
                    return subprocess.CompletedProcess(cmd, 0, crontab_state["text"], "")
                if cmd[1] == "-":
                    crontab_state["text"] = input or ""
                    return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 1, "", "unknown")

        with patch.object(sys, "platform", "linux"), \
             patch.object(notify, "_systemd_dir", return_value=sdir), \
             patch("shutil.which", side_effect=fake_which), \
             patch("subprocess.run", side_effect=fake_run):
            st0 = notify.schedule_status(use_cache=False)
            self.assertTrue(st0["supported"])
            self.assertFalse(st0["enabled"])
            self.assertEqual(st0["backend"], "systemd")

            ok, msg = notify.set_schedule(True, "07:45")
            self.assertTrue(ok, msg)
            svc = sdir / f"{notify.SYSTEMD_UNIT}.service"
            tmr = sdir / f"{notify.SYSTEMD_UNIT}.timer"
            self.assertTrue(svc.is_file())
            self.assertTrue(tmr.is_file())
            self.assertIn("OnCalendar=*-*-* 07:45:00", tmr.read_text(encoding="utf-8"))
            self.assertIn("Persistent=true", tmr.read_text(encoding="utf-8"))

            st1 = notify.schedule_status(use_cache=False)
            self.assertTrue(st1["enabled"])
            self.assertEqual(st1["backend"], "systemd")

            # Clean undo removes unit files
            ok_off, _ = notify.set_schedule(False, "07:45")
            self.assertTrue(ok_off)
            self.assertFalse(svc.exists())
            self.assertFalse(tmr.exists())

            # Fallback to crontab when systemd user bus is unavailable
            systemd_ready["ok"] = False
            ok_c, msg_c = notify.set_schedule(True, "08:15")
            self.assertTrue(ok_c, msg_c)
            self.assertIn("0 6 * * * /usr/bin/backup", crontab_state["text"])
            self.assertIn("15 8 * * *", crontab_state["text"])
            self.assertIn(notify.CRON_MARKER, crontab_state["text"])
            st_c = notify.schedule_status(use_cache=False)
            self.assertEqual(st_c["backend"], "crontab")
            self.assertTrue(st_c["enabled"])

            # Updating time replaces rather than duplicating the lookout entry
            notify.set_schedule(True, "09:05")
            self.assertEqual(crontab_state["text"].count(notify.CRON_MARKER), 1)
            self.assertIn("5 9 * * *", crontab_state["text"])

            # Clean undo removes only the lookout line
            notify.set_schedule(False, "09:05")
            self.assertNotIn(notify.CRON_MARKER, crontab_state["text"])
            self.assertIn("0 6 * * * /usr/bin/backup", crontab_state["text"])

        # Neither systemctl nor crontab available
        with patch.object(sys, "platform", "linux"), \
             patch.object(notify, "_systemd_dir", return_value=sdir), \
             patch("shutil.which", return_value=None):
            st_none = notify.schedule_status(use_cache=False)
            self.assertFalse(st_none["supported"])
            self.assertFalse(st_none["enabled"])
            self.assertIn("crontab", st_none["hint"])
            ok_none, hint = notify.set_schedule(True, "08:30")
            self.assertFalse(ok_none)
            self.assertIn("crontab", hint)

    def test_schedule_macos_launchd_and_crontab_fallback_with_undo(self):
        plist_path = self.tmp / "LaunchAgents" / f"{notify.LAUNCHD_LABEL}.plist"
        crontab_state = {"text": ""}
        launchd_ok = {"ok": True}

        def fake_which(cmd):
            if cmd == "launchctl" and launchd_ok["ok"]:
                return "/bin/launchctl"
            if cmd == "crontab":
                return "/usr/bin/crontab"
            return None

        def fake_run(cmd, input=None, **kw):
            exe = cmd[0]
            if exe.endswith("launchctl"):
                if cmd[1] == "list":
                    return subprocess.CompletedProcess(cmd, 0 if plist_path.exists() else 1, "", "")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if exe.endswith("crontab"):
                if cmd[1] == "-l":
                    if not crontab_state["text"]:
                        return subprocess.CompletedProcess(cmd, 1, "", "crontab: no crontab for user")
                    return subprocess.CompletedProcess(cmd, 0, crontab_state["text"], "")
                if cmd[1] == "-":
                    crontab_state["text"] = input or ""
                    return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        with patch.object(sys, "platform", "darwin"), \
             patch.object(notify, "_launchd_path", return_value=plist_path), \
             patch("shutil.which", side_effect=fake_which), \
             patch("subprocess.run", side_effect=fake_run):
            ok, msg = notify.set_schedule(True, "06:20")
            self.assertTrue(ok, msg)
            self.assertTrue(plist_path.is_file())
            parsed = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(parsed["Label"], notify.LAUNCHD_LABEL)
            self.assertEqual(parsed["StartCalendarInterval"], {"Hour": 6, "Minute": 20})
            self.assertEqual(parsed["ProgramArguments"][-2:], ["scan", "--scheduled"])

            st = notify.schedule_status(use_cache=False)
            self.assertTrue(st["supported"])
            self.assertTrue(st["enabled"])
            self.assertEqual(st["backend"], "launchd")

            # Clean undo
            ok_off, _ = notify.set_schedule(False, "06:20")
            self.assertTrue(ok_off)
            self.assertFalse(plist_path.exists())

            # Fallback to crontab when launchctl unavailable
            launchd_ok["ok"] = False
            ok_c, _ = notify.set_schedule(True, "10:30")
            self.assertTrue(ok_c)
            self.assertIn("30 10 * * *", crontab_state["text"])
            notify.set_schedule(False, "10:30")
            self.assertNotIn(notify.CRON_MARKER, crontab_state["text"])

        preview = notify.schedule_preview("08:30")
        self.assertIn("lookout-daily-scan", preview["cron"])
        self.assertIn("OnCalendar=*-*-* 08:30:00", preview["systemd_timer"])
        self.assertIn(notify.LAUNCHD_LABEL, preview["launchd_plist"])

    def test_desktop_notifications_escaping_and_missing_tools(self):
        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", return_value="C:\\Windows\\powershell.exe"), \
             patch("subprocess.run") as m_run:
            notify.desktop("Title & <tag>", ["L1", "L2", "L3"], "http://127.0.0.1/?a=1&b=2")
        env = m_run.call_args.kwargs["env"]
        self.assertIn("Title &amp; &lt;tag&gt;", env["LOOKOUT_TOAST"])
        self.assertNotIn("L3", env["LOOKOUT_TOAST"])

        # Bare localhost URL resolves to ?k=<secret> from data/.launch_secret in desktop toast
        (self.data_dir / ".launch_secret").write_text("toast_sec_99\n", encoding="utf-8")
        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", return_value="C:\\Windows\\powershell.exe"), \
             patch("subprocess.run") as m_run:
            notify.desktop("New items", ["Item 1"], "http://127.0.0.1:8780/")
        self.assertIn('launch="http://127.0.0.1:8780/?k=toast_sec_99"', m_run.call_args.kwargs["env"]["LOOKOUT_TOAST"])
        (self.data_dir / ".launch_secret").unlink(missing_ok=True)

        with patch.object(sys, "platform", "darwin"), \
             patch("shutil.which", return_value="/usr/bin/osascript"), \
             patch("subprocess.run") as m_run:
            notify.desktop('Say "Hi"', ['One "two"\nline'], "http://127.0.0.1/")
        script = m_run.call_args.args[0][2]
        self.assertIn(r'\"Hi\"', script)
        self.assertIn(r'\"two\"', script)

        with patch.object(sys, "platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/notify-send"), \
             patch("subprocess.run") as m_run:
            notify.desktop("News", ["Item A"], "http://127.0.0.1/")
        self.assertEqual(m_run.call_args.args[0][0], "/usr/bin/notify-send")

        for plat in ("win32", "darwin", "linux"):
            with patch.object(sys, "platform", plat), patch("shutil.which", return_value=None):
                notify.desktop("Safe", ["Line"], "http://127.0.0.1/")
            with patch.object(sys, "platform", plat), \
                 patch("shutil.which", return_value="/usr/bin/notify-send"), \
                 patch("subprocess.run", side_effect=OSError("boom")):
                notify.desktop('Title "1"', ["Line A", "Line B"], "http://127.0.0.1:8780/")

    def test_mail_digest_tls_port_465_and_errors(self):
        ok, msg = notify.mail_digest([], "http://127.0.0.1:8780/")
        self.assertFalse(ok)
        self.assertIn("not configured", msg)

        db.set_settings({"smtp_user": "a@example.com", "smtp_password": "secret", "mail_to": "b@example.com",
                         "smtp_port": "badport"})
        ok_port, msg_port = notify.mail_digest([], "http://127.0.0.1:8780/")
        self.assertFalse(ok_port)
        self.assertIn("port", msg_port.lower())

        # Port 587: STARTTLS with verified default SSLContext
        db.set_settings({"smtp_port": "587"})
        srv = MagicMock()
        smtp_ctx = MagicMock()
        smtp_ctx.__enter__.return_value = srv
        items = [{"title": "Intern", "url": "https://example.com/1", "org": "Acme",
                  "source_name": "Board", "deadline": "2027-01-15"}]
        with patch("smtplib.SMTP", return_value=smtp_ctx):
            ok_sent, msg_sent = notify.mail_digest(items, "http://127.0.0.1:8780/")
        self.assertTrue(ok_sent)
        self.assertIn("b@example.com", msg_sent)
        srv.starttls.assert_called_once()
        tls_ctx = srv.starttls.call_args.kwargs.get("context")
        self.assertIsInstance(tls_ctx, ssl.SSLContext)
        self.assertEqual(tls_ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(tls_ctx.check_hostname)
        srv.login.assert_called_once_with("a@example.com", "secret")
        srv.sendmail.assert_called_once()

        # Port 465: SMTP_SSL with default SSLContext, no starttls()
        db.set_settings({"smtp_port": "465"})
        srv465 = MagicMock()
        smtp465_ctx = MagicMock()
        smtp465_ctx.__enter__.return_value = srv465
        with patch("smtplib.SMTP_SSL", return_value=smtp465_ctx) as ssl_mock:
            ok_465, msg_465 = notify.mail_digest(items, "http://127.0.0.1:8780/")
        self.assertTrue(ok_465, msg_465)
        ssl_mock.assert_called_once()
        ctx_465 = ssl_mock.call_args.kwargs.get("context")
        self.assertIsInstance(ctx_465, ssl.SSLContext)
        self.assertEqual(ctx_465.verify_mode, ssl.CERT_REQUIRED)
        srv465.starttls.assert_not_called()
        srv465.login.assert_called_once_with("a@example.com", "secret")
        srv465.sendmail.assert_called_once()

        db.set_settings({"smtp_port": "587"})
        for exc, needle in [
            (smtplib.SMTPAuthenticationError(535, b"bad auth"), "authentication failed"),
            (ssl.SSLCertVerificationError("cert expired"), "certificate verification failed"),
            (ssl.SSLError("handshake failed"), "TLS/SSL error"),
            (smtplib.SMTPConnectError(421, b"down"), "Could not connect"),
            (TimeoutError("timed out"), "Timed out"),
            (smtplib.SMTPException("generic"), "SMTP error"),
            (OSError("refused"), "Could not send"),
        ]:
            with patch("smtplib.SMTP", side_effect=exc):
                ok_fail, msg_fail = notify.mail_digest(items, "http://127.0.0.1:8780/")
            self.assertFalse(ok_fail)
            self.assertIn(needle, msg_fail)

    def test_webhook_ssrf_json_and_ntfy(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _WebhookHandler)
        srv.hits = []
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            # Missing / private / loopback URLs are refused by default
            ok_empty, msg_empty = notify.webhook("", {"title": "x"})
            self.assertFalse(ok_empty)
            self.assertIn("not configured", msg_empty.lower())

            for bad in (
                f"http://127.0.0.1:{port}/hook",
                f"http://localhost:{port}/hook",
                "http://10.0.0.1/hook",
                "http://169.254.169.254/latest/meta-data",
                "http://100.64.0.1/hook",
            ):
                ok_b, msg_b = notify.webhook(bad, {"a": 1})
                self.assertFalse(ok_b, bad)
                self.assertIn("blocked", msg_b.lower())
            self.assertEqual(srv.hits, [])

            # Opt-in allow_local=True allows local webhooks
            ok_loc, msg_loc = notify.webhook(f"http://127.0.0.1:{port}/hook", {"event": "scan", "new": 2}, allow_local=True)
            self.assertTrue(ok_loc, msg_loc)
            self.assertEqual(len(srv.hits), 1)
            _, path, hdrs, raw = srv.hits[0]
            self.assertEqual(path, "/hook")
            self.assertIn("application/json", hdrs["content-type"])
            self.assertEqual(json.loads(raw.decode("utf-8")), {"event": "scan", "new": 2})

            # Public DNS mock -> generic JSON POST, ntfy topic, redirect SSRF block, HTTP 500, timeout
            orig_getaddrinfo = socket.getaddrinfo
            orig_create_conn = socket.create_connection

            def fake_getaddrinfo(host, p, *a, **kw):
                h = str(host or "").rstrip(".").lower()
                if h in ("hooks.example", "ntfy.sh"):
                    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", int(p or 80)))]
                return orig_getaddrinfo(host, p, *a, **kw)

            def fake_create_conn(addr, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
                host, p = addr
                if str(host).lower() in ("hooks.example", "ntfy.sh", "93.184.216.34"):
                    return orig_create_conn(("127.0.0.1", port), timeout=timeout, source_address=source_address)
                return orig_create_conn(addr, timeout=timeout, source_address=source_address)

            srv.hits.clear()
            with patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo), \
                 patch.object(socket, "create_connection", side_effect=fake_create_conn):
                # Redirect to loopback is blocked
                ok_r, msg_r = notify.webhook(f"http://hooks.example:{port}/redir-local", {"x": 1})
                self.assertFalse(ok_r)
                self.assertIn("blocked", msg_r.lower())
                self.assertFalse(any(p == "/secret" for _, p, _, _ in srv.hits))

                # Public ntfy topic URL formats plain text + Title/Click headers safely
                ok_n, msg_n = notify.webhook(
                    f"http://ntfy.sh:{port}/lookout-alerts",
                    {
                        "title": "Lookout — 1 new item",
                        "message": "Found 1 match",
                        "url": "http://127.0.0.1:8780/#/feed",
                        "tags": ["bell", "briefcase"],
                        "items": [{"title": "ML Intern", "source_name": "Acme", "url": "https://acme.example/1"}],
                    },
                )
                self.assertTrue(ok_n, msg_n)
                _, n_path, n_hdrs, n_body = srv.hits[-1]
                self.assertEqual(n_path, "/lookout-alerts")
                self.assertIn("text/plain", n_hdrs["content-type"])
                self.assertEqual(n_hdrs["title"], "Lookout - 1 new item")
                self.assertEqual(n_hdrs["click"], "http://127.0.0.1:8780/#/feed")
                self.assertEqual(n_hdrs["tags"], "bell,briefcase")
                self.assertIn("ML Intern", n_body.decode("utf-8"))

                # HTTP 500 and timeout errors
                ok_500, msg_500 = notify.webhook(f"http://hooks.example:{port}/fail", {"x": 1})
                self.assertFalse(ok_500)
                self.assertIn("500", msg_500)
                self.assertIn("server error", msg_500)

                ok_to, msg_to = notify.webhook(f"http://hooks.example:{port}/slow", {"x": 1}, timeout=0.5)
                self.assertFalse(ok_to)
                self.assertIn("timed out", msg_to.lower())
        finally:
            srv.shutdown()
            srv.server_close()

    def _patch_settings(self, overrides):
        merged = db.get_settings()
        merged.update(overrides)
        return patch.object(notify.db, "get_settings", return_value=merged)

    def test_desktop_return_values_and_error_handling(self):
        with patch.object(sys, "platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/notify-send"), \
             patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")):
            ok, msg = notify.desktop("Lookout", ["Line 1"], "http://127.0.0.1/")
        self.assertTrue(ok)
        self.assertIn("sent", msg.lower())

        for plat in ("win32", "darwin", "linux"):
            with patch.object(sys, "platform", plat), patch("shutil.which", return_value=None):
                ok_miss, msg_miss = notify.desktop("Title", ["Line"], "http://127.0.0.1/")
            self.assertFalse(ok_miss)
            self.assertIn("not available", msg_miss.lower())

        with patch.object(sys, "platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/notify-send"), \
             patch("subprocess.run", return_value=subprocess.CompletedProcess([], 2, b"", b"dbus error")):
            ok_rc, msg_rc = notify.desktop("Title", ["Line"], "http://127.0.0.1/")
        self.assertFalse(ok_rc)
        self.assertIn("dbus error", msg_rc)

        with patch.object(sys, "platform", "win32"), \
             patch("shutil.which", return_value="powershell.exe"), \
             patch("subprocess.run", return_value=subprocess.CompletedProcess([], 3, b"", b"")):
            ok_rc2, msg_rc2 = notify.desktop("Title", ["Line"], "http://127.0.0.1/")
        self.assertFalse(ok_rc2)
        self.assertIn("exit code 3", msg_rc2)

        with patch.object(sys, "platform", "darwin"), \
             patch("shutil.which", return_value="/usr/bin/osascript"), \
             patch("subprocess.run", side_effect=OSError("boom")):
            ok_exc, msg_exc = notify.desktop("Title", ["Line"], "http://127.0.0.1/")
        self.assertFalse(ok_exc)
        self.assertIn("boom", msg_exc)

    def test_mail_digest_from_multi_to_injection_changed_items_and_starttls_modes(self):
        # Custom mail_from + multi-delimiter recipients + changed_items rendering
        srv = MagicMock()
        srv.has_extn.return_value = True
        smtp_ctx = MagicMock()
        smtp_ctx.__enter__.return_value = srv

        new_items = [{"title": "New <Role>", "url": "https://ex.com/new?a=1&b=2", "org": "OrgA", "source_name": "Board"}]
        upd_items = [{
            "title": "Updated Grant",
            "url": "https://ex.com/upd",
            "source_name": "Grants",
            "change_summary": "deadline 2027-02-01 -> 2027-03-01 & extra",
        }]
        with self._patch_settings({
            "mail_from": "digest@example.com",
            "smtp_from": "fallback@example.com",
            "smtp_user": "auth@example.com",
            "smtp_password": "secret",
            "mail_to": "one@example.com, two@example.com; three@example.com\n\nfour@example.com",
            "smtp_port": "587",
            "smtp_starttls": "auto",
        }), patch("smtplib.SMTP", return_value=smtp_ctx):
            ok, msg = notify.mail_digest(new_items, "http://127.0.0.1:8780/", changed_items=upd_items)

        self.assertTrue(ok, msg)
        self.assertEqual(
            srv.sendmail.call_args.args[:2],
            ("digest@example.com", ["one@example.com", "two@example.com", "three@example.com", "four@example.com"]),
        )
        raw_mime = srv.sendmail.call_args.args[2]
        parsed_msg = email.message_from_string(raw_mime)
        self.assertEqual(parsed_msg["Subject"], "Lookout: 1 new, 1 updated")
        self.assertEqual(parsed_msg["From"], "digest@example.com")
        self.assertEqual(parsed_msg["To"], "one@example.com, two@example.com, three@example.com, four@example.com")
        parts = {p.get_content_type(): p.get_payload(decode=True).decode("utf-8") for p in parsed_msg.walk() if not p.is_multipart()}
        self.assertIn("New items (1):", parts["text/plain"])
        self.assertIn("Updated items (1):", parts["text/plain"])
        self.assertIn("deadline 2027-02-01 -> 2027-03-01 & extra", parts["text/plain"])
        self.assertIn("New &lt;Role&gt;", parts["text/html"])
        self.assertIn("https://ex.com/new?a=1&amp;b=2", parts["text/html"])
        self.assertIn("deadline 2027-02-01 -&gt; 2027-03-01 &amp; extra", parts["text/html"])
        srv.starttls.assert_called_once()
        srv.login.assert_called_once_with("auth@example.com", "secret")

        # Unauthenticated relay when mail_from + mail_to set without smtp_user
        srv_relay = MagicMock()
        srv_relay.has_extn.return_value = False
        ctx_relay = MagicMock()
        ctx_relay.__enter__.return_value = srv_relay
        mixed_items = [
            {"title": "Fresh", "url": "https://ex.com/1", "is_new": 1, "is_updated": 0},
            {"title": "Changed", "url": "https://ex.com/2", "is_new": 0, "is_updated": 1, "change_summary": "date moved"},
        ]
        with self._patch_settings({
            "mail_from": "relay@example.com",
            "smtp_user": "",
            "smtp_password": "",
            "mail_to": "dest@example.com",
            "smtp_port": "25",
            "smtp_starttls": "auto",
        }), patch("smtplib.SMTP", return_value=ctx_relay):
            ok_rel, msg_rel = notify.mail_digest(mixed_items, "http://127.0.0.1:8780/")
        self.assertTrue(ok_rel, msg_rel)
        srv_relay.starttls.assert_not_called()
        srv_relay.login.assert_not_called()
        self.assertIn("Lookout: 1 new, 1 updated", srv_relay.sendmail.call_args.args[2])

        # smtp_user set without smtp_password is rejected as not configured
        with self._patch_settings({
            "smtp_user": "user@example.com",
            "smtp_password": "",
            "mail_to": "dest@example.com",
        }):
            ok_nopw, msg_nopw = notify.mail_digest([], "http://127.0.0.1:8780/")
        self.assertFalse(ok_nopw)
        self.assertIn("not configured", msg_nopw.lower())

        # CR/LF injection rejection in mail_from and mail_to
        for bad_cfg in (
            {"mail_from": "a@example.com\r\nBcc: evil@example.com", "mail_to": "b@example.com"},
            {"mail_from": "a@example.com\nSubject: bad", "mail_to": "b@example.com"},
            {"mail_from": "a@example.com", "mail_to": "b@example.com\r\nBcc: evil@example.com"},
            {"mail_from": "a@example.com", "mail_to": "b@example.com\rBcc: evil@example.com"},
            {"mail_from": "a@example.com", "mail_to": "b@example.com\nBcc: evil@example.com"},
        ):
            with self._patch_settings(bad_cfg):
                ok_inj, msg_inj = notify.mail_digest([], "http://127.0.0.1:8780/")
            self.assertFalse(ok_inj, bad_cfg)
            self.assertEqual(msg_inj, "Invalid e-mail address.")

        # STARTTLS required ("1") fails when server does not advertise starttls
        srv_nostls = MagicMock()
        srv_nostls.has_extn.return_value = False
        ctx_nostls = MagicMock()
        ctx_nostls.__enter__.return_value = srv_nostls
        with self._patch_settings({
            "smtp_user": "a@example.com",
            "smtp_password": "secret",
            "mail_to": "b@example.com",
            "smtp_port": "587",
            "smtp_starttls": "1",
        }), patch("smtplib.SMTP", return_value=ctx_nostls):
            ok_req, msg_req = notify.mail_digest([], "http://127.0.0.1:8780/")
        self.assertFalse(ok_req)
        self.assertIn("STARTTLS", msg_req)
        srv_nostls.sendmail.assert_not_called()

        # STARTTLS disabled ("0") skips starttls even when offered
        srv_off = MagicMock()
        srv_off.has_extn.return_value = True
        ctx_off = MagicMock()
        ctx_off.__enter__.return_value = srv_off
        with self._patch_settings({
            "smtp_user": "a@example.com",
            "smtp_password": "secret",
            "mail_to": "b@example.com",
            "smtp_port": "587",
            "smtp_starttls": "0",
        }), patch("smtplib.SMTP", return_value=ctx_off):
            ok_off, _ = notify.mail_digest([], "http://127.0.0.1:8780/")
        self.assertTrue(ok_off)
        srv_off.starttls.assert_not_called()
        srv_off.sendmail.assert_called_once()

    def test_webhook_formats_and_autodetection(self):
        self.assertEqual(notify.WEBHOOK_FORMATS, ("json", "slack", "discord", "telegram", "ntfy"))
        self.assertEqual(notify.detect_webhook_format("https://hooks.slack.com/services/T0/B0/XX"), "slack")
        self.assertEqual(notify.detect_webhook_format("https://hooks.slack-gov.com/services/T0/B0/XX"), "slack")
        self.assertEqual(notify.detect_webhook_format("https://discord.com/api/webhooks/123/abc"), "discord")
        self.assertEqual(notify.detect_webhook_format("https://discordapp.com/api/webhooks/123/abc"), "discord")
        self.assertEqual(notify.detect_webhook_format("https://api.telegram.org/bot123:XYZ/sendMessage"), "telegram")
        self.assertEqual(notify.detect_webhook_format("https://ntfy.sh/lookout-alerts"), "ntfy")
        self.assertEqual(notify.detect_webhook_format("https://hooks.example.com/custom"), "json")

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _WebhookHandler)
        srv.hits = []
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            # Invalid format rejected before sending
            ok_bad, msg_bad = notify.webhook(f"http://127.0.0.1:{port}/hook", {"a": 1}, fmt="xml", allow_local=True)
            self.assertFalse(ok_bad)
            self.assertIn("Unsupported webhook format", msg_bad)
            self.assertEqual(srv.hits, [])

            orig_getaddrinfo = socket.getaddrinfo
            orig_create_conn = socket.create_connection
            mapped_hosts = {"hooks.slack.com", "discord.com", "api.telegram.org", "ntfy.sh"}

            def fake_getaddrinfo(host, p, *a, **kw):
                h = str(host or "").rstrip(".").lower()
                if h in mapped_hosts:
                    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", int(p or 80)))]
                return orig_getaddrinfo(host, p, *a, **kw)

            def fake_create_conn(addr, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
                host, _ = addr
                if str(host).lower() in mapped_hosts or str(host) == "93.184.216.34":
                    return orig_create_conn(("127.0.0.1", port), timeout=timeout, source_address=source_address)
                return orig_create_conn(addr, timeout=timeout, source_address=source_address)

            items = [
                {
                    "title": f"Role & <#{k}> > test",
                    "url": f"https://jobs.example.com/{k}",
                    "source_name": "Board",
                    "org": "Acme",
                    "deadline": "2027-05-01",
                }
                for k in range(12)
            ]
            sample_payload = {
                "title": "Lookout: 12 new",
                "message": "Found 12 items",
                "panel_url": "http://127.0.0.1:8780/#/feed",
                "items": items,
            }

            with patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo), \
                 patch.object(socket, "create_connection", side_effect=fake_create_conn):
                # 1. Slack auto-detected
                ok_sl, msg_sl = notify.webhook(f"http://hooks.slack.com:{port}/services/T/B/X", sample_payload)
                self.assertTrue(ok_sl, msg_sl)
                _, _, s_hdrs, s_raw = srv.hits[-1]
                ua_fn = getattr(lookout_extract, "get_user_agent", None)
                expected_ua = ua_fn() if callable(ua_fn) else notify._DEFAULT_USER_AGENT
                self.assertEqual(s_hdrs["user-agent"], expected_ua)
                slack_body = json.loads(s_raw.decode("utf-8"))
                self.assertEqual(slack_body["blocks"][0]["type"], "header")
                self.assertLessEqual(len(slack_body["blocks"][0]["text"]["text"]), 150)
                self.assertEqual(slack_body["blocks"][1]["type"], "section")
                mrkdwn = slack_body["blocks"][1]["text"]["text"]
                self.assertIn("<https://jobs.example.com/0|Role &amp; &lt;#0&gt; &gt; test>", mrkdwn)
                self.assertIn("<http://127.0.0.1:8780/#/feed|Open Lookout>", mrkdwn)
                self.assertIn("and 2 more", mrkdwn)

                # 2. Discord auto-detected
                ok_dc, msg_dc = notify.webhook(f"http://discord.com:{port}/api/webhooks/1/tok", sample_payload)
                self.assertTrue(ok_dc, msg_dc)
                dc_body = json.loads(srv.hits[-1][3].decode("utf-8"))
                self.assertLessEqual(len(dc_body["content"]), 2000)
                embed = dc_body["embeds"][0]
                self.assertEqual(embed["title"], "Lookout: 12 new")
                self.assertEqual(embed["url"], "http://127.0.0.1:8780/#/feed")
                self.assertIn("[Role & <#0> > test](https://jobs.example.com/0)", embed["description"])
                self.assertIn("and 2 more", embed["description"])

                # 3. Telegram auto-detected with URL query chat_id and payload chat_id override
                ok_tg_q, msg_tg_q = notify.webhook(
                    f"http://api.telegram.org:{port}/botABC/sendMessage?chat_id=-100999",
                    sample_payload,
                )
                self.assertTrue(ok_tg_q, msg_tg_q)
                tg_q_body = json.loads(srv.hits[-1][3].decode("utf-8"))
                self.assertTrue(tg_q_body["disable_web_page_preview"])
                self.assertEqual(tg_q_body["chat_id"], "-100999")
                self.assertIn("Role & <#0> > test", tg_q_body["text"])

                tg_payload = dict(sample_payload, chat_id="555123")
                ok_tg_p, msg_tg_p = notify.webhook(
                    f"http://api.telegram.org:{port}/botABC/sendMessage?chat_id=-100999",
                    tg_payload,
                )
                self.assertTrue(ok_tg_p, msg_tg_p)
                tg_p_body = json.loads(srv.hits[-1][3].decode("utf-8"))
                self.assertEqual(tg_p_body["chat_id"], "555123")

                # 4. ntfy explicit fmt
                ok_nt, msg_nt = notify.webhook(f"http://ntfy.sh:{port}/topic1", sample_payload, fmt="ntfy")
                self.assertTrue(ok_nt, msg_nt)
                self.assertIn("text/plain", srv.hits[-1][2]["content-type"])

                # 5. JSON explicit + lookout_extract.get_user_agent override
                with patch.object(lookout_extract, "get_user_agent", return_value="Lookout-Custom/9.9", create=True):
                    ok_js, msg_js = notify.webhook(f"http://127.0.0.1:{port}/json", {"k": "v"}, "json", allow_local=True)
                self.assertTrue(ok_js, msg_js)
                self.assertEqual(srv.hits[-1][2]["user-agent"], "Lookout-Custom/9.9")
                self.assertEqual(json.loads(srv.hits[-1][3].decode("utf-8")), {"k": "v"})
        finally:
            srv.shutdown()
            srv.server_close()

    def test_schedule_env_capture_and_preview_escaping(self):
        special_path = r'/usr/bin:/opt/100%dir/bin:"quoted"\dir'
        special_data = str(self.tmp / 'data %dir "x"')
        special_plug = str(self.tmp / "plugs")
        with patch.dict(os.environ, {
            "PATH": special_path,
            "LOOKOUT_DATA_DIR": special_data,
            "LOOKOUT_PLUGIN_DIR": special_plug,
        }):
            captured = notify._schedule_env()
            self.assertEqual(captured["PATH"], special_path)
            self.assertEqual(captured["LOOKOUT_DATA_DIR"], special_data)
            self.assertEqual(captured["LOOKOUT_PLUGIN_DIR"], special_plug)

            cron = notify.cron_line("07:10")
            self.assertIn("env PATH=", cron)
            self.assertIn("LOOKOUT_DATA_DIR=", cron)
            self.assertIn("LOOKOUT_PLUGIN_DIR=", cron)

            svc, tmr = notify.systemd_units("07:10")
            self.assertIn('Environment="PATH=/usr/bin:/opt/100%%dir/bin:\\"quoted\\"\\\\dir"', svc)
            self.assertIn('Environment="LOOKOUT_DATA_DIR=', svc)
            self.assertIn("%%dir", svc)
            self.assertIn('\\"x\\"', svc)
            self.assertIn('Environment="LOOKOUT_PLUGIN_DIR=', svc)
            self.assertIn("OnCalendar=*-*-* 07:10:00", tmr)

            plist_txt = notify.launchd_plist("07:10")
            parsed = plistlib.loads(plist_txt.encode("utf-8"))
            self.assertEqual(parsed["EnvironmentVariables"], captured)

        with patch.dict(os.environ, {"PATH": os.path.dirname(sys.executable), "LOOKOUT_DATA_DIR": str(self.data_dir)}):
            ok_env, msg_env = notify.test_schedule_env()
            self.assertTrue(ok_env, msg_env)

        ok_missing_dir, _ = notify.test_schedule_env({"PATH": "/usr/bin", "LOOKOUT_DATA_DIR": ""})
        self.assertFalse(ok_missing_dir)

        with patch("shutil.which", return_value=None), patch("os.path.exists", return_value=False):
            ok_no_py, _ = notify.test_schedule_env({"PATH": "/empty", "LOOKOUT_DATA_DIR": str(self.data_dir)})
        self.assertFalse(ok_no_py)

        self.assertEqual(notify.schedule_test_command("task_scheduler"), "schtasks /Run /TN LookoutDailyScan")
        self.assertEqual(notify.schedule_test_command("systemd"), "systemctl --user start lookout-scan.service")
        self.assertRegex(notify.schedule_test_command("launchd"), r"^launchctl kickstart -k gui/\d+/com\.lookout\.dailyscan$")
        cron_cmd = notify.schedule_test_command("crontab")
        self.assertIn("env -i HOME=", cron_cmd)
        self.assertIn("/bin/sh -c", cron_cmd)

        with patch.object(notify, "schedule_status", return_value={"supported": True, "enabled": True, "backend": "systemd"}):
            prev = notify.schedule_preview("08:30")
        self.assertEqual(prev["test_command"], "systemctl --user start lookout-scan.service")

    def test_run_schedule_now_across_backends(self):
        with patch.object(notify, "schedule_status", return_value={"supported": True, "enabled": False, "backend": "systemd"}):
            ok_dis, msg_dis = notify.run_schedule_now()
        self.assertFalse(ok_dis)
        self.assertIn("not enabled", msg_dis.lower())

        # Windows Task Scheduler
        with patch.object(sys, "platform", "win32"), \
             patch.object(notify, "schedule_status", return_value={"supported": True, "enabled": True, "backend": "task_scheduler"}), \
             patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as m_win:
            ok_w, _ = notify.run_schedule_now()
        self.assertTrue(ok_w)
        self.assertEqual(m_win.call_args.args[0], ["schtasks", "/Run", "/TN", notify.TASK_NAME])

        # Linux systemd (wait=False and wait=True)
        with patch.object(sys, "platform", "linux"), \
             patch("shutil.which", side_effect=lambda c: "/usr/bin/systemctl" if c == "systemctl" else None), \
             patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as m_sys:
            ok_s1, _ = notify.run_schedule_now(backend="systemd", wait=False)
            self.assertTrue(ok_s1)
            self.assertEqual(m_sys.call_args.args[0], ["/usr/bin/systemctl", "--user", "start", "--no-block", "lookout-scan.service"])

            ok_s2, _ = notify.run_schedule_now(backend="systemd", wait=True)
            self.assertTrue(ok_s2)
            self.assertEqual(m_sys.call_args.args[0], ["/usr/bin/systemctl", "--user", "start", "lookout-scan.service"])

        # macOS launchd kickstart fallback to launchctl start
        launch_calls = []

        def fake_launchd(cmd, **kw):
            launch_calls.append(cmd)
            if cmd[1] == "kickstart":
                return subprocess.CompletedProcess(cmd, 1, "", "kickstart failed")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(sys, "platform", "darwin"), \
             patch("shutil.which", side_effect=lambda c: "/bin/launchctl" if c == "launchctl" else None), \
             patch("subprocess.run", side_effect=fake_launchd):
            ok_ld, _ = notify.run_schedule_now(backend="launchd")
        self.assertTrue(ok_ld)
        self.assertEqual(launch_calls[0][1], "kickstart")
        self.assertEqual(launch_calls[1], ["/bin/launchctl", "start", notify.LAUNCHD_LABEL])

        # Crontab minimal-env execution (wait=False -> Popen, wait=True -> subprocess.run)
        installed_line = (
            f"15 8 * * * cd '/app' && env PATH='/custom/bin' LOOKOUT_DATA_DIR='/custom/data' "
            f"'/custom/bin/python3' run.py scan --scheduled {notify.CRON_MARKER}"
        )
        with patch.object(sys, "platform", "linux"), \
             patch.object(notify, "_read_crontab", return_value=(True, [installed_line], "")), \
             patch("subprocess.Popen") as m_popen, \
             patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as m_cron_run:
            ok_c1, msg_c1 = notify.run_schedule_now(backend="crontab", wait=False)
            self.assertTrue(ok_c1, msg_c1)
            self.assertEqual(m_popen.call_args.args[0][:2], ["/bin/sh", "-c"])
            self.assertIn("LOOKOUT_DATA_DIR='/custom/data'", m_popen.call_args.args[0][2])
            self.assertNotIn(notify.CRON_MARKER, m_popen.call_args.args[0][2])
            self.assertEqual(
                m_popen.call_args.kwargs["env"],
                {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "SHELL": "/bin/sh"},
            )
            self.assertTrue(m_popen.call_args.kwargs["start_new_session"])

            ok_c2, _ = notify.run_schedule_now(backend="crontab", timeout=25, wait=True)
            self.assertTrue(ok_c2)
            self.assertEqual(m_cron_run.call_args.args[0][:2], ["/bin/sh", "-c"])
            self.assertEqual(m_cron_run.call_args.kwargs["timeout"], 25)
            self.assertEqual(
                m_cron_run.call_args.kwargs["env"],
                {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "SHELL": "/bin/sh"},
            )


if __name__ == "__main__":
    unittest.main()
