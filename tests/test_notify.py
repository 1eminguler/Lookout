"""Unit tests for desktop notifications, SMTP mail digest and scheduling in lookout.notify."""
import smtplib
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from lookout import db, notify
from tests.helpers import TempLookoutTestCase


class NotifyTests(TempLookoutTestCase):
    def test_schedule_windows_and_non_windows(self):
        with patch.object(sys, "platform", "win32"):
            with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run_mock:
                st = notify.schedule_status(use_cache=False)
                self.assertEqual(st, {"supported": True, "enabled": True})
                ok, msg = notify.set_schedule(True, "09:15")
                self.assertTrue(ok)
                self.assertIn("09:15", msg)
                self.assertTrue(run_mock.called)

                ok_bad, _ = notify.set_schedule(True, "25:99")
                self.assertFalse(ok_bad)

                ok_off, _ = notify.set_schedule(False, "09:15")
                self.assertTrue(ok_off)

            with patch("subprocess.run", side_effect=OSError("missing")):
                self.assertEqual(notify.schedule_status(use_cache=False), {"supported": True, "enabled": False})
                ok_err, _ = notify.set_schedule(True, "08:30")
                self.assertFalse(ok_err)

        for plat in ("linux", "darwin"):
            with patch.object(sys, "platform", plat):
                st = notify.schedule_status(use_cache=False)
                self.assertFalse(st["supported"])
                self.assertFalse(st["enabled"])
                self.assertIn("crontab", st["hint"])
                ok, hint = notify.set_schedule(True, "08:30")
                self.assertFalse(ok)
                self.assertIn("crontab", hint)

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

    def test_mail_digest(self):
        ok, msg = notify.mail_digest([], "http://127.0.0.1:8780/")
        self.assertFalse(ok)
        self.assertIn("not configured", msg)

        db.set_settings({"smtp_user": "a@example.com", "smtp_password": "secret", "mail_to": "b@example.com",
                         "smtp_port": "badport"})
        ok_port, msg_port = notify.mail_digest([], "http://127.0.0.1:8780/")
        self.assertFalse(ok_port)
        self.assertIn("port", msg_port.lower())

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
        srv.login.assert_called_once_with("a@example.com", "secret")
        srv.sendmail.assert_called_once()

        for exc, needle in [
            (smtplib.SMTPAuthenticationError(535, b"bad auth"), "authentication failed"),
            (smtplib.SMTPConnectError(421, b"down"), "Could not connect"),
            (TimeoutError("timed out"), "Timed out"),
            (smtplib.SMTPException("generic"), "SMTP error"),
            (OSError("refused"), "Could not send"),
        ]:
            with patch("smtplib.SMTP", side_effect=exc):
                ok_fail, msg_fail = notify.mail_digest(items, "http://127.0.0.1:8780/")
            self.assertFalse(ok_fail)
            self.assertIn(needle, msg_fail)


if __name__ == "__main__":
    unittest.main()
