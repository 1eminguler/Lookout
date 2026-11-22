#!/usr/bin/env python3
"""Lookout — monitor any website with the AI agent you already have installed.

    python run.py            open the app in your browser
    python run.py scan       run one scan now (used by the daily schedule)
"""
import argparse
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from lookout import db, notify, scanner, server  # noqa: E402

URL = f"http://127.0.0.1:{server.PORT}/"


def port_busy() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", server.PORT)) == 0


def wait_for_internet(seconds: int = 300) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=5).close()
            return True
        except OSError:
            time.sleep(10)
    return False


def start_ui_in_background() -> None:
    if port_busy():
        return
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else sys.executable)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([exe, str(ROOT / "run.py"), "--background"], cwd=str(ROOT), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


def cmd_scan(scheduled: bool) -> None:
    db.init()
    db.mark_interrupted()
    if not wait_for_internet():
        print("No internet connection; scan skipped.")
        return
    print("Scanning…")
    scan_id = scanner.run_scan("scheduled" if scheduled else "manual")
    if scan_id is None:
        print("A scan is already running.")
        return
    d = db.scan_detail(scan_id)
    print(f"{d['found']} items, {d['new']} new. "
          f"{sum(1 for s in d['sources'] if s['ok'])}/{len(d['sources'])} sources read.")
    fresh = db.unnotified()
    if fresh and db.get_settings().get("notify") == "1":
        start_ui_in_background()
        notify.desktop(f"{len(fresh)} new item{'s' if len(fresh) != 1 else ''}",
                       [f"{i['title']} — {i['source_name']}" for i in fresh[:2]], URL)
        ok, msg = notify.mail_digest(fresh, URL)
        if ok:
            print(msg)
    db.mark_notified(i["id"] for i in fresh)
    # Agenda reminders the user set for today
    due = [i for i in db.items() if i["remind_on"] == time.strftime("%Y-%m-%d") and i["action"] == "saved"]
    if due and db.get_settings().get("notify") == "1":
        notify.desktop(f"Reminder: {due[0]['title']}" if len(due) == 1 else f"{len(due)} reminders today",
                       [i["title"] for i in due[:2]] if len(due) > 1 else [due[0].get("org") or due[0]["source_name"]], URL)


def main() -> None:
    ap = argparse.ArgumentParser(description="Lookout")
    ap.add_argument("command", nargs="?", default="ui", choices=["ui", "scan"])
    ap.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--browser", action="store_true", help="open in your web browser instead of a window")
    a = ap.parse_args()
    if a.command == "scan":
        return cmd_scan(a.scheduled)
    if port_busy():
        if not a.background and (a.browser or not open_window(start_server=False)):
            webbrowser.open(URL)
        return
    if a.background:
        return server.serve(idle_minutes=12 * 60)
    if a.browser or not open_window():
        print(f"Lookout is running at {URL}  (close this window to stop)")
        __import__("threading").Timer(0.5, lambda: webbrowser.open(URL)).start()
        server.serve()


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
    import threading
    # One window at a time: a second launch while Lookout is open does nothing.
    global _single
    _single = socket.socket()
    try:
        _single.bind(("127.0.0.1", server.PORT + 1))
    except OSError:
        return True
    srv = None
    if start_server:
        srv = server.make_server()
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        for _ in range(50):
            if port_busy():
                break
            time.sleep(0.1)
    try:
        api = WindowApi()
        api._window = webview.create_window("Lookout", URL, js_api=api, width=1320, height=860, min_size=(960, 620),
                                            frameless=True, easy_drag=False, background_color="#0A0A0B")
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
        try:
            _single.close()
        except Exception:
            pass
        return False


if __name__ == "__main__":
    main()
