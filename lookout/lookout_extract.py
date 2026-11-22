"""Helpers available to learned extractors (`from lookout_extract import ...`).

Extractors are small Python files written by the agent the first time it scans a source.
They run in a separate process with a time limit and may only use these helpers plus a few
standard-library modules (checked before they run).
"""
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import uuid
from typing import Any, Dict, Optional, Tuple, Union

DEFAULT_UA = "Lookout/0.3.0 (+https://github.com/1eminguler/lookout)"
UA = os.environ.get("LOOKOUT_USER_AGENT", "").strip() or DEFAULT_UA
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_HTTP_BYTES = 10 * 1024 * 1024
MAX_HTTP_TIMEOUT = 60.0
MAX_WAIT_MS = 30_000
MAX_SCROLLS = 30
MIN_HOST_INTERVAL: float = 0.5
RESPECT_ROBOTS: bool = False
ROBOTS_CACHE_TTL: float = 900.0
_MAX_ROBOTS_BYTES = 512 * 1024
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")

# Single source of truth imported by lookout.extractors (both AST check and runtime runner).
SANDBOX_HELPERS = frozenset({"get", "get_json", "post_json", "browser_eval", "absolute", "allowed"})
SANDBOX_MODULES = frozenset({
    "json", "re", "html", "math", "time", "datetime", "itertools", "collections",
    "typing", "urllib.parse", "unicodedata", "string", "functools",
})
SANDBOX_BANNED_CALLS = frozenset({
    "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "hasattr", "input", "breakpoint", "memoryview", "help", "type", "object", "super", "dir",
    "issubclass", "classmethod", "staticmethod", "property", "exit", "quit", "license", "credits",
    "copyright", "Formatter",
})
SANDBOX_BANNED_ATTRS = frozenset({
    "gi_frame", "gi_code", "gi_yieldfrom", "gi_running", "gi_suspended",
    "cr_frame", "cr_code", "cr_await", "cr_origin", "cr_running", "cr_suspended",
    "ag_frame", "ag_code", "ag_await", "ag_running", "ag_suspended",
    "tb_frame", "tb_next", "tb_lasti", "tb_lineno",
    "f_builtins", "f_globals", "f_locals", "f_back", "f_code", "f_trace", "f_generator",
    "co_code", "co_consts", "co_names", "co_varnames", "co_freevars", "co_cellvars",
    "func_globals", "func_code", "func_closure", "im_func", "im_self", "im_class",
    "sys", "os", "builtins", "subprocess", "importlib", "codecs", "types",
    "operator", "warnings", "copyreg", "enum", "abc", "ipaddress", "io",
    "shutil", "pathlib", "inspect", "gc", "ctypes", "socket", "signal",
    "threading", "multiprocessing", "format_map", "vformat", "get_field", "get_value",
    "Formatter",
})

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_IPV6_ULA_NET = ipaddress.ip_network("fc00::/7")
_IPV6_SITE_LOCAL_NET = ipaddress.ip_network("fec0::/10")
_IPV6_V4COMPAT_NET = ipaddress.ip_network("::/96")
_BLOCKED_HOSTS = frozenset({"localhost", "ip6-localhost", "ip6-loopback", "local"})


