#!/usr/bin/env python3
"""Lookout — monitor any website with the AI agent you already have installed.

    python run.py            open the app in your browser
    python run.py scan       run one scan now (used by the daily schedule)
"""
import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from lookout import agents, db, notify, scanner, server  # noqa: E402

URL = f"http://127.0.0.1:{server.PORT}/"


def _window_lock_path() -> Path:
    return db.DB_PATH.parent / "window.pid"


def acquire_window_lock() -> bool:
    p = _window_lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_file():
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            pid = int(lines[0]) if lines else 0
            if pid and pid != os.getpid() and db._pid_alive(pid):
                return False
        except (OSError, ValueError):
            pass
    try:
        p.write_text(f"{os.getpid()}\n{server.PORT}\n", encoding="utf-8")
    except OSError:
        pass
    return True


def release_window_lock() -> None:
    p = _window_lock_path()
    try:
        if not p.is_file():
            return
        lines = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if lines and lines[0].isdigit() and int(lines[0]) == os.getpid():
            p.unlink(missing_ok=True)
    except OSError:
        pass


def _launch_url(srv=None) -> str:
    if srv is not None:
        sec = getattr(srv, "launch_secret", None)
        port = srv.server_address[1]
        return f"http://127.0.0.1:{port}/?k={sec}" if sec else f"http://127.0.0.1:{port}/"
    sec_file = db.DB_PATH.parent / ".launch_secret"
    if sec_file.is_file():
        try:
            sec = sec_file.read_text(encoding="utf-8", errors="replace").strip()
            if sec:
                return f"http://127.0.0.1:{server.PORT}/?k={sec}"
        except OSError:
            pass
    return f"http://127.0.0.1:{server.PORT}/"


def port_busy() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", server.PORT)) == 0


def wait_for_internet(seconds: int = 60) -> bool:
    """Look for evidence of connectivity within a bounded wait."""
    end = time.monotonic() + min(seconds, 60)
    source_hosts = {}
    for source in db.sources(active_only=True, include_code=False):
        try:
            parts = urlsplit(source["url"])
            if parts.scheme in ("http", "https") and parts.hostname:
                source_hosts.setdefault(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        except ValueError:
            continue
        if len(source_hosts) == 3:
            break

    def connect(host, port):
        socket.create_connection((host, port), timeout=2).close()

    probes = [(connect, (host, port)) for host, port in source_hosts.items()]
    probes.extend((connect, (host, 443)) for host in ("1.1.1.1", "8.8.8.8"))
    if source_hosts:
        probes.append((socket.getaddrinfo, (next(iter(source_hosts)), None)))

    def probe(check, args):
        remaining = end - time.monotonic()
        if remaining <= 0:
            return False
        succeeded = threading.Event()

        def attempt():
            try:
                check(*args)
            except OSError:
                return
            succeeded.set()

        # Name resolution can outlast the socket timeout.
        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(min(3, max(0, end - time.monotonic())))
        return succeeded.is_set()

    while time.monotonic() < end:
        for check, args in probes:
            if probe(check, args):
                return True
        time.sleep(min(5, max(0, end - time.monotonic())))
    return False


def start_ui_in_background() -> None:
    if port_busy():
        return
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else sys.executable)
    kwargs = dict(
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([exe, str(ROOT / "run.py"), "--background"], **kwargs)
    if isinstance(getattr(proc, "pid", None), int):
        sec_file = db.DB_PATH.parent / ".launch_secret"
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if port_busy() and sec_file.is_file():
                break
            time.sleep(0.05)


def cmd_scan(scheduled: bool) -> None:
    db.init()
    db.mark_interrupted()
    if not wait_for_internet():
        print("Internet check was inconclusive. Continuing with the scan.")
    print("Scanning…")
    scan_id = scanner.run_scan(
        "scheduled" if scheduled else "manual",
        panel_url=URL,
        before_desktop=start_ui_in_background,
    )
    if scan_id is None:
        print("A scan is already running.")
        return
    d = db.scan_detail(scan_id)
    print(f"{d['found']} items, {d['new']} new. "
          f"{sum(1 for s in d['sources'] if s['ok'])}/{len(d['sources'])} sources read.")
    # Agenda reminders the user set for today
    due = [i for i in db.items() if i["remind_on"] == time.strftime("%Y-%m-%d") and i["action"] == "saved"]
    if due and db.get_settings().get("notify") == "1":
        start_ui_in_background()
        notify.desktop(f"Reminder: {due[0]['title']}" if len(due) == 1 else f"{len(due)} reminders today",
                       [i["title"] for i in due[:2]] if len(due) > 1 else [due[0].get("org") or due[0]["source_name"]],
                       notify.desktop_launch_url(URL))


def main() -> None:
    ap = argparse.ArgumentParser(description="Lookout")
    ap.add_argument("command", nargs="?", default="ui", choices=["ui", "scan"])
    ap.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--browser", action="store_true", help="open in your web browser instead of a window")
    ap.add_argument("--custom-command", default=None, help="custom agent command template (local CLI only)")
    a = ap.parse_args()
    if a.custom_command is not None:
        try:
            agents.set_cli_custom_command(a.custom_command)
        except ValueError as e:
            print(f"Invalid --custom-command: {e}", file=sys.stderr)
            sys.exit(2)
    if a.command == "scan":
        return cmd_scan(a.scheduled)
    if port_busy():
        if not a.background and (a.browser or not open_window(start_server=False)):
            webbrowser.open(_launch_url())
        return
    if a.background:
        return server.serve(idle_minutes=12 * 60)
    if a.browser or not open_window():
        print(f"Lookout is running at {URL}  (close this window to stop)")
        server.serve(on_start=lambda srv: threading.Timer(0.5, lambda: webbrowser.open(_launch_url(srv))).start())


class WindowApi:
    """Called from the page (window.pywebview.api) for the custom title bar."""
    def __init__(self):
        self._window = None
        self._maximized = False

    def minimize(self):
        self._window.minimize()

    def toggle_maximize(self):
        self._window.restore() if self._maximized else self._window.maximize()
        self._maximized = not self._maximized

    def close(self):
        self._window.destroy()

    def open_external(self, url):
        if str(url).startswith(("http://", "https://")):
            webbrowser.open(url)


def open_window(start_server: bool = True) -> bool:
    """Native desktop window (pywebview). Falls back to browser when unavailable or failing to start."""
    try:
        import webview
    except Exception as e:
        print(f"Desktop window unavailable ({e}); opening in browser.")
        return False
    if not acquire_window_lock():
        print(f"Lookout is already open in another window ({URL}).")
        return True
    srv = None
    try:
        if start_server:
            srv = server.make_server()
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            for _ in range(50):
                if port_busy():
                    break
                time.sleep(0.1)
        api = WindowApi()
        api._window = webview.create_window("Lookout", _launch_url(srv), js_api=api, width=1320, height=860,
                                            min_size=(960, 620), frameless=True, easy_drag=False,
                                            background_color="#0A0A0B")
        webview.start()
        return True
    except Exception as e:
        print(f"Could not start desktop window ({e}); opening in browser.")
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        return False
    finally:
        release_window_lock()


if __name__ == "__main__":
    main()
