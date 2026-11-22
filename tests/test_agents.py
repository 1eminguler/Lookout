"""Unit tests for agent detection, command building, model cache, cancellation and quota handling."""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lookout import agents, db
from tests.helpers import TempLookoutTestCase


class AgentTests(TempLookoutTestCase):
    def test_build_command_for_all_known_agents(self):
        with patch("shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"):
            for aid in agents.AGENTS:
                with self.subTest(agent=aid):
                    cmd_with_model = agents.build_command(aid, "my-model", "/tmp/work", 120)
                    cmd_no_model = agents.build_command(aid, "", "/tmp/work", 120)
                    self.assertTrue(cmd_with_model[0].endswith(agents.AGENTS[aid]["exe"]))
                    self.assertIn("my-model", cmd_with_model)
                    self.assertNotIn("my-model", cmd_no_model)
                    self.assertTrue(any("/tmp/work" in part for part in cmd_with_model))

    def test_custom_command_template_and_errors(self):
        db.set_settings({"custom_command": "mycli run -m {model} --dir {dir} {prompt}"})
        with patch("shutil.which", return_value="/bin/mycli"):
            cmd = agents.build_command("custom", "fast-1", "/tmp/w", 60)
        self.assertEqual(cmd[0], "/bin/mycli")
        self.assertIn("fast-1", cmd)
        self.assertIn("/tmp/w", cmd)
        self.assertTrue(any("TASK.md" in c for c in cmd))

        db.set_settings({"custom_command": "mycli --yes"})
        with patch("shutil.which", return_value="/bin/mycli"):
            cmd2 = agents.build_command("custom", "", "/tmp/w", 60)
        self.assertTrue(cmd2[-1].startswith("Read the file"))

        db.set_settings({"custom_command": 'mycli "unterminated'})
        self.assertIsNone(agents.which("custom"))
        with self.assertRaises(RuntimeError):
            agents.build_command("custom", "", "/tmp/w", 60)

        with self.assertRaises(ValueError):
            agents.build_command("unknown-agent", "", "/tmp/w", 60)

    def test_custom_command_strips_outer_quotes_on_windows(self):
        db.set_settings({"custom_command": 'mycli --dir "{dir}" "{prompt}"'})
        with patch.object(agents.sys, "platform", "win32"), \
             patch("shutil.which", return_value=r"C:\bin\mycli.exe"):
            cmd = agents.build_command("custom", "", r"C:\tmp\work dir", 60)
        self.assertEqual(cmd[0], r"C:\bin\mycli.exe")
        self.assertEqual(cmd[1], "--dir")
        self.assertEqual(cmd[2], r"C:\tmp\work dir")
        self.assertEqual(cmd[3], agents.prompt_for(r"C:\tmp\work dir"))

    def test_detect_browser_tool_and_models_cache(self):
        def fake_which(exe):
            return f"/fake/{exe}" if exe in ("claude", "agy", "opencode", "agent-browser") else None

        def fake_run(cmd, timeout, cwd=None):
            if cmd[1:] == ["--version"]:
                return 0, f"{Path(cmd[0]).name} 1.2.3\n"
            if cmd[0].endswith("agy") and cmd[1:] == ["models"]:
                return 0, "gemini-pro-low\tGemini Pro (low)\ngemini-pro-high\tGemini Pro (high)\n"
            if cmd[0].endswith("opencode") and cmd[1:] == ["models"]:
                return 0, "openai/gpt-5\nanthropic/claude-sonnet-4\nignore this line\n"
            return 1, ""

        with patch.object(agents.shutil, "which", side_effect=fake_which), \
             patch.object(agents, "_run", side_effect=fake_run) as m_run:
            bt = agents.browser_tool()
            self.assertTrue(bt["installed"])
            self.assertEqual(bt["version"], "1.2.3")

            det = {a["id"]: a for a in agents.detect()}
            self.assertTrue(det["claude"]["installed"])
            self.assertEqual(det["claude"]["version"], "1.2.3")
            self.assertFalse(det["codex"]["installed"])

            self.assertTrue(any(k == "haiku" for k, _ in agents.models("claude")))
            agy_models = agents.models("agy")
            self.assertEqual(agy_models, [("gemini-pro-low", "Gemini Pro (low)"), ("gemini-pro-high", "Gemini Pro (high)")])
            oc_models = agents.models("opencode")
            self.assertEqual(oc_models, [("openai/gpt-5", "openai/gpt-5"), ("anthropic/claude-sonnet-4", "anthropic/claude-sonnet-4")])

            calls_before = m_run.call_count
            self.assertEqual(agents.models("agy"), agy_models)
            self.assertEqual(m_run.call_count, calls_before)

    def test_quota_detection_and_resets_at(self):
        self.assertTrue(agents.QUOTA.search("Error: 429 Too Many Requests"))
        self.assertTrue(agents.QUOTA.search("RESOURCE_EXHAUSTED"))
        self.assertIsNotNone(agents._resets_at("Quota resets in 4h29m"))
        self.assertIsNotNone(agents._resets_at("Limit reset in 1h 5m 10s"))
        self.assertIsNone(agents._resets_at("Quota exceeded, try later"))

    def test_quota_block_records_and_skips_subsequent_calls(self):
        with patch("shutil.which", return_value="/bin/claude"), \
             patch.object(agents, "run_tracked", return_value=(1, "429 quota resets in 2h")) as run_mock:
            with self.assertRaises(RuntimeError):
                agents.run_task("claude", "sonnet", self.work_dir, 60)
            self.assertEqual(run_mock.call_count, 1)
            with self.assertRaises(RuntimeError) as ctx:
                agents.run_task("claude", "sonnet", self.work_dir, 60)
            self.assertIn("quota is used up until", str(ctx.exception))
            self.assertEqual(run_mock.call_count, 1)

    def test_cancel_kills_running_subprocess_and_closes_browser_session(self):
        result = {}

        def worker():
            try:
                agents.run_tracked([sys.executable, "-c", "import time; time.sleep(30)"],
                                   timeout=30, tag="scan", session="lookout-test-sess")
            except agents.CancelledError as e:
                result["cancelled"] = str(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with agents._proc_lock:
                if agents._running:
                    break
            time.sleep(0.05)

        t0 = time.monotonic()
        with patch.object(agents, "close_browser_session") as close_mock:
            killed = agents.cancel("scan")
        t.join(timeout=5.0)
        elapsed = time.monotonic() - t0

        self.assertGreaterEqual(killed, 1)
        self.assertFalse(t.is_alive())
        self.assertEqual(result.get("cancelled"), "cancelled")
        self.assertLess(elapsed, 4.0)
        close_mock.assert_called_once_with("lookout-test-sess")


if __name__ == "__main__":
    unittest.main()
