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
        uas = getattr(self.server, "user_agents", None)
        if uas is not None:
            uas.append(self.headers.get("User-Agent", ""))

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
        elif self.path == "/private":
            body = b"private"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/robots.txt":
            status = getattr(self.server, "robots_status", 404)
            body = getattr(self.server, "robots_body", b"")
            hdrs = getattr(self.server, "robots_headers", {})
            self.send_response(status)
            for k, v in hdrs.items():
                self.send_header(k, v)
            if status == 200:
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if status == 200 and body:
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
        for code_items in ([], [{"title": "A", "url": "https://a.test"}]):
            res = extractors.agrees([], code_items)
            self.assertIs(res, extractors.UNVERIFIED)
            self.assertFalse(bool(res))
            self.assertEqual(res, "unverified")
            self.assertIsNot(res, True)
            self.assertIsNot(res, False)
            self.assertNotEqual(res, True)
            self.assertNotEqual(res, False)
            self.assertEqual(str(res), "unverified")
            self.assertEqual(repr(res), "UNVERIFIED")

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

    def test_run_and_compare(self):
        stable_code = "def fetch():\n    return [{'title': 'Stable', 'url': 'https://a.test/1'}]\n"
        ok, items, reason = extractors.run_and_compare(stable_code, timeout=15)
        self.assertTrue(ok)
        self.assertEqual(items, [{"title": "Stable", "url": "https://a.test/1"}])
        self.assertEqual(reason, "")

        empty_code = "def fetch():\n    return []\n"
        ok_empty, items_empty, reason_empty = extractors.run_and_compare(empty_code, timeout=15)
        self.assertFalse(ok_empty)
        self.assertEqual(items_empty, [])
        self.assertIn("no items", reason_empty.lower())

        ok_allow, items_allow, reason_allow = extractors.run_and_compare(empty_code, timeout=15, allow_empty=True)
        self.assertTrue(ok_allow)
        self.assertEqual(items_allow, [])
        self.assertEqual(reason_allow, "")

        unstable_code = (
            "import time\n"
            "def fetch():\n"
            "    n = time.monotonic_ns()\n"
            "    return [{'title': f'Item {n}', 'url': f'https://a.test/{n}'}]\n"
        )
        ok_unstable, items_unstable, reason_unstable = extractors.run_and_compare(unstable_code, timeout=15)
        self.assertFalse(ok_unstable)
        self.assertEqual(len(items_unstable), 1)
        self.assertTrue(reason_unstable)

        # Verify partial overlap, count mismatch, asymmetric overlap, and cancellation
        p_a = [{"title": f"Role {i}", "url": f"https://a.test/{i}"} for i in range(4)]
        p_b = p_a[:3] + [{"title": "Role 99", "url": "https://a.test/99"}]
        with mock.patch.object(extractors, "run", side_effect=[p_a, p_b]):
            ok_partial, got_partial, reason_partial = extractors.run_and_compare("def fetch(): return []", min_overlap=0.7)
        self.assertTrue(ok_partial)
        self.assertEqual(got_partial, p_b)
        self.assertEqual(reason_partial, "")

        with mock.patch.object(extractors, "run", side_effect=[p_a, p_a[:1]]):
            ok_cnt, _, reason_cnt = extractors.run_and_compare("def fetch(): return []", min_overlap=0.7)
        self.assertFalse(ok_cnt)
        self.assertIn("count", reason_cnt.lower())

        with mock.patch.object(extractors, "run", side_effect=[[p_a[0]] * 4, p_a]):
            ok_asym, _, reason_asym = extractors.run_and_compare("def fetch(): return []", min_overlap=0.7)
        self.assertFalse(ok_asym)
        self.assertIn("overlap", reason_asym.lower())

        with mock.patch.object(extractors, "run", side_effect=[p_a, []]):
            ok_drop, _, reason_drop = extractors.run_and_compare("def fetch(): return []")
        self.assertFalse(ok_drop)
        self.assertIn("changed", reason_drop.lower())

        ev = threading.Event()
        threading.Timer(0.05, ev.set).start()
        with mock.patch.object(extractors, "run", return_value=p_a) as m_run:
            with self.assertRaises(agents.CancelledError):
                extractors.run_and_compare("def fetch(): return []", cancel_event=ev, delay=2.0)
        self.assertEqual(m_run.call_count, 1)


