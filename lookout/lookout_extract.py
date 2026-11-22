"""Helpers available to learned extractors (`from lookout_extract import ...`).

Extractors are small Python files written by the agent the first time it scans a source.
They run in a separate process with a time limit and may only use these helpers plus a few
standard-library modules (checked before they run).
"""
import json
import math
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_HTTP_BYTES = 10 * 1024 * 1024
MAX_HTTP_TIMEOUT = 60.0
MAX_WAIT_MS = 30_000
MAX_SCROLLS = 30


def _check_url(url: str) -> str:
    if not isinstance(url, str):
        raise ValueError("URL must be a string")
    clean = url.strip()
    scheme = urllib.parse.urlparse(clean).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("only http and https URLs are allowed")
    return clean


def _clamp_timeout(timeout) -> float:
    try:
        t = float(timeout)
    except (TypeError, ValueError):
        raise ValueError("timeout must be a number") from None
    if math.isnan(t) or math.isinf(t):
        raise ValueError("timeout must be finite")
    return max(0.05, min(t, MAX_HTTP_TIMEOUT))


def _clamp_max_bytes(max_bytes) -> int:
    try:
        n = int(max_bytes)
    except (TypeError, ValueError):
        raise ValueError("max_bytes must be an integer") from None
    return max(1, min(n, MAX_HTTP_BYTES))


def _read_limited(resp, url: str, limit: int) -> bytes:
    cl = resp.headers.get("Content-Length") if getattr(resp, "headers", None) else None
    if cl:
        try:
            if int(cl) > limit:
                raise RuntimeError(f"response from {url} exceeded {limit} bytes")
        except ValueError:
            pass
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError(f"response from {url} exceeded {limit} bytes")
    return data


def _open_url(req: urllib.request.Request, url: str, timeout: float, limit: int):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = _read_limited(r, url, limit)
            charset = r.headers.get_content_charset() if getattr(r, "headers", None) else None
            return body.decode(charset or "utf-8", "replace")
    except RuntimeError:
        raise
    except urllib.error.HTTPError as e:
        code, reason = e.code, e.reason
        try:
            e.close()
        except Exception:
            pass
        raise RuntimeError(f"HTTP {code} for {url}: {reason}") from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise RuntimeError(f"request to {url} timed out after {timeout}s") from None
        raise RuntimeError(f"request to {url} failed: {reason}") from None
    except (TimeoutError, socket.timeout):
        raise RuntimeError(f"request to {url} timed out after {timeout}s") from None


def get(url: str, headers: dict = None, timeout: float = 20, max_bytes: int = MAX_HTTP_BYTES) -> str:
    """HTTP GET, returns the response body as text."""
    clean = _check_url(url)
    t = _clamp_timeout(timeout)
    limit = _clamp_max_bytes(max_bytes)
    req = urllib.request.Request(clean, headers={"User-Agent": UA, "Accept-Language": "en,tr;q=0.8", **(headers or {})})
    return _open_url(req, clean, t, limit)


def get_json(url: str, headers: dict = None, timeout: float = 20, max_bytes: int = MAX_HTTP_BYTES):
    text = get(url, {"Accept": "application/json", **(headers or {})}, timeout=timeout, max_bytes=max_bytes)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON from {url}: {e}") from None


def post_json(url: str, payload, headers: dict = None, timeout: float = 20, max_bytes: int = MAX_HTTP_BYTES):
    clean = _check_url(url)
    t = _clamp_timeout(timeout)
    limit = _clamp_max_bytes(max_bytes)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(clean, data=data, method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json", **(headers or {})})
    text = _open_url(req, clean, t, limit)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON from {clean}: {e}") from None


def _ab(session: str, *args: str, timeout: int = 60) -> str:
    exe = shutil.which("agent-browser")
    if not exe:
        raise RuntimeError("agent-browser is not installed")
    try:
        r = subprocess.run([exe, "--session", session, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        action = args[0] if args else "command"
        raise RuntimeError(f"agent-browser {action} timed out after {timeout}s") from None
    except OSError as e:
        raise RuntimeError(f"agent-browser failed: {e}") from None
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        action = args[0] if args else "command"
        raise RuntimeError(f"agent-browser {action} failed: {msg[:200] or f'exit code {r.returncode}'}")
    return r.stdout


def browser_eval(url: str, js: str, wait_ms: int = 2500, scrolls: int = 4):
    """Open `url` in a real browser, wait, scroll, then evaluate the JavaScript expression `js`
    (which must return something JSON-serialisable, e.g. an array of objects) and return it."""
    clean = _check_url(url)
    wait = max(0, min(int(wait_ms), MAX_WAIT_MS))
    nscrolls = max(0, min(int(scrolls), MAX_SCROLLS))
    session = "lookout-x-" + uuid.uuid4().hex[:8]
    try:
        _ab(session, "open", clean, timeout=90)
        if wait:
            time.sleep(wait / 1000)
        for _ in range(nscrolls):
            _ab(session, "scroll", "down", "3000", timeout=30)
            time.sleep(0.5)
        out = _ab(session, "eval", f"JSON.stringify(({js}))", "--json", timeout=60)
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"browser eval returned invalid JSON: {e}") from None
        if not isinstance(data, dict) or not data.get("success"):
            err = data.get("error") if isinstance(data, dict) else "unexpected response"
            raise RuntimeError(f"browser eval failed: {err}")
        inner = data.get("data")
        if not isinstance(inner, dict) or "result" not in inner:
            raise RuntimeError("browser eval failed: missing result")
        result = inner["result"]
        if isinstance(result, str):
            try:
                return json.loads(result)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"browser eval result was not valid JSON: {e}") from None
        return result
    finally:
        try:
            _ab(session, "close", timeout=15)
        except Exception:
            pass


def absolute(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href or "")
