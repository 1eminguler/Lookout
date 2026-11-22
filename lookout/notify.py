"""Desktop notification, optional e-mail digest and daily scheduling (per operating system)."""
import os
import re
import shutil
import smtplib
import socket
import subprocess
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any, Dict, List, Tuple
from xml.sax.saxutils import escape as xesc

from lookout import db

TASK_NAME = "LookoutDailyScan"
_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_schedule_cache: Dict[str, Any] = {"t": 0.0, "plat": None, "val": None}
_PS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:LOOKOUT_TOAST)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:LOOKOUT_APPID).Show((New-Object Windows.UI.Notifications.ToastNotification $x))
"""


def _as_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def desktop(title: str, lines: List[str], url: str) -> None:
    lines = [str(l) for l in (lines or [])][:2]
    try:
        if sys.platform == "win32":
            ps_exe = shutil.which("powershell.exe")
            if not ps_exe:
                return
            body = "".join(f"<text>{xesc(t)}</text>" for t in [str(title), *lines])
            xml = (f'<toast activationType="protocol" launch="{xesc(str(url))}"><visual><binding template="ToastGeneric">'
                   f'{body}</binding></visual></toast>')
            env = dict(os.environ, LOOKOUT_TOAST=xml,
                       LOOKOUT_APPID=r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe")
            subprocess.run([ps_exe, "-NoProfile", "-NonInteractive", "-Command", _PS_TOAST], env=env,
                           timeout=30, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif sys.platform == "darwin":
            osa = shutil.which("osascript")
            if not osa:
                return
            msg = _as_escape(" — ".join(lines))
            t = _as_escape(title)
            subprocess.run([osa, "-e", f'display notification "{msg}" with title "{t}"'],
                           timeout=15, capture_output=True)
        else:
            ns = shutil.which("notify-send")
            if ns:
                subprocess.run([ns, str(title), "\n".join(lines)], timeout=15, capture_output=True)
    except Exception:
        pass


def mail_digest(items: List[Dict], panel_url: str) -> Tuple[bool, str]:
    s = db.get_settings()
    if not (s.get("smtp_user") and s.get("smtp_password") and s.get("mail_to")):
        return False, "E-mail is not configured."
    host = (s.get("smtp_host") or "").strip()
    if not host:
        return False, "SMTP server is not configured."
    raw_port = str(s.get("smtp_port") or "587").strip()
    try:
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        return False, f"Invalid SMTP port: {raw_port}"

    rows = "".join(
        f'<tr><td style="padding:10px 0;border-bottom:1px solid #eee"><a href="{escape(str(i.get("url", "")), quote=True)}" '
        f'style="color:#111;font-weight:600;text-decoration:none">{escape(str(i.get("title", "")))}</a><br>'
        f'<span style="color:#666;font-size:13px">{escape(" · ".join(x for x in (i.get("org"), i.get("source_name"), i.get("deadline") and "deadline " + str(i["deadline"])) if x))}</span></td></tr>'
        for i in items[:40])
    html = (f'<div style="font-family:Segoe UI,Arial,sans-serif;max-width:620px"><h2 style="margin:0 0 6px">'
            f'{len(items)} new item{"s" if len(items) != 1 else ""}</h2><table width="100%">{rows}</table>'
            f'<p><a href="{escape(str(panel_url), quote=True)}">Open Lookout</a> (works on the computer running Lookout)</p></div>')
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = f"Lookout: {len(items)} new", s["smtp_user"], s["mail_to"]
    msg.attach(MIMEText("\n".join(f"- {i.get('title', '')} {i.get('url', '')}" for i in items), "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(host, port, timeout=25) as srv:
            srv.starttls()
            srv.login(s["smtp_user"], s["smtp_password"])
            srv.sendmail(s["smtp_user"], [s["mail_to"]], msg.as_string())
        return True, f"Sent to {s['mail_to']}"
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed: check your e-mail address and app password."
    except (smtplib.SMTPConnectError, ConnectionRefusedError, socket.gaierror) as e:
        return False, f"Could not connect to SMTP server {host}:{port}: {e}"
    except (TimeoutError, socket.timeout):
        return False, f"Timed out connecting to SMTP server {host}:{port}."
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {e}"
    except Exception as e:
        return False, f"Could not send: {e}"


# ---------- daily schedule ----------
def _cron_hint() -> str:
    return f"Daily schedule requires Windows Task Scheduler. Add to crontab: 30 8 * * * cd {db.ROOT} && {sys.executable} run.py scan --scheduled"


def schedule_status(use_cache: bool = True) -> Dict:
    if (use_cache and _schedule_cache["plat"] == sys.platform
            and _schedule_cache["val"] is not None and time.monotonic() - _schedule_cache["t"] < 60):
        return dict(_schedule_cache["val"])
    if sys.platform != "win32":
        res = {"supported": False, "enabled": False, "hint": _cron_hint()}
    else:
        try:
            r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            res = {"supported": True, "enabled": r.returncode == 0}
        except (OSError, subprocess.SubprocessError):
            res = {"supported": True, "enabled": False}
    _schedule_cache.update(t=time.monotonic(), plat=sys.platform, val=dict(res))
    return res


def set_schedule(enabled: bool, at: str) -> Tuple[bool, str]:
    if sys.platform != "win32":
        return False, _cron_hint()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _schedule_cache["t"] = 0.0
    if not enabled:
        try:
            subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True,
                           timeout=15, creationflags=flags)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"Could not turn off daily scan: {e}"
        return True, "Daily scan turned off."
    at_clean = str(at or "").strip()
    if not _TIME_RE.fullmatch(at_clean):
        return False, "Time must be in HH:MM (24-hour) format."
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    exe_ps = exe.replace("'", "''")
    root_ps = str(db.ROOT).replace("'", "''")
    script_ps = str(db.ROOT / "run.py").replace("'", "''")
    # PowerShell is used because schtasks cannot set "run as soon as possible after a missed start".
    ps = (f"$a = New-ScheduledTaskAction -Execute '{exe_ps}' -Argument '\"{script_ps}\" scan --scheduled' "
          f"-WorkingDirectory '{root_ps}'; $t = New-ScheduledTaskTrigger -Daily -At {at_clean}; "
          f"$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
          f"Register-ScheduledTask -TaskName {TASK_NAME} -Action $a -Trigger $t -Settings $s -Force | Out-Null")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Could not update daily scan: {e}"
    return (r.returncode == 0, f"Lookout will scan every day at {at_clean}." if r.returncode == 0 else (r.stderr or r.stdout)[-300:].strip() or "Could not schedule daily scan.")