class TestLookoutExtractHelpers(LookoutTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._orig_interval = lookout_extract.MIN_HOST_INTERVAL
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in ("LOOKOUT_USER_AGENT", "LOOKOUT_MIN_INTERVAL", "LOOKOUT_RESPECT_ROBOTS")
        }
        self._env_patch = mock.patch.dict(os.environ, clean_env, clear=True)
        self._env_patch.start()
        lookout_extract.set_host_interval(0.0)
        lookout_extract.reset_throttle()
        lookout_extract.clear_robots_cache()

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.srv.hits = []
        self.srv.user_agents = []
        self.srv.robots_status = 404
        self.srv.robots_body = b""
        self.srv.robots_headers = {}
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        lookout_extract.set_host_interval(self._orig_interval)
        lookout_extract.reset_throttle()
        lookout_extract.clear_robots_cache()
        self._env_patch.stop()
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

    def test_user_agent_default_and_override(self):
        self.assertTrue(lookout_extract.DEFAULT_UA.startswith("Lookout/"))
        self.assertNotIn("Chrome", lookout_extract.DEFAULT_UA)
        self.assertNotIn("Mozilla", lookout_extract.DEFAULT_UA)
        self.assertEqual(lookout_extract.get_user_agent(), lookout_extract.DEFAULT_UA)

        base = f"http://public.example:{self.port}"
        with _mock_public_dns(self.port):
            self.assertEqual(lookout_extract.get(f"{base}/ok"), "hello world")
            self.assertEqual(lookout_extract.post_json(f"{base}/post", {"k": 1}), {"echo": {"k": 1}})
            self.assertEqual(self.srv.user_agents[-2:], [lookout_extract.DEFAULT_UA, lookout_extract.DEFAULT_UA])

            # Caller-provided User-Agent header overrides default
            lookout_extract.get(f"{base}/ok", headers={"User-Agent": "ExplicitAgent/2.0"})
            lookout_extract.post_json(f"{base}/post", {"k": 2}, headers={"user-agent": "LowerAgent/3.0"})
            self.assertEqual(self.srv.user_agents[-2:], ["ExplicitAgent/2.0", "LowerAgent/3.0"])

            # LOOKOUT_USER_AGENT env var overrides get_user_agent() in get(), post_json(), and allowed()
            with mock.patch.dict(os.environ, {"LOOKOUT_USER_AGENT": "EnvBot/1.2 (+https://example.org)"}):
                self.assertEqual(lookout_extract.get_user_agent(), "EnvBot/1.2 (+https://example.org)")
                lookout_extract.get(f"{base}/ok")
                lookout_extract.post_json(f"{base}/post", {"k": 3})
                lookout_extract.clear_robots_cache()
                self.assertTrue(lookout_extract.allowed(f"{base}/ok"))
                self.assertEqual(
                    self.srv.user_agents[-3:],
                    [
                        "EnvBot/1.2 (+https://example.org)",
                        "EnvBot/1.2 (+https://example.org)",
                        "EnvBot/1.2 (+https://example.org)",
                    ],
                )

    def test_per_host_throttle(self):
        self.assertGreater(self._orig_interval, 0.0)

        lookout_extract.set_host_interval(-1.0)
        self.assertEqual(lookout_extract.MIN_HOST_INTERVAL, 0.0)
        lookout_extract.set_host_interval(0.15)
        self.assertEqual(lookout_extract.get_host_interval(), 0.15)

        with mock.patch.dict(os.environ, {"LOOKOUT_MIN_INTERVAL": "0.08"}):
            self.assertAlmostEqual(lookout_extract.get_host_interval(), 0.08)
        with mock.patch.dict(os.environ, {"LOOKOUT_MIN_INTERVAL": "-2"}):
            self.assertEqual(lookout_extract.get_host_interval(), 0.0)
        for bad in ("not-a-float", "nan", "inf"):
            with mock.patch.dict(os.environ, {"LOOKOUT_MIN_INTERVAL": bad}):
                self.assertEqual(lookout_extract.get_host_interval(), 0.15)

        base_a = f"http://public.example:{self.port}"
        base_b = f"http://example.com:{self.port}"
        lookout_extract.reset_throttle()

        with _mock_public_dns(self.port):
            t0 = time.monotonic()
            self.assertEqual(lookout_extract.get(f"{base_a}/ok"), "hello world")
            # Different host must NOT be blocked by base_a's throttle
            t_b0 = time.monotonic()
            self.assertEqual(lookout_extract.get(f"{base_b}/ok"), "hello world")
            dt_other = time.monotonic() - t_b0
            self.assertLess(dt_other, 0.12)

            # Second request to same host must wait at least ~0.15s from first
            self.assertEqual(lookout_extract.get(f"{base_a}/ok"), "hello world")
            dt_same = time.monotonic() - t0
            self.assertGreaterEqual(dt_same, 0.13)

            # Concurrent requests to the same host queue cleanly without thundering herd
            lookout_extract.reset_throttle()
            barrier = threading.Barrier(3, timeout=3.0)
            results = []

            def worker():
                barrier.wait()
                results.append(lookout_extract.get(f"{base_a}/ok"))

            t_conc0 = time.monotonic()
            threads = [threading.Thread(target=worker) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)
            dt_conc = time.monotonic() - t_conc0
            self.assertEqual(results, ["hello world"] * 3)
            self.assertGreaterEqual(dt_conc, 0.27)

            # post_json and browser_eval also throttle per host
            lookout_extract.reset_throttle()
            t_pb0 = time.monotonic()
            self.assertEqual(lookout_extract.post_json(f"{base_a}/post", {"a": 1}), {"echo": {"a": 1}})
            with mock.patch.object(lookout_extract, "_ab", side_effect=lambda s, *a, **kw: (
                json.dumps({"success": True, "data": {"result": "[]"}}) if a[0] == "eval" else ""
            )):
                self.assertEqual(lookout_extract.browser_eval(f"{base_a}/ok", "[]", wait_ms=0, scrolls=0), [])
            self.assertGreaterEqual(time.monotonic() - t_pb0, 0.13)

    def test_robots_txt_allowed_and_enforcement(self):
        base = f"http://public.example:{self.port}"
        with _mock_public_dns(self.port):
            # 200 with Disallow: /private
            self.srv.robots_status = 200
            self.srv.robots_body = b"User-agent: *\nDisallow: /private\n"
            lookout_extract.clear_robots_cache()
            self.assertFalse(lookout_extract.allowed(f"{base}/private"))
            self.assertTrue(lookout_extract.allowed(f"{base}/ok"))

            # Cached result stays valid until cache is cleared
            self.srv.robots_status = 403
            self.assertTrue(lookout_extract.allowed(f"{base}/ok"))
            lookout_extract.clear_robots_cache()
            self.assertFalse(lookout_extract.allowed(f"{base}/ok"))

            # 401 also disallows; 404 allows
            self.srv.robots_status = 401
            lookout_extract.clear_robots_cache()
            self.assertFalse(lookout_extract.allowed(f"{base}/ok"))

            self.srv.robots_status = 404
            lookout_extract.clear_robots_cache()
            self.assertTrue(lookout_extract.allowed(f"{base}/private"))

            # 5xx fails open without caching
            self.srv.robots_status = 503
            lookout_extract.clear_robots_cache()
            self.assertTrue(lookout_extract.allowed(f"{base}/private"))
            self.srv.robots_status = 200
            self.srv.robots_body = b"User-agent: *\nDisallow: /private\n"
            self.assertFalse(lookout_extract.allowed(f"{base}/private"))

            # Redirect from /robots.txt to loopback must raise SSRF ValueError
            self.srv.robots_status = 302
            self.srv.robots_headers = {"Location": f"http://127.0.0.1:{self.port}/secret"}
            lookout_extract.clear_robots_cache()
            with self.assertRaises(ValueError) as cm_ssrf:
                lookout_extract.allowed(f"{base}/ok")
            self.assertIn("blocked", str(cm_ssrf.exception).lower())
            self.assertFalse(any(p == "/secret" for _, p in self.srv.hits))

            # respect_robots=True blocks get, get_json, post_json, browser_eval before fetching
            self.srv.robots_status = 200
            self.srv.robots_headers = {}
            self.srv.robots_body = b"User-agent: *\nDisallow: /private\n"
            lookout_extract.clear_robots_cache()
            self.srv.hits.clear()

            with self.assertRaises(RuntimeError) as cm_get:
                lookout_extract.get(f"{base}/private", respect_robots=True)
            self.assertIn("robots.txt", str(cm_get.exception))
            with self.assertRaises(RuntimeError):
                lookout_extract.get_json(f"{base}/private", respect_robots=True)
            with self.assertRaises(RuntimeError):
                lookout_extract.post_json(f"{base}/private", {"a": 1}, respect_robots=True)
            with mock.patch.object(lookout_extract, "_ab") as ab_mock:
                with self.assertRaises(RuntimeError):
                    lookout_extract.browser_eval(f"{base}/private", "[]", respect_robots=True)
                ab_mock.assert_not_called()
            self.assertFalse(any(p == "/private" for _, p in self.srv.hits))

            # Default respect_robots=None leaves existing fetches unaffected; LOOKOUT_RESPECT_ROBOTS=1 enforces
            self.assertEqual(lookout_extract.get(f"{base}/private"), "private")
            with mock.patch.dict(os.environ, {"LOOKOUT_RESPECT_ROBOTS": "1"}):
                with self.assertRaises(RuntimeError):
                    lookout_extract.get(f"{base}/private")
                self.assertEqual(lookout_extract.get(f"{base}/ok"), "hello world")
                self.assertEqual(lookout_extract.get(f"{base}/private", respect_robots=False), "private")

        # allowed() is exported in SANDBOX_HELPERS and usable in isolated extractors.run()
        self.assertIn("allowed", lookout_extract.SANDBOX_HELPERS)
        res = extractors.run(
            "from lookout_extract import allowed\n"
            "def fetch():\n"
            "    return [{'title': str(callable(allowed)), 'url': 'https://a.test/1'}]\n",
            timeout=15,
        )
        self.assertEqual(res, [{"title": "True", "url": "https://a.test/1"}])
        with self.assertRaises(RuntimeError) as cm_sub:
            extractors.run(
                "from lookout_extract import allowed\n"
                "def fetch():\n"
                "    return [{'title': str(allowed('http://127.0.0.1/ok')), 'url': 'https://a.test/1'}]\n",
                timeout=15,
            )
        self.assertIn("blocked", str(cm_sub.exception).lower())

    def test_strip_control_chars(self):
        self.assertEqual(lookout_extract.strip_control_chars(None), "")
        self.assertEqual(lookout_extract.strip_control_chars(12345), "12345")
        raw = "Hello\x00\x07\x08\tWorld\x0b\x0c\nLine2\r\n\x0e\x1fEnd\x7f\ufffe\uffff-şğüöçıİ"
        self.assertEqual(lookout_extract.strip_control_chars(raw), "Hello\tWorld\nLine2\r\nEnd-şğüöçıİ")


if __name__ == "__main__":
    unittest.main()
