"""Unit tests for desktop notifications, TLS/SSL SMTP mail digest, webhooks and OS scheduling."""
import http.server
import json
import plistlib
import smtplib
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from lookout import db, notify
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

                ok_to, msg_to = notify.webhook(f"http://hooks.example:{port}/slow", {"x": 1}, timeout=0.5)
                self.assertFalse(ok_to)
                self.assertIn("timed out", msg_to.lower())
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
