"""Detects AI coding agents installed on *this* computer and runs them non-interactively.

Every agent gets the same one-line prompt; the real task is written to TASK.md in a scratch
directory (passing long multi-line prompts through Windows .cmd shims is unreliable).
"""
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lookout import db

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROMPT = ("Read the file {dir}{sep}TASK.md and carry out exactly what it says. All files it mentions live in the "
          "folder {dir}. Do not ask questions.")


class CancelledError(RuntimeError):
    """Raised when an in-flight agent or extractor process is cancelled."""


def prompt_for(workdir: str) -> str:
    # The absolute path matters: some CLIs (e.g. opencode) work in their own project folder, not the process cwd.
    return PROMPT.format(dir=workdir, sep=os.sep)


def _claude(exe, model, workdir, timeout):
    # Only the browser CLI and file tools are allowed; no general shell access.
    cmd = [exe, "-p", prompt_for(workdir), "--add-dir", workdir, "--permission-mode", "acceptEdits", "--allowedTools",
           "Bash(agent-browser:*)", "Bash(agent-browser *)", "Bash(python test_extractor.py)",
           "Bash(python3 test_extractor.py)", "Read", "Write", "Edit", "WebSearch", "WebFetch"]
    return cmd + (["--model", model] if model else [])


def _agy(exe, model, workdir, timeout):
    cmd = [exe, "-p", prompt_for(workdir), "--add-dir", workdir, "--dangerously-skip-permissions", "--print-timeout", f"{timeout}s"]
    return cmd + (["--model", model] if model else [])


# verified: flags checked against the CLI's --help on a real install
AGENTS: Dict[str, Dict] = {
    "claude": {"name": "Claude Code", "exe": "claude", "verified": True, "build": _claude,
               "models": [("haiku", "Claude Haiku (fastest)"), ("sonnet", "Claude Sonnet"), ("opus", "Claude Opus")]},
    "codex": {"name": "Codex CLI", "exe": "codex", "verified": False,
              "build": lambda e, m, w, t: [e, "exec", prompt_for(w), "--full-auto"] + (["-m", m] if m else []),
              "models": []},
    "gemini": {"name": "Gemini CLI", "exe": "gemini", "verified": True,
               "build": lambda e, m, w, t: [e, "-p", prompt_for(w), "--yolo"] + (["-m", m] if m else []),
               "models": [("gemini-2.5-flash", "Gemini 2.5 Flash"), ("gemini-2.5-pro", "Gemini 2.5 Pro")]},
    "agy": {"name": "Antigravity CLI", "exe": "agy", "verified": True, "build": _agy, "list_models": ["models"]},
    "opencode": {"name": "opencode", "exe": "opencode", "verified": True, "list_models": ["models"],
                 "build": lambda e, m, w, t: [e, "run", prompt_for(w), "--auto"] + (["-m", m] if m else [])},
    "cursor-agent": {"name": "Cursor Agent", "exe": "cursor-agent", "verified": False,
                     "build": lambda e, m, w, t: [e, "-p", prompt_for(w), "--force"] + (["--model", m] if m else []),
                     "models": []},
    "qwen": {"name": "Qwen Code", "exe": "qwen", "verified": False,
             "build": lambda e, m, w, t: [e, "-p", prompt_for(w), "--yolo"] + (["-m", m] if m else []), "models": []},
    "copilot": {"name": "GitHub Copilot CLI", "exe": "copilot", "verified": False,
                "build": lambda e, m, w, t: [e, "-p", prompt_for(w), "--allow-all-tools"] + (["--model", m] if m else []),
                "models": []},
}

_model_cache: Dict[str, Tuple[float, List[Tuple[str, str]]]] = {}
QUOTA = re.compile(r"quota|rate.?limit|usage limit|resource_exhausted|\b429\b|limit reached|too many requests", re.I)
_SHELL_META_RE = re.compile(r"(&&|\|\||[|;`]|\$\(|[\r\n])")

