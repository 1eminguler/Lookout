"""Unit tests for run.py CLI scan execution, window lock, background launch, and flags."""
import builtins
import contextlib
import io
import os
import subprocess
import sys
import time
import types
import unittest
from unittest.mock import MagicMock, patch

import run
from lookout import agents, db, notify, scanner, server
from tests.helpers import TempLookoutTestCase


class RunTests(TempLookoutTestCase):
    def test_open_window_falls_back_on_import_error_and_start_error(self):
        real_import = builtins.__import__

        def missing_webview(name, *args, **kwargs):
            if name == "webview":
                raise ImportError("No module named 'webview'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=missing_webview):
            self.assertFalse(run.open_window(start_server=False))

        fake_webview = types.ModuleType("webview")
        fake_webview.create_window = MagicMock(return_value=MagicMock())
        fake_webview.start = MagicMock(side_effect=RuntimeError("No GTK/Qt backend"))

        fake_srv = MagicMock()
        with patch.dict(sys.modules, {"webview": fake_webview}), \
             patch.object(server, "PORT", self.port), \
             patch.object(run, "URL", f"http://127.0.0.1:{self.port}/"), \
             patch.object(server, "make_server", return_value=fake_srv), \
             patch.object(run, "port_busy", return_value=True):
            self.assertFalse(run.open_window(start_server=True))
        fake_srv.shutdown.assert_called_once()
        fake_srv.server_close.assert_called_once()

        # main() opens web browser when open_window returns False
        with patch.object(sys, "argv", ["run.py"]), \
             patch.object(run, "port_busy", return_value=False), \
             patch.object(run, "open_window", return_value=False), \
             patch.object(run.webbrowser, "open") as m_open, \
             patch.object(server, "serve") as m_serve:
            run.main()
        m_serve.assert_called_once()
        # Timer starts quickly to open URL in browser
        self.assertTrue(m_serve.called or m_open.called)

    def test_internet_check_tries_source_hosts_then_ip_and_dns_fallbacks(self):
        db.add_source("A", "https://first.example.com/careers")
        db.add_source("B", "http://second.example.com/jobs")
        with patch.object(run.socket, "create_connection", side_effect=OSError) as connect, \
             patch.object(run.socket, "getaddrinfo", return_value=[]) as resolve:
            self.assertTrue(run.wait_for_internet(10))
        self.assertEqual([call.args[0] for call in connect.call_args_list], [
            ("first.example.com", 443), ("second.example.com", 80), ("1.1.1.1", 443), ("8.8.8.8", 443),
        ])
        resolve.assert_called_once_with("first.example.com", None)

    def test_internet_check_honors_deadline_when_resolution_stalls(self):
        db.add_source("Careers", "https://example.com/jobs")
        release = run.threading.Event()
        stopped = run.threading.Event()

        def stalled_connection(*args, **kwargs):
            release.wait(3)
            stopped.set()
            raise OSError

        start = time.monotonic()
        with patch.object(run.socket, "create_connection", side_effect=stalled_connection) as connect:
            try:
                self.assertFalse(run.wait_for_internet(0.2))
                self.assertLess(time.monotonic() - start, 1.5)
                # The deadline is what matters; a slow runner may start one more probe just before it.
                self.assertGreaterEqual(connect.call_count, 1)
                self.assertLessEqual(connect.call_count, 2)
            finally:
                release.set()
                stopped.wait(3)

    def test_cmd_scan_runs_offline_without_http_token(self):
        db.set_settings({"notify": "1"})
        plugin_path = self.plugin_dir / "cli_p.py"
        plugin_path.write_text(
            "SOURCES = [{'name': 'CliPlug', 'url': 'https://cli.example.com', "
            "'fetch': lambda: [{'title': 'CLI Item', 'url': 'https://cli.example.com/1'}]}]\n",
            encoding="utf-8",
        )
        panel_url = f"http://127.0.0.1:{self.port}/"

        # 1st scan sets silent baseline without desktop notification
        with patch.object(run, "URL", panel_url), \
             patch.object(run, "wait_for_internet", return_value=False), \
             patch.object(run, "start_ui_in_background") as m_bg, \
             patch.object(notify, "desktop") as m_desk, \
             patch.object(notify, "mail_digest", return_value=(False, "not configured")):
            run.cmd_scan(scheduled=False)

        src = db.source_by_plugin("CliPlug")
        self.assertIsNotNone(src)
        self.assertEqual(src["baseline_done"], 1)
        items = db.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "CLI Item")
        self.assertEqual(items[0]["notified"], 1)
        m_desk.assert_not_called()
        m_bg.assert_not_called()

        # 2nd scan with a new item triggers before_desktop and notifies once via notify_after_scan
        # When before_desktop writes a fresh .launch_secret, desktop notification gets ?k=<secret> while email stays bare URL
        plugin_path.write_text(
            "SOURCES = [{'name': 'CliPlug', 'url': 'https://cli.example.com', "
            "'fetch': lambda: ["
            "{'title': 'CLI Item', 'url': 'https://cli.example.com/1'}, "
            "{'title': 'CLI Item 2', 'url': 'https://cli.example.com/2'}"
            "]}]\n",
            encoding="utf-8",
        )
        db.set_settings({"smtp_user": "u@example.com", "smtp_password": "pw", "mail_to": "to@example.com"})
        sec_file = self.data_dir / ".launch_secret"
        call_order = []

        def fake_start_bg():
            call_order.append("before_desktop")
            sec_file.write_text("fresh_bg_secret\n", encoding="utf-8")

        with patch.object(run, "URL", panel_url), \
             patch.object(run, "wait_for_internet", return_value=True), \
             patch.object(run, "start_ui_in_background", side_effect=fake_start_bg) as m_bg, \
             patch.object(scanner, "notify_after_scan", wraps=scanner.notify_after_scan) as m_after, \
             patch.object(
                 notify, "desktop",
                 side_effect=lambda *a, **kw: (call_order.append("desktop"), (True, "ok"))[1],
             ) as m_desk, \
             patch.object(notify, "mail_digest", return_value=(True, "sent")) as m_mail:
            run.cmd_scan(scheduled=False)

        m_after.assert_called_once()
        self.assertIs(m_after.call_args.kwargs.get("before_desktop"), m_bg)
        m_bg.assert_called_once()
        m_desk.assert_called_once()
        self.assertEqual(call_order, ["before_desktop", "desktop"])
        self.assertEqual(m_desk.call_args[0][0], "1 new item")
        self.assertEqual(m_desk.call_args[0][2], f"{panel_url}?k=fresh_bg_secret")
        m_mail.assert_called_once()
        self.assertEqual(m_mail.call_args[0][1], panel_url)
        self.assertNotIn("fresh_bg_secret", m_mail.call_args[0][1])
        items = db.items()
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i["notified"] == 1 for i in items))

        # Saved item with remind_on == today triggers agenda reminder notification with launch secret URL
        first_item = next(i for i in items if i["title"] == "CLI Item")
        today = time.strftime("%Y-%m-%d")
        self.assertTrue(db.set_item(first_item["id"], action="saved", remind_on=today))
        with patch.object(run, "URL", panel_url), \
             patch.object(run, "wait_for_internet", return_value=True), \
             patch.object(run, "start_ui_in_background") as m_bg, \
             patch.object(notify, "desktop", return_value=(True, "ok")) as m_desk, \
             patch.object(notify, "mail_digest", return_value=(False, "not configured")):
            run.cmd_scan(scheduled=False)

        m_bg.assert_called_once()
        m_desk.assert_called_once()
        self.assertEqual(m_desk.call_args[0][0], "Reminder: CLI Item")
        self.assertEqual(m_desk.call_args[0][2], f"{panel_url}?k=fresh_bg_secret")

    def test_window_pid_lockfile_alive_stale_and_release(self):
        lock_path = self.data_dir / "window.pid"
        panel_url = f"http://127.0.0.1:{self.port}/"

        with patch.object(server, "PORT", self.port):
            self.assertTrue(run.acquire_window_lock())
        self.assertTrue(lock_path.is_file())
        self.assertEqual(
            lock_path.read_text(encoding="utf-8").splitlines(),
            [str(os.getpid()), str(self.port)],
        )

        # Different alive PID blocks second lock and open_window() reports URL cleanly
        lock_path.write_text(f"99999\n{self.port}\n", encoding="utf-8")
        fake_webview = types.ModuleType("webview")
        fake_webview.create_window = MagicMock()
        fake_webview.start = MagicMock()
        out = io.StringIO()
        with patch.object(db, "_pid_alive", return_value=True) as m_alive, \
             patch.object(server, "PORT", self.port), \
             patch.object(run, "URL", panel_url), \
             patch.object(server, "make_server") as m_make, \
             patch.dict(sys.modules, {"webview": fake_webview}), \
             contextlib.redirect_stdout(out):
            self.assertFalse(run.acquire_window_lock())
            self.assertTrue(run.open_window(start_server=True))
        self.assertEqual(m_alive.call_count, 2)
        m_alive.assert_called_with(99999)
        m_make.assert_not_called()
        fake_webview.create_window.assert_not_called()
        fake_webview.start.assert_not_called()
        msg = out.getvalue()
        self.assertIn(panel_url, msg)
        self.assertIn("already open", msg.lower())

        # release_window_lock() does not delete a lock held by another PID
        run.release_window_lock()
        self.assertTrue(lock_path.is_file())

        # Stale dead PID is cleanly overwritten
        with patch.object(db, "_pid_alive", return_value=False) as m_dead, \
             patch.object(server, "PORT", self.port):
            self.assertTrue(run.acquire_window_lock())
        m_dead.assert_called_once_with(99999)
        self.assertEqual(
            lock_path.read_text(encoding="utf-8").splitlines(),
            [str(os.getpid()), str(self.port)],
        )

        # release_window_lock() removes lock when PID matches os.getpid()
        run.release_window_lock()
        self.assertFalse(lock_path.exists())

    def test_start_ui_in_background_posix_start_new_session(self):
        for plat in ("linux", "darwin"):
            with self.subTest(platform=plat), \
                 patch.object(sys, "platform", plat), \
                 patch.object(run, "port_busy", return_value=False), \
                 patch.object(subprocess, "Popen") as m_popen:
                run.start_ui_in_background()
            m_popen.assert_called_once()
            args, kwargs = m_popen.call_args
            self.assertEqual(args[0][1:], [str(run.ROOT / "run.py"), "--background"])
            self.assertIs(kwargs.get("start_new_session"), True)
            self.assertNotIn("creationflags", kwargs)

        with patch.object(run, "port_busy", return_value=True), \
             patch.object(subprocess, "Popen") as m_popen:
            run.start_ui_in_background()
        m_popen.assert_not_called()

    def test_custom_command_cli_flag(self):
        try:
            with patch.object(sys, "argv", ["run.py", "--custom-command", "mycli {prompt}"]), \
                 patch.object(run, "port_busy", return_value=False), \
                 patch.object(run, "open_window", return_value=True), \
                 patch.object(agents, "set_cli_custom_command", wraps=agents.set_cli_custom_command) as m_set:
                run.main()
            m_set.assert_called_once_with("mycli {prompt}")
            self.assertEqual(agents.get_custom_command(), "mycli {prompt}")

            agents.set_cli_custom_command(None)
            err = io.StringIO()
            with patch.object(sys, "argv", ["run.py", "--custom-command", "mycli && id"]), \
                 contextlib.redirect_stderr(err), \
                 self.assertRaises(SystemExit) as cm:
                run.main()
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("Invalid --custom-command", err.getvalue())
            self.assertEqual(agents.get_custom_command(), "")
        finally:
            agents.set_cli_custom_command(None)

    def test_launch_url_uses_server_secret_or_secret_file(self):
        srv_with_secret = types.SimpleNamespace(launch_secret="sec123", server_address=("127.0.0.1", self.port))
        self.assertEqual(run._launch_url(srv_with_secret), f"http://127.0.0.1:{self.port}/?k=sec123")

        srv_without_secret = types.SimpleNamespace(launch_secret=None, server_address=("127.0.0.1", self.port))
        self.assertEqual(run._launch_url(srv_without_secret), f"http://127.0.0.1:{self.port}/")

        sec_file = self.data_dir / ".launch_secret"
        with patch.object(server, "PORT", self.port):
            self.assertEqual(run._launch_url(), f"http://127.0.0.1:{self.port}/")
            sec_file.write_text("  file_tok_456 \n", encoding="utf-8")
            self.assertEqual(run._launch_url(), f"http://127.0.0.1:{self.port}/?k=file_tok_456")

    def test_headless_prints_link_on_start_and_after_each_sign_in(self):
        captured = {}

        def fake_serve(on_start=None, idle_minutes=0):
            srv = types.SimpleNamespace(launch_secret="first")
            on_start(srv)
            captured["srv"] = srv
            srv.on_new_secret("second")

        out = io.StringIO()
        with patch.object(server, "PORT", self.port), patch.object(run, "port_busy", return_value=False), \
             patch.object(server, "serve", side_effect=fake_serve), contextlib.redirect_stdout(out):
            run.cmd_headless()
        text = out.getvalue()
        self.assertIn(f"http://127.0.0.1:{self.port}/?k=first", text)
        self.assertIn(f"http://127.0.0.1:{self.port}/?k=second", text)
        self.assertTrue(callable(captured["srv"].on_new_secret))

    def test_headless_refuses_busy_port_and_env_selects_headless(self):
        err = io.StringIO()
        with patch.object(run, "port_busy", return_value=True), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                run.cmd_headless()
        self.assertIn("--print-url", err.getvalue())

        with patch.dict(os.environ, {"LOOKOUT_HEADLESS": "1"}), patch.object(sys, "argv", ["run.py"]), \
             patch.object(run, "cmd_headless") as headless, patch.object(run, "open_window") as window:
            run.main()
        headless.assert_called_once()
        window.assert_not_called()

    def test_print_url_reads_secret_of_running_instance(self):
        sec_file = self.data_dir / ".launch_secret"
        sec_file.unlink(missing_ok=True)
        out = io.StringIO()
        with patch.object(server, "PORT", self.port), patch.object(run, "port_busy", return_value=True):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run.cmd_print_url()
            sec_file.write_text("tok789", encoding="utf-8")
            with contextlib.redirect_stdout(out):
                run.cmd_print_url()
        self.assertEqual(out.getvalue().strip(), f"http://127.0.0.1:{self.port}/?k=tok789")
        with patch.object(run, "port_busy", return_value=False), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                run.cmd_print_url()

    def test_consuming_a_secret_calls_the_new_secret_hook(self):
        with patch.object(server, "PORT", self.port):
            srv = server.make_server()
        try:
            seen = []
            srv.on_new_secret = seen.append
            first = srv.launch_secret
            handler = server.Handler.__new__(server.Handler)
            handler.server = srv
            self.assertTrue(handler._consume_launch_secret(first))
            self.assertEqual(seen, [srv.launch_secret])
            self.assertNotEqual(seen[0], first)
        finally:
            srv.server_close()

    def test_maximize_fills_work_area_on_windows_and_restores(self):
        api = run.WindowApi()
        api._window = MagicMock()
        api._window.native.Handle = 1234
        placed = []
        with patch.object(run.sys, "platform", "win32"), \
             patch.object(run, "_win_window_rect", return_value=(100, 100, 900, 700)), \
             patch.object(run, "_win_work_area", return_value=(0, 0, 1920, 1032)), \
             patch.object(run, "_win_place", side_effect=lambda h, r: placed.append((h, r)) or True):
            api.toggle_maximize()
            api.toggle_maximize()
        self.assertEqual(placed, [(1234, (0, 0, 1920, 1032)), (1234, (100, 100, 900, 700))])
        api._window.maximize.assert_not_called()  # the native maximize would cover the taskbar
        api._window.restore.assert_not_called()

    def test_maximize_falls_back_to_native_outside_windows(self):
        api = run.WindowApi()
        api._window = MagicMock()
        with patch.object(run.sys, "platform", "linux"):
            api.toggle_maximize()
            api.toggle_maximize()
        api._window.maximize.assert_called_once()
        api._window.restore.assert_called_once()


if __name__ == "__main__":
    unittest.main()
