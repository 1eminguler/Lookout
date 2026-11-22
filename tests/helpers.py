"""Shared fixtures, isolated test base class, fake agent, DB seeding and HTTP test server."""
import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union
from unittest import mock

from lookout import agents, db, lookout_extract, notify, scanner, server, tasks


def free_port() -> int:
    """Return an unused local TCP port other than Lookout's default 8780."""
    for _ in range(20):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != 8780:
            return port
    raise RuntimeError("could not allocate a free port")


class TempLookoutTestCase(unittest.TestCase):
    """Base TestCase isolating DB, plugin directory, task scratch directory and module caches."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="lookout_t_"))
        self.tmp_root = self.tmp
        self.data_dir = self.tmp / "data"
        self.plugin_dir = self.tmp / "plugins"
        self.work_dir = self.tmp / "work"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.port = free_port()

        self._patches = [
            mock.patch.dict(
                os.environ,
                {
                    "LOOKOUT_DATA_DIR": str(self.data_dir),
                    "LOOKOUT_PLUGIN_DIR": str(self.plugin_dir),
                    "LOOKOUT_PORT": str(self.port),
                },
            ),
            mock.patch.object(db, "DB_PATH", self.data_dir / "lookout.db"),
            mock.patch.object(scanner, "PLUGIN_DIR", self.plugin_dir),
            mock.patch.object(tasks, "WORK", self.work_dir),
        ]
        for p in self._patches:
            p.start()

        self._reset_globals()
        db.init()
        self.assertNotEqual(db.DB_PATH.parent.resolve(), (db.ROOT / "data").resolve())

    def tearDown(self) -> None:
        agents.cancel()
        self._reset_globals()
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    @staticmethod
    def _reset_globals() -> None:
        scanner._cancel_event.clear()
        scanner.progress.clear()
        scanner.progress.update(running=False, scan_id=None, done=0, total=0, current=[], learning=[], cancel=False)
        server._disc_cancel.clear()
        server.discovery.clear()
        server.discovery.update(id=None, watch_id=None, running=False, request="", title="",
                                results=[], error="", finished=None)
        server._test_cancel.clear()
        server.test_job.clear()
        server.test_job.update(id=None, running=False, source_id=None, title="",
                               result=None, error="", finished=None)
        agents.set_cli_custom_command(None)
        agents._model_cache.clear()
        with agents._proc_lock:
            agents._running.clear()
        with scanner._active_lock:
            scanner._active_scan = None
        with scanner._url_locks_guard:
            scanner._url_locks.clear()
            scanner._url_generations.clear()
        notify._schedule_cache.update(t=0.0, plat=None, val=None)
        lookout_extract.reset_throttle()
        lookout_extract.clear_robots_cache()
        server.last_hit["t"] = 0.0


LookoutTestCase = TempLookoutTestCase


def seed_db(item_count: int = 6) -> Dict[str, Any]:
    """Populate the current temp DB with sources, filters, scans, items, discoveries and usage."""
    db.init()
    db.set_settings({"agent": "agy", "model": "gemini-pro-medium", "notify": "1", "mail_to": "user@example.com"})

    src_agent = db.add_source("Acme Careers", "https://acme.example.com/jobs", "Internships tab")
    db.update_source(
        src_agent,
        extractor="from lookout_extract import get_json\ndef fetch():\n    return []\n",
        extractor_ok=1,
        extractor_updated=db.now(),
        extractor_runs=2,
    )
    src_plugin = db.add_source("Local Feed Plugin", "plugin:sample.py", "Local plugin", method="plugin", plugin="sample")
    src_off = db.add_source("Paused Source", "https://paused.example.com/jobs", "")
    db.update_source(src_off, active=0)

    f_inc = db.add_filter("include", "python, machine learning")
    f_exc = db.add_filter("exclude", "senior, unpaid")
    f_off = db.add_filter("location", "Antarctica")
    db.update_filter(f_off, False)

    today = date.today()
    scan_id = db.start_scan("manual", "agy", "gemini-pro-medium", db.filters(active_only=True), label="Acme Careers")
    db.record_source_result(scan_id, {"id": src_agent, "name": "Acme Careers"}, True, item_count, item_count, "", 420)

    item_ids: List[str] = []
    for idx in range(max(1, item_count)):
        dl = (today + timedelta(days=3 + idx * 4)).isoformat()
        iid = f"item-{idx + 1}"
        item = {
            "id": iid,
            "title": f"Python ML Internship #{idx + 1}",
            "org": f"Lab {idx + 1}",
            "category": "internship" if idx % 2 == 0 else "hackathon",
            "url": f"https://acme.example.com/jobs/{idx + 1}",
            "deadline": dl,
            "start_date": None,
            "location": "Istanbul" if idx % 2 == 0 else "Remote",
            "summary": f"Exciting ML engineering opportunity #{idx + 1}.",
            "extra": None,
        }
        db.save_item(scan_id, {"id": src_agent, "name": "Acme Careers"}, item)
        item_ids.append(iid)

    db.set_matches({iid: True for iid in item_ids}, "seedkey")
    if len(item_ids) >= 1:
        db.set_item(item_ids[0], action="saved", note="Prepare CV", remind_on=(today + timedelta(days=1)).isoformat())
    if len(item_ids) >= 2:
        db.set_item(item_ids[1], action="applied", note="Submitted")
    if len(item_ids) >= 3:
        db.set_item(item_ids[2], action="hidden")
    if len(item_ids) >= 4:
        db.set_matches({item_ids[3]: False}, "seedkey")

    db.finish_scan(scan_id, "done")

    disc_id = db.start_discovery("ML internships in Berlin", "ML internships Berlin", "agy", "gemini-pro-medium")
    db.finish_discovery(
        disc_id,
        "done",
        [
            {
                "name": "Berlin Tech Jobs",
                "url": "https://berlin.example.com/internships",
                "why": "Lists student internships.",
                "notes": "Filter by ML",
                "sample": "ML Intern at Foo",
            }
        ],
        title="ML internships Berlin",
    )
    db.log_agent_call("agy", "gemini-pro-medium", ok=True)

    return {
        "sources": [src_agent, src_plugin, src_off],
        "filters": [f_inc, f_exc, f_off],
        "scan_id": scan_id,
        "items": item_ids,
        "discovery_id": disc_id,
    }


class LiveServer:
    """Context manager running Lookout's ThreadingHTTPServer on an ephemeral local port."""

    def __init__(self, port: Optional[int] = None, api_token: Optional[str] = None):
        self.requested_port = port or 0
        self.port = 0
        self.api_token = api_token or server.API_TOKEN
        self.srv: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self._patches: List[Any] = []

    def __enter__(self) -> "LiveServer":
        db.init()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.requested_port), server.Handler)
        self.srv.api_token = self.api_token
        self.srv.auth_lock = threading.Lock()
        self.srv.launch_secrets = set()
        self.srv.session_tokens = set()
        self.srv.issue_launch_secret = lambda: server._issue_launch_secret(self.srv)
        self.srv.launch_secret = server._issue_launch_secret(self.srv)
        self.srv.daemon_threads = True
        try:
            self.port = self.srv.server_address[1]
            if self.port == 8780:
                raise RuntimeError("refusing to bind port 8780 in tests")
            self._patches = [mock.patch.object(server, "PORT", self.port)]
            for p in self._patches:
                p.start()
            self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
            self.thread.start()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc) -> None:
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        if self.thread:
            self.thread.join(timeout=3)
        for p in reversed(self._patches):
            p.stop()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Union[Dict[str, Any], List[Any], bytes, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        include_token: bool = True,
    ) -> Tuple[int, Dict[str, str], bytes, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if include_token and path.startswith("/api/") and "X-Lookout-Token" not in hdrs:
            hdrs["X-Lookout-Token"] = self.api_token
        raw_body: Optional[bytes] = None
        if body is not None:
            if isinstance(body, (dict, list)):
                raw_body = json.dumps(body).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
            elif isinstance(body, str):
                raw_body = body.encode("utf-8")
            else:
                raw_body = body
        try:
            conn.request(method, path, body=raw_body, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            parsed = None
            ctype = resp_headers.get("content-type", "")
            if "json" in ctype:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except ValueError:
                    parsed = None
            return resp.status, resp_headers, raw, parsed
        finally:
            conn.close()


def _detect_task_kind(task_md: str) -> str:
    low = task_md.lower()
    if "learn how to read one website" in low:
        return "learn"
    if "decide which items match" in low:
        return "classify"
    if "find good sources" in low:
        return "discover"
    return "unknown"


def make_fake_agent(
    responses: Union[Dict[str, Any], Iterable[Dict[str, Any]], Callable[..., Dict[str, Any]]]
) -> Tuple[Callable[..., str], List[Dict[str, Any]]]:
    """Build a fake agents.run_task callback that writes configured files into workdir."""
    calls: List[Dict[str, Any]] = []
    seq = iter(responses) if isinstance(responses, (list, tuple)) else None

    def fake_run_task(agent_id: str, model: str, workdir: Path, timeout: int, *args, **kwargs) -> str:
        wd = Path(workdir)
        task_md = (wd / "TASK.md").read_text(encoding="utf-8") if (wd / "TASK.md").exists() else ""
        kind = _detect_task_kind(task_md)
        calls.append({
            "agent_id": agent_id,
            "model": model,
            "workdir": str(wd),
            "timeout": timeout,
            "kind": kind,
            "task_md": task_md,
            "args": args,
            "kwargs": kwargs,
        })
        if callable(responses):
            spec = responses(agent_id, model, wd, timeout, task_md)
        elif seq is not None:
            spec = next(seq)
        else:
            spec = responses.get(kind) if kind in responses else responses.get("*", responses)

        if isinstance(spec, Exception):
            raise spec
        if not isinstance(spec, dict):
            return ""
        if "__raise__" in spec and spec["__raise__"] is not None:
            raise spec["__raise__"]

        for fname, content in spec.items():
            if fname.startswith("__"):
                continue
            target = wd / fname
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, (dict, list)):
                target.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
            elif isinstance(content, bytes):
                target.write_bytes(content)
            elif content is not None:
                target.write_text(str(content), encoding="utf-8")
        return str(spec.get("__log__", "fake agent ok"))

    return fake_run_task, calls
