"""Tests for extractor AST/runtime sandbox hardening, process-tree timeouts, cancel, output caps and HTTP helpers."""
import ctypes
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from lookout import agents, extractors, lookout_extract
from tests.helpers import LookoutTestCase


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class _StubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/ok":
            body = b"hello world"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/json":
            body = json.dumps({"items": [1, 2]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/badjson":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{not json")
        elif self.path == "/big":
            body = b"A" * 4096
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/big-noclen":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"B" * 4096)
        elif self.path == "/slow":
            time.sleep(0.5)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"late")
            except OSError:
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        got = json.loads(self.rfile.read(n).decode("utf-8"))
        body = json.dumps({"echo": got}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestExtractors(LookoutTestCase):
    def test_valid_extractor_runs(self):
        code = (
            "import json, re\n"
            "from urllib.parse import urljoin\n"
            "from lookout_extract import absolute\n"
            "def _helper(x):\n"
            "    return '{} - {}'.format(x, 'ok')\n"
            "def fetch():\n"
            "    nums = [i for i in (n * 2 for n in range(2))]\n"
            "    fn = lambda v: v + 1\n"
            "    return [{'title': _helper(fn(nums[1])), 'url': absolute('https://a.test/p/', '1')}]\n"
        )
        extractors.check(code)
        res = extractors.run(code, timeout=15)
        self.assertEqual(res, [{"title": "3 - ok", "url": "https://a.test/p/1"}])

    def test_structural_rejections(self):
        for bad in [
            "def f(:",
            "x = 1\n",
            "def outer():\n    def fetch():\n        return []\n",
        ]:
            with self.assertRaises(ValueError):
                extractors.check(bad)

        with self.assertRaises(RuntimeError):
            extractors.run("def fetch():\n    return {'not': 'a list'}\n", timeout=15)

    def test_forbidden_code_vectors_rejected(self):
        attacks = [
            "import os\ndef fetch(): return []\n",
            "import subprocess\ndef fetch(): return []\n",
            "import socket\ndef fetch(): return []\n",
            "from pathlib import Path\ndef fetch(): return []\n",
            "from . import helper\ndef fetch(): return []\n",
            "from json import *\ndef fetch(): return []\n",
            "from lookout_extract import _ab\ndef fetch(): return []\n",
            "import typing\ndef fetch(): return typing.sys.modules\n",
            "from typing import sys\ndef fetch(): return []\n",
            "from typing import operator\ndef fetch(): return []\n",
            "from json import codecs\ndef fetch(): return []\n",
            "from urllib.parse import warnings\ndef fetch(): return []\n",
            "from re import enum\ndef fetch(): return []\n",
            "from collections import abc\ndef fetch(): return []\n",
            "from string import Formatter\ndef fetch(): return []\n",
            "import string\ndef fetch(): return string.Formatter()\n",
            "def fetch(): return open('secret.txt').read()\n",
            "def fetch(): return eval('1+1')\n",
            "def fetch(): return exec('x=1')\n",
            "def fetch(): return __import__('os')\n",
            "def fetch(): return getattr('', 'join')\n",
            "def fetch(): return globals()\n",
            "def fetch(): return vars()\n",
            "def fetch(): return type('X', (), {})\n",
            "def fetch(): return dir()\n",
            "from lookout_extract import get\ndef fetch(): return get.__globals__\n",
            "def fetch(): return ().__class__.__mro__\n",
            "def fetch(): return __builtins__\n",
            "def fetch(): return 'x'._private\n",
            "def __evil__(): pass\ndef fetch(): return []\n",
            "def fetch():\n    g = (x for x in [1])\n    return g.gi_frame.f_builtins\n",
            "async def co(): pass\ndef fetch(): return co().cr_frame\n",
            "def fetch():\n    try:\n        1/0\n    except ZeroDivisionError as e:\n        return e.__traceback__\n",
            "def fetch(): return (lambda: 0).co_code\n",
            "def fetch(x): return x.tb_frame\n",
            "def fetch(x): return x.f_back\n",
            "class Evil:\n    pass\ndef fetch(): return []\n",
            "from lookout_extract import get\ndef fetch(): return '{0.__globals__}'.format(get)\n",
            "def fetch():\n    g = (x for x in [1])\n    return '{0.gi_frame}'.format(g)\n",
            "def fetch(): return '{0[__globals__]}'.format({})\n",
            "def fetch():\n    f = '{0}'\n    return f.format(1)\n",
            "def fetch(): return str.format('{0}', 1)\n",
            "def fetch(): return '{a}'.format_map({'a': 1})\n",
            "def fetch(): return __builtins__['__imp' + 'ort__']('os')\n",
        ]
        for code in attacks:
            with self.subTest(code=code.strip().splitlines()[0]):
                with self.assertRaises(ValueError):
                    extractors.check(code)

    def test_runtime_safe_import_blocks_escaped_imports(self):
        work = self.tmp_root / "rt"
        work.mkdir()
        (work / "lookout_extract.py").write_text(
            Path(extractors.__file__).with_name("lookout_extract.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (work / "runner.py").write_text(extractors.RUNNER, encoding="utf-8")
        (work / "extractor.py").write_text("import os\ndef fetch(): return []\n", encoding="utf-8")
        r = subprocess.run([sys.executable, "-I", str(work / "runner.py"), str(work)],
                           capture_output=True, text=True, timeout=10)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not allowed", r.stderr)

    def test_timeout_cancel_and_process_tree_kill(self):
        with self.assertRaises(RuntimeError) as cm:
            extractors.run("import time\ndef fetch():\n    time.sleep(30)\n    return []\n", timeout=1)
        self.assertIn("longer than 1s", str(cm.exception))

        # Cancellation event stops a slow extractor immediately
        ev = threading.Event()
        threading.Timer(0.25, ev.set).start()
        t0 = time.monotonic()
        with self.assertRaises(agents.CancelledError):
            extractors.run("import time\ndef fetch():\n    time.sleep(30)\n    return []\n", timeout=20, cancel_event=ev)
        self.assertLess(time.monotonic() - t0, 3.0)

        # Verify _kill_tree kills parent + spawned child process
        parent_script = (
            "import subprocess, sys, os, time\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(f'{os.getpid()},{c.pid}', flush=True)\n"
            "time.sleep(60)\n"
        )
        kwargs = {"stdout": subprocess.PIPE, "text": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen([sys.executable, "-c", parent_script], **kwargs)
        try:
            line = proc.stdout.readline().strip()
            parent_pid, child_pid = (int(x) for x in line.split(","))
            self.assertTrue(_pid_alive(parent_pid))
            self.assertTrue(_pid_alive(child_pid))
            extractors._kill_tree(proc)
            time.sleep(0.25)
            self.assertFalse(_pid_alive(parent_pid))
            self.assertFalse(_pid_alive(child_pid))
        finally:
            if proc.poll() is None:
                extractors._kill_tree(proc)
            if proc.stdout:
                proc.stdout.close()

    def test_output_size_cap(self):
        flood = "def fetch():\n    while True:\n        print('X' * 50000)\n    return []\n"
        with self.assertRaises(RuntimeError) as cm:
            extractors.run(flood, timeout=10, max_output=4096)
        self.assertIn("size limit", str(cm.exception))

        big_return = "def fetch():\n    return [{'title': 'A' * 3000, 'url': 'https://a.test'}] * 5\n"
        with self.assertRaises(RuntimeError) as cm2:
            extractors.run(big_return, timeout=10, max_output=4096)
        self.assertIn("size limit", str(cm2.exception))

    def test_agrees_edge_cases(self):
        self.assertTrue(extractors.agrees([], []))
        self.assertFalse(extractors.agrees([{"title": "", "url": ""}], [{"title": "", "url": ""}]))
        agent = [
            {"title": "ML   Intern", "url": "https://a.test/1/"},
            {"title": "Data Intern", "url": "https://a.test/2"},
            {"title": "Systems", "url": "https://a.test/3"},
        ]
        code = [
            {"title": "ml intern", "url": "https://other.test"},
            {"title": "Different", "url": "https://a.test/2/"},
        ]
        self.assertTrue(extractors.agrees(agent, code))
        five = [{"title": f"Item {i}", "url": f"https://a.test/{i}"} for i in range(5)]
        self.assertFalse(extractors.agrees(five, five[:2]))
        self.assertTrue(extractors.agrees(five, five[:3]))


class TestLookoutExtractHelpers(LookoutTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def test_http_helpers_success_errors_timeouts_and_caps(self):
        base = f"http://127.0.0.1:{self.port}"
        self.assertEqual(lookout_extract.get(f"{base}/ok"), "hello world")
        self.assertEqual(lookout_extract.get_json(f"{base}/json"), {"items": [1, 2]})
        self.assertEqual(lookout_extract.post_json(f"{base}/post", {"a": 1}), {"echo": {"a": 1}})
        self.assertEqual(lookout_extract.absolute("https://a.test/jobs/", "1"), "https://a.test/jobs/1")

        for bad_url in ("file:///etc/passwd", "ftp://example.com/a", "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                lookout_extract.get(bad_url)

        with self.assertRaises(RuntimeError) as cm_404:
            lookout_extract.get(f"{base}/missing")
        self.assertIn("404", str(cm_404.exception))

        with self.assertRaises(RuntimeError) as cm_json:
            lookout_extract.get_json(f"{base}/badjson")
        self.assertIn("invalid JSON", str(cm_json.exception))

        with self.assertRaises(RuntimeError) as cm_to:
            lookout_extract.get(f"{base}/slow", timeout=0.1)
        self.assertIn("timed out", str(cm_to.exception))

        with self.assertRaises(RuntimeError) as cm_cap1:
            lookout_extract.get(f"{base}/big", max_bytes=128)
        self.assertIn("exceeded", str(cm_cap1.exception))

        with self.assertRaises(RuntimeError) as cm_cap2:
            lookout_extract.get(f"{base}/big-noclen", max_bytes=128)
        self.assertIn("exceeded", str(cm_cap2.exception))

    def test_browser_eval_always_closes_session_even_on_errors(self):
        calls = []

        def fake_ab(session, *args, timeout=60):
            calls.append((session, args))
            if args[0] == "eval":
                return json.dumps({"success": True, "data": {"result": json.dumps([{"title": "X"}])}})
            return ""

        with mock.patch.object(lookout_extract, "_ab", side_effect=fake_ab), \
             mock.patch.object(lookout_extract.time, "sleep"):
            res = lookout_extract.browser_eval("https://example.com", "[]", wait_ms=10, scrolls=1)
        self.assertEqual(res, [{"title": "X"}])
        self.assertEqual([c[1][0] for c in calls], ["open", "scroll", "eval", "close"])

        calls.clear()

        def failing_ab(session, *args, timeout=60):
            calls.append(args[0])
            if args[0] == "eval":
                raise RuntimeError("eval exploded")
            if args[0] == "close":
                raise RuntimeError("close failed")
            return ""

        with mock.patch.object(lookout_extract, "_ab", side_effect=failing_ab), \
             mock.patch.object(lookout_extract.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                lookout_extract.browser_eval("https://example.com", "[]", wait_ms=0, scrolls=0)
        self.assertEqual(str(cm.exception), "eval exploded")
        self.assertIn("close", calls)

        with self.assertRaises(ValueError):
            lookout_extract.browser_eval("file:///bad", "[]")

        with mock.patch.object(lookout_extract.shutil, "which", return_value=None):
            with self.assertRaises(RuntimeError) as cm_missing:
                lookout_extract._ab("s1", "open", "https://example.com")
            self.assertIn("not installed", str(cm_missing.exception))


if __name__ == "__main__":
    unittest.main()
