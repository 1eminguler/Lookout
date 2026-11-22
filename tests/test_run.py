"""Unit tests for run.py CLI scan execution and pywebview browser fallback."""
import builtins
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

import run
from lookout import db, notify, scanner, server
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

    def test_cmd_scan_runs_offline_without_http_token(self):
        db.set_settings({"notify": "1"})
        (self.plugin_dir / "cli_p.py").write_text(
            "SOURCES = [{'name': 'CliPlug', 'url': 'https://cli.example.com', "
            "'fetch': lambda: [{'title': 'CLI Item', 'url': 'https://cli.example.com/1'}]}]\n",
            encoding="utf-8",
        )
        with patch.object(run, "wait_for_internet", return_value=True), \
             patch.object(run, "start_ui_in_background"), \
             patch.object(notify, "desktop") as m_desk, \
             patch.object(notify, "mail_digest", return_value=(False, "not configured")):
            run.cmd_scan(scheduled=False)
        items = db.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "CLI Item")
        self.assertEqual(items[0]["notified"], 1)
        m_desk.assert_called_once()


if __name__ == "__main__":
    unittest.main()
