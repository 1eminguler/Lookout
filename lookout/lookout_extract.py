"""Helpers available to learned extractors (`from lookout_extract import ...`).

Extractors are small Python files written by the agent the first time it scans a source.
They run in a separate process with a time limit and may only use these helpers plus a few
standard-library modules (checked before they run).
"""
import ipaddress
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
from typing import Optional, Union

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_HTTP_BYTES = 10 * 1024 * 1024
MAX_HTTP_TIMEOUT = 60.0
MAX_WAIT_MS = 30_000
MAX_SCROLLS = 30

# Single source of truth imported by lookout.extractors (both AST check and runtime runner).
SANDBOX_HELPERS = frozenset({"get", "get_json", "post_json", "browser_eval", "absolute"})
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
