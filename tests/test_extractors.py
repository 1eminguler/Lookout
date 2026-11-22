"""Tests for extractor AST/runtime sandbox hardening, SSRF guards, process-tree timeouts, cancel and HTTP helpers."""
import contextlib
import ctypes
import http.server
import json
import os
import socket
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

    def _record(self):
        hits = getattr(self.server, "hits", None)
        if hits is not None:
            hits.append((self.command, self.path))

    def do_GET(self):
        self._record()
        port = self.server.server_address[1]
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
        elif self.path == "/secret":
            body = b"SECRET"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redir-loopback":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/secret")
            self.end_headers()
        elif self.path == "/redir-localhost":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{port}/secret")
            self.end_headers()
        elif self.path == "/redir-private-dns":
            self.send_response(302)
            self.send_header("Location", f"http://meta.internal:{port}/secret")
            self.end_headers()
        elif self.path == "/redir-308-loopback":
            self.send_response(308)
            self.send_header("Location", f"http://127.0.0.1:{port}/secret")
            self.end_headers()
        elif self.path == "/redir-public":
            self.send_response(302)
            self.send_header("Location", f"http://public.example:{port}/ok")
            self.end_headers()
        elif self.path == "/redir-relative":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        self._record()
        port = self.server.server_address[1]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        if self.path == "/redir-308-loopback":
            self.send_response(308)
            self.send_header("Location", f"http://127.0.0.1:{port}/secret")
            self.end_headers()
            return
        got = json.loads(raw.decode("utf-8")) if raw else {}
        body = json.dumps({"echo": got}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def _mock_public_dns(local_port: int):
    """Map public.example/example.com -> 93.184.216.34 (routed to local_port), meta.internal -> 169.254.169.254."""
    orig_getaddrinfo = socket.getaddrinfo
    orig_create_connection = socket.create_connection

    def fake_getaddrinfo(host, port, *args, **kwargs):
        h = str(host or "").rstrip(".").lower()
        p = int(port) if port else 80
        if h in ("public.example", "example.com"):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", p))]
        if h == "meta.internal":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("169.254.169.254", p))]
        if h == "dual.example":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", p)),
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", p)),
            ]
        return orig_getaddrinfo(host, port, *args, **kwargs)

    def fake_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        host, port = address
        h = str(host or "").rstrip(".").lower()
        if h in ("public.example", "example.com", "93.184.216.34"):
            return orig_create_connection(("127.0.0.1", local_port), timeout=timeout, source_address=source_address)
        return orig_create_connection(address, timeout=timeout, source_address=source_address)

    with mock.patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo), \
         mock.patch.object(socket, "create_connection", side_effect=fake_create_connection):
        yield