_proc_lock = threading.Lock()
_running: Dict[int, Dict[str, Any]] = {}


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                           creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=0.3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def close_browser_session(session: Optional[str]) -> None:
    if not session:
        return
    exe = shutil.which("agent-browser")
    if not exe:
        return
    try:
        subprocess.run([exe, "--session", session, "close"], capture_output=True, timeout=15,
                       creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    except Exception:
        pass


def cancel(tag: Optional[str] = None, async_browser: bool = False) -> int:
    """Terminates tracked subprocess trees matching tag (or all) and closes their browser sessions."""
    with _proc_lock:
        matched = [e for e in _running.values() if tag is None or e["tag"] == tag]
        procs = []
        sessions = []
        for e in matched:
            e["cancelled"] = True
            procs.append(e["proc"])
            s = e.get("session")
            if s and not e["browser_closed"]:
                e["browser_closed"] = True
                if s not in sessions:
                    sessions.append(s)
    for proc in procs:
        _kill_tree(proc)
    if sessions:
        def _close_all():
            for s in sessions:
                close_browser_session(s)
        if async_browser:
            threading.Thread(target=_close_all, daemon=True).start()
        else:
            _close_all()
    return len(procs)


def _run_proc(cmd: List[str], timeout: int, cwd: Optional[str] = None, tag: Optional[str] = None,
              session: Optional[str] = None, cancel_event: Optional[threading.Event] = None) -> Tuple[int, str, str]:
    if cancel_event and cancel_event.is_set():
        raise CancelledError("cancelled")
    kwargs: Dict[str, Any] = dict(
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        cwd=cwd, stdin=subprocess.DEVNULL,
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as e:
        return -1, "", str(e)
    entry: Dict[str, Any] = {"proc": proc, "tag": tag, "session": session, "cancelled": False, "browser_closed": False}
    with _proc_lock:
        _running[proc.pid] = entry
        if cancel_event and cancel_event.is_set():
            entry["cancelled"] = True
    if entry["cancelled"]:
        _kill_tree(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        if entry["cancelled"] or (cancel_event and cancel_event.is_set()):
            raise CancelledError("cancelled")
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        if entry["cancelled"] or (cancel_event and cancel_event.is_set()):
            raise CancelledError("cancelled")
        if session and not entry["browser_closed"]:
            with _proc_lock:
                entry["browser_closed"] = True
            close_browser_session(session)
        return -9, out or "", (err or "") + "\n[timed out]"
    finally:
        close_now = False
        with _proc_lock:
            _running.pop(proc.pid, None)
            if entry["cancelled"] and session and not entry["browser_closed"]:
                entry["browser_closed"] = True
                close_now = True
        if close_now:
            close_browser_session(session)


def run_tracked(cmd: List[str], timeout: int, cwd: Optional[str] = None, tag: Optional[str] = None,
                session: Optional[str] = None, cancel_event: Optional[threading.Event] = None) -> Tuple[int, str]:
    code, out, err = _run_proc(cmd, timeout, cwd=cwd, tag=tag, session=session, cancel_event=cancel_event)
    return code, out + err


def _run(cmd: List[str], timeout: int, cwd: Optional[str] = None) -> Tuple[int, str]:
    return run_tracked(cmd, timeout, cwd)


def _split_custom(cmd: str) -> List[str]:
    posix = sys.platform != "win32"
    parts = shlex.split(cmd, posix=posix)
    if not posix:
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in ('"', "'") else p for p in parts]
    return parts


def validate_custom_command(cmd: str) -> str:
    """Validate a custom command template, rejecting shell metacharacters and broken quoting."""
    s = str(cmd or "").strip()
    if not s:
        return ""
    if _SHELL_META_RE.search(s):
        raise ValueError("Custom command cannot contain shell metacharacters (&&, |, ;, backticks, $()).")
    try:
        parts = _split_custom(s)
    except ValueError as e:
        raise ValueError(f"Invalid custom command: {e}") from None
    if not parts:
        return ""
    return s


def which(agent_id: str) -> Optional[str]:
    if agent_id == "custom":
        cmd = db.get_settings().get("custom_command", "")
        if not cmd.strip():
            return None
        try:
            clean = validate_custom_command(cmd)
            parts = _split_custom(clean)
        except ValueError:
            return None
        return shutil.which(parts[0]) if parts else None
    if agent_id not in AGENTS:
        return None
    return shutil.which(AGENTS[agent_id]["exe"])


def version(exe: str) -> Optional[str]:
    code, out = _run([exe, "--version"], 20)
    m = re.search(r"\d+\.\d+(\.\d+)?", out)
    return m.group(0) if m else ("?" if code == 0 else None)


def browser_tool() -> Dict:
    """agent-browser is what every agent uses to drive the browser."""
    exe = shutil.which("agent-browser")
    return {"installed": bool(exe), "version": version(exe) if exe else None,
            "install": "npm install -g agent-browser && agent-browser install"}


def models(agent_id: str) -> List[Tuple[str, str]]:
    info = AGENTS.get(agent_id, {})
    if "list_models" not in info:
        return info.get("models", [])
    cached = _model_cache.get(agent_id)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    exe, out_list = which(agent_id), []
    if exe:
        _, out = _run([exe, *info["list_models"]], 60)
        for line in out.splitlines():
            line = line.strip()
            if "\t" in line:
                k, name = line.split("\t", 1)
                out_list.append((k.strip(), name.strip()))
            elif re.match(r"^[\w.-]+/[\w.:#-]+$", line):
                out_list.append((line, line))
    _model_cache[agent_id] = (time.time(), out_list)
    return out_list


def detect() -> List[Dict]:
    """All known agents with install state on this machine, plus the user's custom command."""
    out = []
    for aid, info in AGENTS.items():
        exe = shutil.which(info["exe"])
        out.append({"id": aid, "name": info["name"], "installed": bool(exe), "version": version(exe) if exe else None,
                    "verified": info["verified"], "lists_models": "list_models" in info,
                    "usage": db.agent_usage(aid) if exe else []})
    custom = db.get_settings().get("custom_command", "")
    out.append({"id": "custom", "name": "Custom command", "installed": bool(which("custom")), "version": None,
                "verified": False, "lists_models": False, "command": custom, "usage": db.agent_usage("custom")})
    return out


def _resets_at(text: str) -> Optional[str]:
    m = re.search(r"resets? in\s+((\d+)h)?\s*((\d+)m)?\s*((\d+)s)?", text, re.I)
    if not m or not any(m.group(i) for i in (2, 4, 6)):
        return None
    secs = int(m.group(2) or 0) * 3600 + int(m.group(4) or 0) * 60 + int(m.group(6) or 0)
    return (datetime.now() + timedelta(seconds=secs)).strftime(db.TS)


def build_command(agent_id: str, model: str, workdir: str, timeout: int) -> List[str]:
    if agent_id == "custom":
        tpl = db.get_settings().get("custom_command", "")
        if not tpl.strip():
            raise RuntimeError("Custom command is empty")
        try:
            tpl = validate_custom_command(tpl)
            parts = _split_custom(tpl)
        except ValueError as e:
            raise RuntimeError(str(e)) from None
        if not parts:
            raise RuntimeError("Custom command is empty")
        parts = [p.replace("{prompt}", prompt_for(workdir)).replace("{model}", model or "").replace("{dir}", workdir) for p in parts]
        if "{prompt}" not in tpl:
            parts.append(prompt_for(workdir))
        exe = shutil.which(parts[0])
        return [exe or parts[0], *parts[1:]]
    if agent_id not in AGENTS:
        raise ValueError(f"Unknown agent: {agent_id}")
    exe = which(agent_id) or AGENTS[agent_id]["exe"]
    return AGENTS[agent_id]["build"](exe, model, workdir, timeout)


def run_task(agent_id: str, model: str, workdir: Path, timeout: int, tag: Optional[str] = None,
             session: Optional[str] = None, cancel_event: Optional[threading.Event] = None) -> str:
    """Runs the agent inside workdir. Raises when missing, out of quota, or cancelled."""
    if cancel_event and cancel_event.is_set():
        raise CancelledError("cancelled")
    if not which(agent_id):
        raise RuntimeError(f"{agent_id} is not installed on this computer")
    blocked = db.agent_blocked_until(agent_id, model)
    if blocked:
        raise RuntimeError(f"{agent_id} quota is used up until {blocked[11:16]}")
    code, out = run_tracked(build_command(agent_id, model, str(workdir), timeout), timeout + 60, str(workdir),
                            tag=tag, session=session, cancel_event=cancel_event)
    quota_hit = code != 0 and bool(QUOTA.search(out))
    db.log_agent_call(agent_id, model, ok=code == 0 and not quota_hit, error=out[-400:] if code else "",
                      resets_at=_resets_at(out) if quota_hit else None)
    if quota_hit:
        raise RuntimeError(f"{agent_id} quota is used up")
    return out
