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
from typing import Any, Dict, List, Optional

from lookout import agents

HELPERS = {"get", "get_json", "post_json", "browser_eval", "absolute"}
MODULES = {"json", "re", "html", "math", "time", "datetime", "itertools", "collections", "typing", "urllib.parse",
           "unicodedata", "string", "functools"}
BANNED_CALLS = {
    "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "hasattr", "input", "breakpoint", "memoryview", "help", "type", "object", "super", "dir",
    "issubclass", "classmethod", "staticmethod", "property", "exit", "quit", "license", "credits",
    "copyright", "Formatter",
}
BANNED_ATTRS = {
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
}
MAX_OUTPUT_BYTES = 5 * 1024 * 1024

RUNNER = """import importlib, json, os, sys, types
sys.stdout.reconfigure(encoding="utf-8")
work = sys.argv[1]
marker = sys.argv[2] if len(sys.argv) > 2 else "@@LOOKOUT@@"
max_bytes = int(sys.argv[3]) if len(sys.argv) > 3 else 5242880
sys.path.insert(0, work)
import lookout_extract as _le

_HELPERS = {"get", "get_json", "post_json", "browser_eval", "absolute"}
_MODULES = {"json", "re", "html", "math", "time", "datetime", "itertools", "collections", "typing", "urllib.parse",
            "unicodedata", "string", "functools"}
_BANNED_ATTRS = {
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
}

def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level != 0:
        raise ImportError("relative imports are not allowed")
    if name == "lookout_extract":
        if not fromlist or any(a not in _HELPERS for a in fromlist):
            raise ImportError("unsupported import from lookout_extract")
        return types.SimpleNamespace(**{k: getattr(_le, k) for k in _HELPERS})
    if name in _MODULES:
        if fromlist and any(a == "*" or a.startswith("_") or a in _BANNED_ATTRS for a in fromlist):
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
payload = json.dumps(items, ensure_ascii=False, default=str)
if len(payload.encode("utf-8")) > max_bytes:
    raise ValueError("extractor output exceeded size limit")
print(marker + payload)
"""


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


def agrees(agent_items: List[Dict], code_items: List[Dict]) -> bool:
    """Does the code find (most of) what the agent found by browsing?"""
    if not agent_items:
        return True
    norm = lambda s: " ".join(str(s or "").lower().split())[:80]
    code_titles = {t for i in code_items if (t := norm(i.get("title")))}
    code_urls = {u for i in code_items if (u := str(i.get("url") or "").strip().rstrip("/"))}
    hits = sum(
        1 for a in agent_items
        if ((t := norm(a.get("title"))) and t in code_titles)
        or ((u := str(a.get("url") or "").strip().rstrip("/")) and u in code_urls)
    )
    return hits >= max(1, int(len(agent_items) * 0.6))
