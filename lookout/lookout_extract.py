"""Helpers available to learned extractors (`from lookout_extract import ...`).

Extractors are small Python files written by the agent the first time it scans a source.
They run in a separate process with a time limit and may only use these helpers plus a few
standard-library modules (checked before they run).
"""
import json
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def get(url: str, headers: dict = None, timeout: int = 20) -> str:
    """HTTP GET, returns the response body as text."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en,tr;q=0.8", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


def get_json(url: str, headers: dict = None, timeout: int = 20):
    return json.loads(get(url, {"Accept": "application/json", **(headers or {})}, timeout))


def post_json(url: str, payload, headers: dict = None, timeout: int = 20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _ab(session: str, *args: str, timeout: int = 60) -> str:
    exe = shutil.which("agent-browser")
    if not exe:
        raise RuntimeError("agent-browser is not installed")
    r = subprocess.run([exe, "--session", session, *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    return r.stdout


def browser_eval(url: str, js: str, wait_ms: int = 2500, scrolls: int = 4):
    """Open `url` in a real browser, wait, scroll, then evaluate the JavaScript expression `js`
    (which must return something JSON-serialisable, e.g. an array of objects) and return it."""
    session = "lookout-x-" + uuid.uuid4().hex[:8]
    try:
        _ab(session, "open", url, timeout=90)
        time.sleep(wait_ms / 1000)
        for _ in range(scrolls):
            _ab(session, "scroll", "down", "3000", timeout=30)
            time.sleep(0.5)
        out = _ab(session, "eval", f"JSON.stringify(({js}))", "--json", timeout=60)
        data = json.loads(out)
        if not data.get("success"):
            raise RuntimeError(f"browser eval failed: {data.get('error')}")
        result = data["data"]["result"]
        return json.loads(result) if isinstance(result, str) else result
    finally:
        _ab(session, "close", timeout=30)


def absolute(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href or "")
