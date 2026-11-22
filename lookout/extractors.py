"""Checks and runs extractors (site-reading code the agent wrote) in a separate, time-limited process."""
import ast
import json
import os
import re
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from lookout import agents, lookout_extract

HELPERS = lookout_extract.SANDBOX_HELPERS
MODULES = lookout_extract.SANDBOX_MODULES
BANNED_CALLS = lookout_extract.SANDBOX_BANNED_CALLS
BANNED_ATTRS = lookout_extract.SANDBOX_BANNED_ATTRS
MAX_OUTPUT_BYTES = 5 * 1024 * 1024

RUNNER = """import importlib, json, os, sys, types
sys.stdout.reconfigure(encoding="utf-8")
work = sys.argv[1]
marker = sys.argv[2] if len(sys.argv) > 2 else "@@LOOKOUT@@"
max_bytes = int(sys.argv[3]) if len(sys.argv) > 3 else 5242880
sys.path.insert(0, work)
import lookout_extract as _le

_HELPERS = _le.SANDBOX_HELPERS
_MODULES = _le.SANDBOX_MODULES
_BANNED_CALLS = _le.SANDBOX_BANNED_CALLS
_BANNED_ATTRS = _le.SANDBOX_BANNED_ATTRS

def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0:
        raise ImportError("relative imports are not allowed")
    if name == "lookout_extract":
        if not fromlist or any(a not in _HELPERS for a in fromlist):
            raise ImportError("unsupported import from lookout_extract")
        return types.SimpleNamespace(**{k: getattr(_le, k) for k in _HELPERS})
    if name in _MODULES:
        if fromlist and any(a == "*" or a.startswith("_") or a in _BANNED_ATTRS or a in _BANNED_CALLS for a in fromlist):
            raise ImportError(f"import from '{name}' is not allowed")
        mod = importlib.import_module(name)
        if name == "urllib.parse" and not fromlist:
            return types.SimpleNamespace(parse=mod)
        return mod
    raise ImportError(f"import of '{name}' is not allowed")

_SAFE_BUILTINS = {
    "None": None, "True": True, "False": False,
    "abs": abs, "all": all, "any": any, "ascii": ascii, "bin": bin, "bool": bool,
    "bytearray": bytearray, "bytes": bytes, "callable": callable, "chr": chr, "complex": complex,
    "dict": dict, "divmod": divmod, "enumerate": enumerate, "filter": filter, "float": float,
    "format": format, "frozenset": frozenset, "hash": hash, "hex": hex, "int": int,
    "isinstance": isinstance, "iter": iter, "len": len, "list": list, "map": map,
    "max": max, "min": min, "next": next, "oct": oct, "ord": ord, "pow": pow,
    "print": print, "range": range, "repr": repr, "reversed": reversed, "round": round,
    "set": set, "slice": slice, "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "RuntimeError": RuntimeError, "StopIteration": StopIteration,
    "LookupError": LookupError, "ArithmeticError": ArithmeticError, "AttributeError": AttributeError,
    "NameError": NameError, "ZeroDivisionError": ZeroDivisionError, "ImportError": ImportError,
    "__import__": _safe_import,
}

_src_path = os.path.join(work, "extractor.py")
with open(_src_path, encoding="utf-8") as _f:
    _code = compile(_f.read(), _src_path, "exec")
_ns = {"__name__": "extractor", "__builtins__": _SAFE_BUILTINS}
exec(_code, _ns, _ns)
items = _ns["fetch"]()
if not isinstance(items, list):
    raise TypeError("fetch() must return a list")
if not items and _ns.get("EMPTY_OK") is True:
    items = [{"empty": True}]
payload = json.dumps(items, ensure_ascii=False, default=str)
if len(payload.encode("utf-8")) > max_bytes:
    raise ValueError("extractor output exceeded size limit")
print(marker + payload)
"""


def describe_rules() -> str:
    """Generate the prompt restriction list directly from the sandbox constants in extractors.py."""
    mods = ", ".join(sorted(MODULES))
    helpers = ", ".join(sorted(HELPERS))
    banned_calls = ", ".join(sorted(BANNED_CALLS))
    banned_attrs = ", ".join(sorted(BANNED_ATTRS))
    return (
        f"- Required entry point: define a top-level `def fetch():` function (`fetch()` is required and must return a `list` of `dict` items).\n"
        f"- Allowed standard-library imports only: {mods}. No other modules, no relative imports, and no star (`*`) imports.\n"
        f"- Allowed helper imports from `lookout_extract` only: `from lookout_extract import {helpers}`.\n"
        f"- No classes: `class` definitions are not allowed.\n"
        f"- No dynamic `.format()` calls: calling `.format()` on a variable or expression is not allowed — use f-strings instead (and never access `_` or banned attributes inside format strings).\n"
        f"- No names or attributes starting with `_`: attribute access starting with `_` and names or imports starting with `__` are not allowed.\n"
        f"- Banned calls: {banned_calls}.\n"
        f"- Banned attributes: {banned_attrs}."
    )


