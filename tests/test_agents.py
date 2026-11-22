"""Unit tests for agent detection, command building, model cache, cancellation and quota handling."""
from datetime import datetime, timedelta, timezone
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lookout import agents, db
from tests.helpers import TempLookoutTestCase


class AgentTests(TempLookoutTestCase):
    def setUp(self) -> None:
        super().setUp()
        agents.set_cli_custom_command(None)

    def tearDown(self) -> None:
        agents.set_cli_custom_command(None)
        super().tearDown()

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

    def test_validate_custom_command_rejects_shell_metacharacters(self):
        self.assertEqual(agents.validate_custom_command(""), "")
        self.assertEqual(
            agents.validate_custom_command("mycli run -m {model} {prompt}"),
            "mycli run -m {model} {prompt}",
        )
        for bad in (
            "mycli --yes && rm -rf /",
            "mycli --yes || echo pwn",
            "mycli --yes | cat",
            "mycli --yes; id",
            "mycli `id`",
            "mycli $(whoami)",
            "mycli --yes\necho pwn",
            'mycli "unterminated',
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    agents.validate_custom_command(bad)
                db.set_settings({"custom_command": bad})
                self.assertIsNone(agents.which("custom"))
                with self.assertRaises(RuntimeError):
                    agents.build_command("custom", "", "/tmp/w", 60)

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

    def test_claude_task_tools_per_task_kind(self):
        def allowed_tools(cmd):
            start = cmd.index("--allowedTools") + 1
            end = cmd.index("--model") if "--model" in cmd else len(cmd)
            return cmd[start:end]

        with patch("shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"):
            classify_cmd = agents.build_command("claude", "sonnet", "/tmp/w", 120, task_kind="classify")
            classify_tools = allowed_tools(classify_cmd)
            self.assertEqual(classify_tools, ["Read", "Write"])
            self.assertFalse(any("agent-browser" in t for t in classify_tools))
            for forbidden in ("WebFetch", "WebSearch", "Edit"):
                self.assertNotIn(forbidden, classify_tools)
            self.assertEqual(classify_cmd[-2:], ["--model", "sonnet"])

            quick_tools = allowed_tools(
                agents.build_command("claude", "sonnet", "/tmp/w", 120, task_kind="quick_read")
            )
            self.assertEqual(quick_tools, agents.CLAUDE_TASK_TOOLS["quick_read"])
            self.assertTrue(any("agent-browser" in t for t in quick_tools))
            self.assertIn("Read", quick_tools)
            self.assertIn("Write", quick_tools)
            for forbidden in ("Edit", "WebFetch", "WebSearch"):
                self.assertNotIn(forbidden, quick_tools)

            learn_tools = allowed_tools(
                agents.build_command("claude", "sonnet", "/tmp/w", 120, task_kind="learn")
            )
            self.assertEqual(learn_tools, agents.CLAUDE_TASK_TOOLS["learn"])
            self.assertTrue(any("agent-browser" in t for t in learn_tools))
            self.assertTrue(any("test_extractor.py" in t for t in learn_tools))
            for expected in ("Read", "Write", "Edit", "WebFetch"):
                self.assertIn(expected, learn_tools)
            self.assertNotIn("WebSearch", learn_tools)

            discover_tools = allowed_tools(
                agents.build_command("claude", "sonnet", "/tmp/w", 120, task_kind="discover")
            )
            self.assertEqual(discover_tools, agents.CLAUDE_TASK_TOOLS["discover"])
            self.assertTrue(any("agent-browser" in t for t in discover_tools))
            for expected in ("Read", "Write", "Edit", "WebSearch", "WebFetch"):
                self.assertIn(expected, discover_tools)
            self.assertFalse(any("test_extractor.py" in t for t in discover_tools))

            default_tools = allowed_tools(agents.build_command("claude", "", "/tmp/w", 120))
            self.assertEqual(default_tools, agents.CLAUDE_TASK_TOOLS["default"])

            gemini_cmd = agents.build_command("gemini", "gemini-2.5-flash", "/tmp/w", 120, task_kind="classify")
            self.assertTrue(gemini_cmd[0].endswith("gemini"))
            self.assertIn("gemini-2.5-flash", gemini_cmd)

    def test_custom_command_precedence_file_and_cli_override(self):
        cmd_path = agents.custom_command_path()
        self.assertEqual(cmd_path, self.data_dir / "agent_command.txt")

        def detect_custom(expected_exe=None):
            which_fn = (lambda exe: f"/bin/{exe}" if exe == expected_exe else None) if expected_exe else (lambda exe: None)
            with patch("shutil.which", side_effect=which_fn), patch.object(agents, "_run", return_value=(1, "")):
                det = {a["id"]: a for a in agents.detect()}["custom"]
                cmd = agents.build_command("custom", "m1", "/tmp/w", 60) if expected_exe else None
            return det, cmd

        det_empty, _ = detect_custom()
        self.assertFalse(det_empty["configured"])
        self.assertFalse(det_empty["installed"])
        self.assertIsNone(det_empty["source"])
        self.assertEqual(det_empty["config_path"], str(cmd_path))

        db.set_settings({"custom_command": "dbcli run -m {model} {prompt}"})
        self.assertEqual(agents.get_custom_command(), "dbcli run -m {model} {prompt}")
        det_db, cmd_db = detect_custom("dbcli")
        self.assertTrue(det_db["configured"])
        self.assertTrue(det_db["installed"])
        self.assertEqual(det_db["source"], "db")
        self.assertEqual(det_db["config_path"], str(cmd_path))
        self.assertEqual(cmd_db[0], "/bin/dbcli")

        cmd_path.write_text(
            "\n   \n# comment line\n   # indented comment\n  filecli exec --dir {dir} {prompt}  \nothercli ignored\n",
            encoding="utf-8",
        )
        self.assertEqual(agents.get_custom_command(), "filecli exec --dir {dir} {prompt}")
        det_file, cmd_file = detect_custom("filecli")
        self.assertTrue(det_file["configured"])
        self.assertTrue(det_file["installed"])
        self.assertEqual(det_file["source"], "file")
        self.assertEqual(det_file["config_path"], str(cmd_path))
        self.assertEqual(cmd_file[0], "/bin/filecli")

        try:
            for bad in (
                "clicli && rm -rf /",
                "clicli || echo bad",
                "clicli | cat",
                "clicli; id",
                "clicli `id`",
                "clicli $(whoami)",
                "clicli\necho bad",
            ):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        agents.set_cli_custom_command(bad)

            self.assertEqual(agents.get_custom_command(), "filecli exec --dir {dir} {prompt}")

            cleaned = agents.set_cli_custom_command("  clicli run -m {model} {prompt}  ")
            self.assertEqual(cleaned, "clicli run -m {model} {prompt}")
            self.assertEqual(agents.get_custom_command(), "clicli run -m {model} {prompt}")

            det_cli, cmd_cli = detect_custom("clicli")
            self.assertTrue(det_cli["configured"])
            self.assertTrue(det_cli["installed"])
            self.assertEqual(det_cli["source"], "cli")
            self.assertEqual(det_cli["config_path"], str(cmd_path))
            self.assertEqual(cmd_cli[0], "/bin/clicli")

            with self.assertRaises(ValueError):
                agents.set_cli_custom_command("clicli && bad")
            self.assertEqual(agents.get_custom_command(), "clicli run -m {model} {prompt}")
        finally:
            agents.set_cli_custom_command(None)

        self.assertEqual(agents.get_custom_command(), "filecli exec --dir {dir} {prompt}")
        cmd_path.write_text("# only comments\n\n   \n", encoding="utf-8")
        self.assertEqual(agents.get_custom_command(), "dbcli run -m {model} {prompt}")

    def test_resets_at_parses_iso_clock_and_relative_formats(self):
        expected_utc_iso = (
            datetime(2027, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
            .astimezone()
            .replace(tzinfo=None)
            .strftime(db.TS)
        )
        self.assertEqual(
            agents._resets_at("Quota exhausted until 2027-01-15T12:30:00Z"),
            expected_utc_iso,
        )
        self.assertEqual(
            agents._resets_at("Limit resets at 2027-01-15 14:45:00"),
            "2027-01-15 14:45:00",
        )

        now_local = datetime.now()
        res_1830 = agents._resets_at("Usage limit resets at 18:30")
        self.assertIsNotNone(res_1830)
        dt_1830 = datetime.strptime(res_1830, db.TS)
        self.assertEqual((dt_1830.hour, dt_1830.minute, dt_1830.second), (18, 30, 0))
        self.assertGreater(dt_1830, now_local - timedelta(seconds=2))
        self.assertLessEqual(dt_1830, now_local + timedelta(days=1, seconds=2))

        now_utc = datetime.now(timezone.utc)
        res_915 = agents._resets_at("Quota will be available at 9:15 am UTC")
        self.assertIsNotNone(res_915)
        dt_915_utc = datetime.strptime(res_915, db.TS).astimezone(timezone.utc)
        self.assertEqual((dt_915_utc.hour, dt_915_utc.minute, dt_915_utc.second), (9, 15, 0))
        self.assertGreater(dt_915_utc, now_utc - timedelta(seconds=60))
        self.assertLessEqual(dt_915_utc, now_utc + timedelta(days=1, seconds=60))

        t_before = datetime.now()
        res_rel = agents._resets_at("Too many requests, try again in 1h 20m")
        t_after = datetime.now()
        self.assertIsNotNone(res_rel)
        dt_rel = datetime.strptime(res_rel, db.TS)
        self.assertGreaterEqual(dt_rel, t_before + timedelta(minutes=80) - timedelta(seconds=2))
        self.assertLessEqual(dt_rel, t_after + timedelta(minutes=80) + timedelta(seconds=2))

    def test_quota_fallback_cooldown_when_no_reset_time(self):
        t_before = datetime.now()
        with patch("shutil.which", return_value="/bin/claude"), \
             patch.object(agents, "run_tracked", return_value=(1, "Error: 429 Too Many Requests")) as run_mock:
            with self.assertRaises(RuntimeError) as ctx1:
                agents.run_task("claude", "sonnet", self.work_dir, 60)
            self.assertIn("quota is used up", str(ctx1.exception))
            self.assertEqual(run_mock.call_count, 1)

            blocked = db.agent_blocked_until("claude", "sonnet")
            self.assertIsNotNone(blocked)
            blocked_dt = datetime.strptime(blocked, db.TS)
            expected = timedelta(minutes=agents.DEFAULT_QUOTA_COOLDOWN_MINUTES)
            self.assertGreaterEqual(blocked_dt, t_before + expected - timedelta(seconds=5))
            self.assertLessEqual(blocked_dt, datetime.now() + expected + timedelta(seconds=5))

            with self.assertRaises(RuntimeError) as ctx2:
                agents.run_task("claude", "sonnet", self.work_dir, 60)
            self.assertIn("quota is used up until", str(ctx2.exception))
            self.assertEqual(run_mock.call_count, 1)

    def test_quota_tail_and_run_proc_stderr_vs_buried_stdout(self):
        clean_tail = "\n".join(f"Regular output line {i}" for i in range(25))
        buried_stdout = "Scraped site mentions 429 and quota exceeded\n" + clean_tail

        self.assertIsNone(agents.QUOTA.search(agents._quota_tail(buried_stdout, "")))
        self.assertIsNotNone(agents.QUOTA.search(agents._quota_tail(buried_stdout, "HTTP 429 quota exceeded\n")))

        self.assertIs(agents.run_tracked, agents._DEFAULT_RUN_TRACKED)
        with patch("shutil.which", return_value="/bin/claude"), \
             patch.object(agents, "_run_proc", return_value=(1, buried_stdout, "")) as proc_mock:
            out = agents.run_task("claude", "sonnet", self.work_dir, 60)
            self.assertIn("Regular output line 24", out)
            self.assertIsNone(db.agent_blocked_until("claude", "sonnet"))
            self.assertEqual(proc_mock.call_count, 1)

        with patch("shutil.which", return_value="/bin/claude"), \
             patch.object(agents, "_run_proc", return_value=(1, clean_tail, "Error: quota exceeded (429)\n")) as proc_mock:
            with self.assertRaises(RuntimeError) as ctx1:
                agents.run_task("claude", "sonnet", self.work_dir, 60)
            self.assertIn("quota is used up", str(ctx1.exception))
            self.assertIsNotNone(db.agent_blocked_until("claude", "sonnet"))
            self.assertEqual(proc_mock.call_count, 1)

            with self.assertRaises(RuntimeError) as ctx2:
                agents.run_task("claude", "sonnet", self.work_dir, 60)
            self.assertIn("quota is used up until", str(ctx2.exception))
            self.assertEqual(proc_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