def _non_public_reason(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> Optional[str]:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            reason = _non_public_reason(ip.ipv4_mapped)
            if reason:
                return f"IPv4-mapped {reason}"
        if ip.sixtofour is not None:
            reason = _non_public_reason(ip.sixtofour)
            if reason:
                return f"6to4 {reason}"
        if ip.teredo is not None:
            for teredo_ip in ip.teredo:
                reason = _non_public_reason(teredo_ip)
                if reason:
                    return f"Teredo {reason}"
        if ip in _IPV6_ULA_NET:
            return "IPv6 unique-local address"
        if ip in _IPV6_SITE_LOCAL_NET:
            return "IPv6 site-local address"
        if not ip.is_unspecified and not ip.is_loopback and ip in _IPV6_V4COMPAT_NET:
            compat_v4 = ipaddress.IPv4Address(int(ip))
            reason = _non_public_reason(compat_v4)
            return f"IPv4-compatible {reason}" if reason else "deprecated IPv4-compatible address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return "CGNAT address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if not ip.is_global:
        return "non-global address"
    return None


def check_url(url: str, *, allow_private: bool = False) -> str:
    """Validate an http(s) URL and block non-public hosts/IPs unless allow_private=True."""
    if not isinstance(url, str):
        raise ValueError("URL must be a string")
    clean = url.strip()
    parsed = urllib.parse.urlparse(clean)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("only http and https URLs are allowed")
    raw_host = parsed.hostname
    if not raw_host:
        raise ValueError("URL must include a hostname")
    # Accessing parsed.port validates numeric range (1..65535) if a port is present.
    port = parsed.port or (443 if scheme == "https" else 80)
    if allow_private:
        return clean

    if "%" in raw_host:
        raise ValueError(f"blocked non-public host '{raw_host}' in {clean}: zone-scoped address")
    host = raw_host.rstrip(".").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError(f"blocked non-public host '{host or raw_host}' in {clean}: localhost/local hostname is not allowed")

    try:
        ip_lit = ipaddress.ip_address(host)
    except ValueError:
        ip_lit = None

    if ip_lit is not None:
        reason = _non_public_reason(ip_lit)
        if reason:
            raise ValueError(f"blocked non-public address {ip_lit} for {clean}: {reason}")
        return clean

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as e:
        raise RuntimeError(f"request to {clean} failed: could not resolve host '{host}': {e}") from None
    if not infos:
        raise RuntimeError(f"request to {clean} failed: host '{host}' resolved to no addresses")

    for info in infos:
        addr_raw = str(info[4][0])
        if "%" in addr_raw:
            raise ValueError(f"blocked non-public address '{addr_raw}' for {clean} (host '{host}'): zone-scoped address")
        try:
            ip = ipaddress.ip_address(addr_raw)
        except ValueError:
            raise ValueError(f"blocked invalid resolved address '{addr_raw}' for {clean}") from None
        reason = _non_public_reason(ip)
        if reason:
            raise ValueError(f"blocked non-public address {ip} for {clean} (host '{host}'): {reason}")

    return clean


def _check_url(url: str) -> str:
    return check_url(url, allow_private=False)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-checks SSRF rules before following any HTTP redirect (including 308)."""

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_307

    def __init__(self, *, allow_private: bool = False):
        super().__init__()
        self._allow_private = allow_private

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        try:
            check_url(target, allow_private=self._allow_private)
        except Exception:
            if fp is not None:
                try:
                    fp.close()
                except Exception:
                    pass
            raise
        return super().redirect_request(req, fp, code, msg, headers, target)


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
    opener = urllib.request.build_opener(_SafeRedirectHandler(allow_private=False))
    try:
        with opener.open(req, timeout=timeout) as r:
            body = _read_limited(r, url, limit)
            charset = r.headers.get_content_charset() if getattr(r, "headers", None) else None
            return body.decode(charset or "utf-8", "replace")
    except (ValueError, RuntimeError):
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


_throttle_lock = threading.Lock()
_host_next_at: Dict[str, float] = {}
_robots_lock = threading.Lock()
_robots_cache: Dict[str, Tuple[float, Any]] = {}


def get_user_agent() -> str:
    """Return the configured User-Agent string."""
    return os.environ.get("LOOKOUT_USER_AGENT", "").strip() or UA


def get_host_interval() -> float:
    """Return the minimum interval in seconds between requests to the same host."""
    raw = os.environ.get("LOOKOUT_MIN_INTERVAL", "").strip()
    if raw:
        try:
            val = float(raw)
            if math.isfinite(val):
                return max(0.0, val)
        except ValueError:
            pass
    with _throttle_lock:
        return max(0.0, float(MIN_HOST_INTERVAL))


def set_host_interval(seconds: float) -> None:
    """Update the default minimum per-host request interval in seconds (clamped >= 0.0)."""
    global MIN_HOST_INTERVAL
    try:
        val = float(seconds)
    except (TypeError, ValueError):
        raise ValueError("interval must be a number") from None
    if not math.isfinite(val):
        raise ValueError("interval must be finite")
    with _throttle_lock:
        MIN_HOST_INTERVAL = max(0.0, val)


def reset_throttle() -> None:
    """Clear all recorded per-host throttle timestamps."""
    with _throttle_lock:
        _host_next_at.clear()


def _throttle_host(url: str, min_interval: Optional[float] = None) -> float:
    """Wait if needed to enforce the per-host request interval; returns wait time in seconds."""
    host = (urllib.parse.urlparse(url).hostname or "").rstrip(".").lower()
    if not host:
        return 0.0
    if min_interval is not None:
        try:
            val = float(min_interval)
        except (TypeError, ValueError):
            raise ValueError("interval must be a number") from None
        if not math.isfinite(val):
            raise ValueError("interval must be finite")
        interval = max(0.0, val)
    else:
        interval = get_host_interval()
    if interval <= 0.0:
        return 0.0
    with _throttle_lock:
        now = time.monotonic()
        prev_next = _host_next_at.get(host, 0.0)
        slot = max(now, prev_next)
        _host_next_at[host] = slot + interval
    wait = slot - now
    if wait > 0.0:
        time.sleep(wait)
    return max(0.0, wait)


def clear_robots_cache() -> None:
    """Clear all cached robots.txt entries."""
    with _robots_lock:
        _robots_cache.clear()


def _respect_robots_enabled(respect_robots: Optional[bool] = None) -> bool:
    if respect_robots is not None:
        return bool(respect_robots)
    raw = os.environ.get("LOOKOUT_RESPECT_ROBOTS", "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    return bool(RESPECT_ROBOTS)


def _fetch_robots_text(robots_url: str, ua: str, timeout: float) -> Tuple[Optional[int], str]:
    clean_robots = _check_url(robots_url)
    opener = urllib.request.build_opener(_SafeRedirectHandler(allow_private=False))
    req = urllib.request.Request(clean_robots, headers={"User-Agent": ua})
    try:
        with opener.open(req, timeout=timeout) as r:
            body = _read_limited(r, clean_robots, _MAX_ROBOTS_BYTES)
            charset = r.headers.get_content_charset() if getattr(r, "headers", None) else None
            return int(getattr(r, "status", 200) or 200), body.decode(charset or "utf-8", "replace")
    except ValueError:
        raise
    except urllib.error.HTTPError as e:
        code = int(e.code)
        try:
            e.close()
        except Exception:
            pass
        return code, ""
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, RuntimeError):
        return None, ""


def allowed(url: str, *, user_agent: Optional[str] = None, timeout: float = 10.0) -> bool:
    """Check whether `url` is allowed by the target origin's robots.txt."""
    clean = _check_url(url)
    t = _clamp_timeout(timeout)
    ua = (user_agent if user_agent is not None else "").strip() or get_user_agent()
    parsed = urllib.parse.urlparse(clean)
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"

    now = time.monotonic()
    with _robots_lock:
        entry = _robots_cache.get(origin)
        if entry is not None and (now - entry[0]) < ROBOTS_CACHE_TTL:
            cached = entry[1]
            if isinstance(cached, urllib.robotparser.RobotFileParser):
                return cached.can_fetch(ua, clean)
            return bool(cached)

    robots_url = f"{origin}/robots.txt"
    status, text = _fetch_robots_text(robots_url, ua, t)
    fetched_at = time.monotonic()

    if status is None or status >= 500 or status < 200 or 300 <= status < 400:
        return True
    if status in (401, 403):
        with _robots_lock:
            _robots_cache[origin] = (fetched_at, False)
        return False
    if 400 <= status < 500:
        with _robots_lock:
            _robots_cache[origin] = (fetched_at, True)
        return True

    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    rp.parse(text.splitlines())
    with _robots_lock:
        _robots_cache[origin] = (fetched_at, rp)
    return rp.can_fetch(ua, clean)


def _merge_headers(defaults: Dict[str, str], headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    merged = dict(defaults)
    if not any(k.lower() == "user-agent" for k in (headers or {})):
        merged["User-Agent"] = get_user_agent()
    if headers:
        merged.update(headers)
    return merged


def _enforce_policy_and_throttle(clean: str, respect_robots: Optional[bool], throttle: Optional[float]) -> None:
    if _respect_robots_enabled(respect_robots) and not allowed(clean):
        raise RuntimeError(f"blocked by robots.txt: {clean}")
    _throttle_host(clean, min_interval=throttle)


def get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20,
    max_bytes: int = MAX_HTTP_BYTES,
    respect_robots: Optional[bool] = None,
    throttle: Optional[float] = None,
) -> str:
    """HTTP GET, returns the response body as text."""
    clean = _check_url(url)
    t = _clamp_timeout(timeout)
    limit = _clamp_max_bytes(max_bytes)
    _enforce_policy_and_throttle(clean, respect_robots, throttle)
    req = urllib.request.Request(clean, headers=_merge_headers({"Accept-Language": "en,tr;q=0.8"}, headers))
    return _open_url(req, clean, t, limit)


def get_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20,
    max_bytes: int = MAX_HTTP_BYTES,
    respect_robots: Optional[bool] = None,
    throttle: Optional[float] = None,
):
    text = get(
        url,
        {"Accept": "application/json", **(headers or {})},
        timeout=timeout,
        max_bytes=max_bytes,
        respect_robots=respect_robots,
        throttle=throttle,
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON from {url}: {e}") from None


def post_json(
    url: str,
    payload,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20,
    max_bytes: int = MAX_HTTP_BYTES,
    respect_robots: Optional[bool] = None,
    throttle: Optional[float] = None,
):
    clean = _check_url(url)
    t = _clamp_timeout(timeout)
    limit = _clamp_max_bytes(max_bytes)
    data = json.dumps(payload).encode("utf-8")
    _enforce_policy_and_throttle(clean, respect_robots, throttle)
    req = urllib.request.Request(
        clean,
        data=data,
        method="POST",
        headers=_merge_headers({"Content-Type": "application/json", "Accept": "application/json"}, headers),
    )
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


def browser_eval(
    url: str,
    js: str,
    wait_ms: int = 2500,
    scrolls: int = 4,
    respect_robots: Optional[bool] = None,
    throttle: Optional[float] = None,
):
    """Open `url` in a real browser, wait, scroll, then evaluate the JavaScript expression `js`
    (which must return something JSON-serialisable, e.g. an array of objects) and return it."""
    clean = _check_url(url)
    wait = max(0, min(int(wait_ms), MAX_WAIT_MS))
    nscrolls = max(0, min(int(scrolls), MAX_SCROLLS))
    _enforce_policy_and_throttle(clean, respect_robots, throttle)
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


def strip_control_chars(text: Any) -> str:
    """Strip XML-invalid and ASCII control characters while preserving tabs and newlines."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    return _CONTROL_CHAR_RE.sub("", s)