CHECK_READER = '''"""Validates extractor.py with extractors.check rules and runs a dry run of fetch() in the sandbox."""
import ast
import json
import os
import re
import string
import subprocess
import sys
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
WORK = Path(__file__).resolve().parent
sys.path.insert(0, str(WORK))
import lookout_extract as _le

HELPERS = _le.SANDBOX_HELPERS
MODULES = _le.SANDBOX_MODULES
BANNED_CALLS = _le.SANDBOX_BANNED_CALLS
BANNED_ATTRS = _le.SANDBOX_BANNED_ATTRS
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
RUNNER = ''' + repr(RUNNER) + '''


def _check_name(name: str) -> None:
    if name in BANNED_CALLS or name.startswith("__"):
        raise ValueError(f"'{name}' is not allowed")


def _check_format_str(fmt: str) -> None:
    try:
        parsed = list(string.Formatter().parse(fmt))
    except ValueError as e:
        raise ValueError(f"invalid format string: {e}") from None
    for _, field_name, format_spec, _ in parsed:
        if field_name is not None:
            if "__" in field_name or "[_" in field_name:
                raise ValueError("unsafe access in format string is not allowed")
            for attr in re.findall(r"\\.([A-Za-z_]\\w*)", field_name):
                if attr.startswith("_") or attr in BANNED_ATTRS:
                    raise ValueError(f"access to '{attr}' in format string is not allowed")
            for bracket in re.findall(r"\\[([^\\]]+)\\]", field_name):
                key = bracket.strip()
                if key.startswith("_") or key in BANNED_ATTRS:
                    raise ValueError(f"access to '{key}' in format string is not allowed")
        if format_spec:
            _check_format_str(format_spec)


def check(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"syntax error: {e}") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            raise ValueError("class definitions are not allowed")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in MODULES:
                    raise ValueError(f"import of '{a.name}' is not allowed")
                if a.asname:
                    _check_name(a.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                raise ValueError("relative imports are not allowed")
            mod = node.module or ""
            for a in node.names:
                if a.name == "*":
                    raise ValueError("star imports are not allowed")
                if mod == "lookout_extract":
                    if a.name not in HELPERS:
                        raise ValueError(f"lookout_extract.{a.name} is not available")
                elif mod not in MODULES:
                    raise ValueError(f"import from '{mod}' is not allowed")
                elif a.name.startswith("_") or a.name in BANNED_CALLS or a.name in BANNED_ATTRS:
                    raise ValueError(f"import of '{mod}.{a.name}' is not allowed")
                if a.asname:
                    _check_name(a.asname)
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in BANNED_ATTRS:
                raise ValueError(f"access to '{node.attr}' is not allowed")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                if not (isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str)):
                    raise ValueError("dynamic '.format()' calls are not allowed")
                _check_format_str(node.func.value.value)
        elif isinstance(node, ast.Name):
            _check_name(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _check_name(node.name)
        elif isinstance(node, ast.arg):
            _check_name(node.arg)
    if not any(isinstance(n, ast.FunctionDef) and n.name == "fetch" for n in tree.body):
        raise ValueError("no fetch() function")


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (WORK / "extractor.py")
    if not target.is_file():
        print(f"ERROR: {target.name} not found")
        return 1
    try:
        code = target.read_text(encoding="utf-8")
        check(code)
    except Exception as e:
        print(f"ERROR: {e}")
        return 1

    run_dir = target.parent
    marker = f"@@LOOKOUT_{uuid.uuid4().hex}@@"
    try:
        r = subprocess.run(
            [sys.executable, "-I", "-c", RUNNER, str(run_dir), marker, str(MAX_OUTPUT_BYTES)],
            cwd=str(run_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        print("ERROR: extractor took longer than 180s")
        return 1
    except Exception as e:
        print(f"ERROR: {e}")
        return 1

    stdout = r.stdout or ""
    stderr = r.stderr or ""
    idx = stdout.rfind(marker)
    if r.returncode != 0 or idx < 0:
        err = (stderr or stdout).strip().splitlines()
        msg = err[-1] if err else f"exit code {r.returncode}"
        print(f"ERROR: extractor failed: {msg}")
        return 1
    try:
        items = json.loads(stdout[idx + len(marker):])
    except Exception as e:
        print(f"ERROR: invalid JSON output: {e}")
        return 1
    if not isinstance(items, list):
        print("ERROR: fetch() must return a list")
        return 1
    print(f"OK ({len(items)} items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _check_name(name: str) -> None:
    if name in BANNED_CALLS or name.startswith("__"):
        raise ValueError(f"'{name}' is not allowed")


def _check_format_str(fmt: str) -> None:
    try:
        parsed = list(string.Formatter().parse(fmt))
    except ValueError as e:
        raise ValueError(f"invalid format string: {e}") from None
    for _, field_name, format_spec, _ in parsed:
        if field_name is not None:
            if "__" in field_name or "[_" in field_name:
                raise ValueError("unsafe access in format string is not allowed")
            for attr in re.findall(r"\.([A-Za-z_]\w*)", field_name):
                if attr.startswith("_") or attr in BANNED_ATTRS:
                    raise ValueError(f"access to '{attr}' in format string is not allowed")
            for bracket in re.findall(r"\[([^\]]+)\]", field_name):
                key = bracket.strip()
                if key.startswith("_") or key in BANNED_ATTRS:
                    raise ValueError(f"access to '{key}' in format string is not allowed")
        if format_spec:
            _check_format_str(format_spec)


def check(code: str) -> None:
    """Raises ValueError if the code uses anything outside the allowed surface."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"syntax error: {e}") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            raise ValueError("class definitions are not allowed")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in MODULES:
                    raise ValueError(f"import of '{a.name}' is not allowed")
                if a.asname:
                    _check_name(a.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                raise ValueError("relative imports are not allowed")
            mod = node.module or ""
            for a in node.names:
                if a.name == "*":
                    raise ValueError("star imports are not allowed")
                if mod == "lookout_extract":
                    if a.name not in HELPERS:
                        raise ValueError(f"lookout_extract.{a.name} is not available")
                elif mod not in MODULES:
                    raise ValueError(f"import from '{mod}' is not allowed")
                elif a.name.startswith("_") or a.name in BANNED_CALLS or a.name in BANNED_ATTRS:
                    raise ValueError(f"import of '{mod}.{a.name}' is not allowed")
                if a.asname:
                    _check_name(a.asname)
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in BANNED_ATTRS:
                raise ValueError(f"access to '{node.attr}' is not allowed")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                if not (isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str)):
                    raise ValueError("dynamic '.format()' calls are not allowed")
                _check_format_str(node.func.value.value)
        elif isinstance(node, ast.Name):
            _check_name(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _check_name(node.name)
        elif isinstance(node, ast.arg):
            _check_name(node.arg)
    if not any(isinstance(n, ast.FunctionDef) and n.name == "fetch" for n in tree.body):
        raise ValueError("no fetch() function")


def _kill_tree(proc: subprocess.Popen) -> None:
    """Terminates a subprocess and all of its descendants."""
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdin=subprocess.DEVNULL,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _drain(pipe, buf: bytearray, limit: int, overflow: threading.Event, kill_cb) -> None:
    try:
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            if len(buf) + len(chunk) > limit:
                take = max(0, limit - len(buf))
                if take:
                    buf.extend(chunk[:take])
                overflow.set()
                kill_cb()
                break
            buf.extend(chunk)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def run(
    code: str,
    timeout: int = 180,
    max_output: int = MAX_OUTPUT_BYTES,
    tag: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
) -> List[Dict[str, Any]]:
    check(code)
    if cancel_event is not None and cancel_event.is_set():
        raise agents.CancelledError("cancelled")
    work = Path(tempfile.gettempdir()) / "lookout_extractor" / uuid.uuid4().hex[:10]
    work.mkdir(parents=True, exist_ok=True)
    proc = None
    try:
        (work / "extractor.py").write_text(code, encoding="utf-8")
        (work / "runner.py").write_text(RUNNER, encoding="utf-8")
        shutil.copy(Path(__file__).with_name("lookout_extract.py"), work / "lookout_extract.py")

        marker = f"@@LOOKOUT_{uuid.uuid4().hex}@@"
        cap = max(1024, int(max_output))
        kwargs: Dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.DEVNULL,
            "cwd": str(work),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen([sys.executable, "-I", str(work / "runner.py"), str(work), marker, str(cap)], **kwargs)
        entry: Dict[str, Any] = {"proc": proc, "tag": tag, "session": None, "cancelled": False, "browser_closed": False}
        with agents._proc_lock:
            agents._running[proc.pid] = entry
            if cancel_event is not None and cancel_event.is_set():
                entry["cancelled"] = True

        out_buf, err_buf = bytearray(), bytearray()
        overflow = threading.Event()
        kill_lock = threading.Lock()

        def kill_once():
            with kill_lock:
                _kill_tree(proc)

        if entry["cancelled"]:
            kill_once()

        t_out = threading.Thread(target=_drain, args=(proc.stdout, out_buf, cap + len(marker) + 1024, overflow, kill_once), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, err_buf, cap, overflow, kill_once), daemon=True)
        t_out.start()
        t_err.start()

        deadline = time.monotonic() + timeout
        timed_out = False
        cancelled = bool(entry["cancelled"])
        while proc.poll() is None:
            if entry["cancelled"] or (cancel_event is not None and cancel_event.is_set()):
                cancelled = True
                kill_once()
                break
            if overflow.is_set():
                break
            rem = deadline - time.monotonic()
            if rem <= 0:
                timed_out = True
                kill_once()
                break
            try:
                proc.wait(timeout=min(0.1, rem))
            except subprocess.TimeoutExpired:
                continue

        t_out.join(timeout=2)
        t_err.join(timeout=2)

        if cancelled or entry["cancelled"] or (cancel_event is not None and cancel_event.is_set()):
            raise agents.CancelledError("cancelled")
        if overflow.is_set():
            raise RuntimeError("extractor output exceeded size limit")
        if timed_out:
            raise RuntimeError(f"extractor took longer than {timeout}s")

        stdout = out_buf.decode("utf-8", errors="replace")
        stderr = err_buf.decode("utf-8", errors="replace")
        idx = stdout.rfind(marker)
        if proc.returncode != 0 or idx < 0:
            err = (stderr or stdout).strip().splitlines()
            raise RuntimeError("extractor failed: " + (err[-1] if err else f"exit code {proc.returncode}"))
        items = json.loads(stdout[idx + len(marker):])
        return [i for i in items if isinstance(i, dict)]
    finally:
        if proc is not None:
            with agents._proc_lock:
                agents._running.pop(proc.pid, None)
            if proc.poll() is None:
                _kill_tree(proc)
        shutil.rmtree(work, ignore_errors=True)


class _UnverifiedResult:
    """Sentinel returned by agrees() when agent_items is empty and verification cannot occur."""

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return "unverified"

    def __repr__(self) -> str:
        return "UNVERIFIED"

    def __eq__(self, other: object) -> bool:
        return other is self or (isinstance(other, str) and other == "unverified")

    def __hash__(self) -> int:
        return hash("unverified")


UNVERIFIED = _UnverifiedResult()


def _norm_title(s: Any) -> str:
    return " ".join(str(s or "").lower().split())[:80]


def _norm_url(s: Any) -> str:
    return str(s or "").strip().rstrip("/")


def _overlap_hits(source_items: List[Dict[str, Any]], target_items: List[Dict[str, Any]]) -> int:
    target_titles = {t for i in target_items if (t := _norm_title(i.get("title")))}
    target_urls = {u for i in target_items if (u := _norm_url(i.get("url")))}
    return sum(
        1 for item in source_items
        if ((t := _norm_title(item.get("title"))) and t in target_titles)
        or ((u := _norm_url(item.get("url"))) and u in target_urls)
    )


def agrees(agent_items: List[Dict], code_items: List[Dict]) -> Union[bool, _UnverifiedResult]:
    """Does the code find (most of) what the agent found by browsing?"""
    if not agent_items:
        return UNVERIFIED
    hits = _overlap_hits(agent_items, code_items)
    return hits >= max(1, int(len(agent_items) * 0.6))


def run_and_compare(
    code: str,
    *,
    timeout: int = 180,
    max_output: int = MAX_OUTPUT_BYTES,
    tag: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
    min_overlap: float = 0.7,
    allow_empty: bool = False,
    delay: float = 0.0,
) -> Tuple[bool, List[Dict[str, Any]], str]:
    """Execute `code` twice and verify that its outputs are consistent and non-empty."""
    first = run(code, timeout=timeout, max_output=max_output, tag=tag, cancel_event=cancel_event)
    if delay and delay > 0:
        if cancel_event is not None:
            if cancel_event.wait(delay):
                raise agents.CancelledError("cancelled")
        else:
            time.sleep(delay)
    if cancel_event is not None and cancel_event.is_set():
        raise agents.CancelledError("cancelled")
    second = run(code, timeout=timeout, max_output=max_output, tag=tag, cancel_event=cancel_event)

    if not first and not second:
        return (True, second, "") if allow_empty else (False, second, "extractor returned no items on both runs")
    if bool(first) != bool(second):
        return False, second, f"extractor results changed between runs ({len(first)} vs {len(second)})"

    lo, hi = min(len(first), len(second)), max(len(first), len(second))
    if lo / hi < min_overlap:
        return False, second, f"extractor item count varied between runs ({len(first)} vs {len(second)})"

    fwd_ratio = _overlap_hits(first, second) / len(first)
    rev_ratio = _overlap_hits(second, first) / len(second)
    if min(fwd_ratio, rev_ratio) < min_overlap:
        return (
            False,
            second,
            f"extractor results changed between runs ({min(fwd_ratio, rev_ratio):.0%} overlap < {min_overlap:.0%})",
        )
    return True, second, ""