class TestExtractors(LookoutTestCase):
    def test_sandbox_single_source_of_truth_runtime_and_parse_time(self):
        self.assertIs(extractors.HELPERS, lookout_extract.SANDBOX_HELPERS)
        self.assertIs(extractors.MODULES, lookout_extract.SANDBOX_MODULES)
        self.assertIs(extractors.BANNED_CALLS, lookout_extract.SANDBOX_BANNED_CALLS)
        self.assertIs(extractors.BANNED_ATTRS, lookout_extract.SANDBOX_BANNED_ATTRS)
        self.assertIn("_HELPERS = _le.SANDBOX_HELPERS", extractors.RUNNER)
        self.assertIn("_MODULES = _le.SANDBOX_MODULES", extractors.RUNNER)
        self.assertIn("_BANNED_CALLS = _le.SANDBOX_BANNED_CALLS", extractors.RUNNER)
        self.assertIn("_BANNED_ATTRS = _le.SANDBOX_BANNED_ATTRS", extractors.RUNNER)
        self.assertNotIn('"gi_frame"', extractors.RUNNER)
        self.assertNotIn('"browser_eval"', extractors.RUNNER)
        self.assertNotIn('"unicodedata"', extractors.RUNNER)

        # Verify the runtime runner process sees the exact same sets as parse-time check().
        work = self.tmp_root / "ssot"
        work.mkdir()
        (work / "lookout_extract.py").write_text(
            Path(extractors.__file__).with_name("lookout_extract.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        prelude = extractors.RUNNER.split("_src_path =", 1)[0]
        probe = (
            f"import sys\nsys.argv = ['runner.py', {str(work)!r}]\n"
            + prelude
            + "\nprint(json.dumps({"
            + "'helpers': sorted(_HELPERS), 'modules': sorted(_MODULES), "
            + "'banned_calls': sorted(_BANNED_CALLS), 'banned_attrs': sorted(_BANNED_ATTRS)}))\n"
        )
        r = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        rt = json.loads(r.stdout.strip())
        self.assertEqual(set(rt["helpers"]), set(extractors.HELPERS))
        self.assertEqual(set(rt["modules"]), set(extractors.MODULES))
        self.assertEqual(set(rt["banned_calls"]), set(extractors.BANNED_CALLS))
        self.assertEqual(set(rt["banned_attrs"]), set(extractors.BANNED_ATTRS))

        # Verify tasks.py's inline test_extractor.py convention works with single-source RUNNER.
        (work / "test_extractor.py").write_text(extractors.RUNNER.replace("sys.argv[1]", "'.'"), encoding="utf-8")
        (work / "extractor.py").write_text("from lookout_extract import absolute\ndef fetch():\n    return [{'title': 'T', 'url': absolute('https://a.test/', '1')}]\n", encoding="utf-8")
        r2 = subprocess.run([sys.executable, "-I", "test_extractor.py"], cwd=work, capture_output=True, text=True, timeout=10)
        self.assertEqual(r2.returncode, 0, msg=r2.stderr)
        self.assertIn("@@LOOKOUT@@", r2.stdout)

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
            "from lookout_extract import SANDBOX_MODULES\ndef fetch(): return []\n",
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

        (work / "extractor.py").write_text("from lookout_extract import SANDBOX_MODULES\ndef fetch(): return []\n", encoding="utf-8")
        r2 = subprocess.run([sys.executable, "-I", str(work / "runner.py"), str(work)],
                            capture_output=True, text=True, timeout=10)
        self.assertNotEqual(r2.returncode, 0)
        self.assertIn("unsupported import", r2.stderr.lower())

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
        self.srv.hits = []
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def test_ssrf_blocks_local_private_link_local_cgnat_multicast_ipv6_and_dns(self):
        blocked_urls = [
            f"http://127.0.0.1:{self.port}/ok",
            f"http://localhost:{self.port}/ok",
            f"http://sub.localhost:{self.port}/ok",
            f"http://LOCALHOST.:{self.port}/ok",
            f"http://service.local:{self.port}/ok",
            f"http://[::1]:{self.port}/ok",
            f"http://[::ffff:127.0.0.1]:{self.port}/ok",
            f"http://[::127.0.0.1]:{self.port}/ok",
            f"http://0.0.0.0:{self.port}/ok",
            f"http://[::]:{self.port}/ok",
            "http://10.0.0.1/secret",
            "http://172.16.0.1/secret",
            "http://192.168.1.1/secret",
            "http://169.254.169.254/latest/meta-data/",
            "http://[fe80::1]/secret",
            "http://100.64.0.1/secret",
            "http://100.127.255.254/secret",
            "http://224.0.0.1/",
            "http://[ff02::1]/",
            "http://[fc00::1]/",
            "http://[fd12:3456::1]/",
            "http://[fec0::1]/",
            "http://[::ffff:10.0.0.1]/",
            "http://[::ffff:169.254.169.254]/",
            "http://[2002:7f00:0001::]/",
        ]
        for url in blocked_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as cm_get:
                    lookout_extract.get(url)
                self.assertIn("blocked", str(cm_get.exception).lower())
                with self.assertRaises(ValueError):
                    lookout_extract.get_json(url)
                with self.assertRaises(ValueError):
                    lookout_extract.post_json(url, {"a": 1})
                with mock.patch.object(lookout_extract, "_ab") as ab_mock:
                    with self.assertRaises(ValueError):
                        lookout_extract.browser_eval(url, "[]")
                    ab_mock.assert_not_called()

        # Multi-record DNS response containing both public and loopback must be blocked.
        with _mock_public_dns(self.port):
            with self.assertRaises(ValueError):
                lookout_extract.get(f"http://dual.example:{self.port}/ok")
            with self.assertRaises(ValueError):
                lookout_extract.get(f"http://meta.internal:{self.port}/ok")
            with self.assertRaises(ValueError):
                lookout_extract.browser_eval(f"http://meta.internal:{self.port}/ok", "[]")

        # Verify local server never received a single request from blocked calls.
        self.assertEqual(self.srv.hits, [])

        # Even inside isolated extractor subprocess, localhost access is rejected.
        with self.assertRaises(RuntimeError) as cm_sub:
            extractors.run(
                f"from lookout_extract import get\ndef fetch():\n    return [{{'title': get('http://127.0.0.1:{self.port}/secret'), 'url': 'https://a.test'}}]\n",
                timeout=15,
            )
        self.assertIn("blocked", str(cm_sub.exception).lower())
        self.assertEqual(self.srv.hits, [])

    def test_redirect_ssrf_blocks_loopback_localhost_and_private_dns(self):
        with _mock_public_dns(self.port):
            for path in ("/redir-loopback", "/redir-localhost", "/redir-private-dns", "/redir-308-loopback"):
                with self.subTest(path=path):
                    with self.assertRaises(ValueError) as cm:
                        lookout_extract.get(f"http://public.example:{self.port}{path}")
                    self.assertIn("blocked", str(cm.exception).lower())
            with self.assertRaises(ValueError):
                lookout_extract.post_json(f"http://public.example:{self.port}/redir-308-loopback", {"a": 1})

            # Safe redirects between public targets succeed.
            self.assertEqual(lookout_extract.get(f"http://public.example:{self.port}/redir-public"), "hello world")
            self.assertEqual(lookout_extract.get(f"http://public.example:{self.port}/redir-relative"), "hello world")

        self.assertFalse(any(p == "/secret" for _, p in self.srv.hits))

    def test_http_helpers_public_path_success_errors_timeouts_and_caps(self):
        base = f"http://public.example:{self.port}"
        with _mock_public_dns(self.port):
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

        with _mock_public_dns(self.port), \
             mock.patch.object(lookout_extract, "_ab", side_effect=fake_ab), \
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

        with _mock_public_dns(self.port), \
             mock.patch.object(lookout_extract, "_ab", side_effect=failing_ab), \
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
