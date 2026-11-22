"""Desktop notification, optional e-mail digest and daily scheduling (per operating system)."""
import os
import shutil
import smtplib
import subprocess
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Dict, List, Tuple
from xml.sax.saxutils import escape as xesc

from lookout import db

TASK_NAME = "LookoutDailyScan"
_PS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:LOOKOUT_TOAST)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:LOOKOUT_APPID).Show((New-Object Windows.UI.Notifications.ToastNotification $x))
"""


def desktop(title: str, lines: List[str], url: str) -> None:
    lines = [str(l) for l in lines][:2]
    try:
        if sys.platform == "win32":
            body = "".join(f"<text>{xesc(t)}</text>" for t in [title, *lines])
            xml = (f'<toast activationType="protocol" launch="{xesc(url)}"><visual><binding template="ToastGeneric">'
                   f'{body}</binding></visual></toast>')
            env = dict(os.environ, LOOKOUT_TOAST=xml,
                       LOOKOUT_APPID=r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe")
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_TOAST], env=env,
                           timeout=30, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif sys.platform == "darwin":
            msg = " — ".join(lines).replace('"', "'")
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'], timeout=15)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, "\n".join(lines)], timeout=15)
    except Exception:
        pass


def mail_digest(items: List[Dict], panel_url: str) -> Tuple[bool, str]:
    s = db.get_settings()
    if not (s["smtp_user"] and s["smtp_password"] and s["mail_to"]):
        return False, "E-mail is not configured."
    rows = "".join(
        f'<tr><td style="padding:10px 0;border-bottom:1px solid #eee"><a href="{escape(i["url"], quote=True)}" '
        f'style="color:#111;font-weight:600;text-decoration:none">{escape(i["title"])}</a><br>'
        f'<span style="color:#666;font-size:13px">{escape(" · ".join(x for x in (i.get("org"), i["source_name"], i.get("deadline") and "deadline " + i["deadline"]) if x))}</span></td></tr>'
        for i in items[:40])
    html = (f'<div style="font-family:Segoe UI,Arial,sans-serif;max-width:620px"><h2 style="margin:0 0 6px">'
            f'{len(items)} new item{"s" if len(items) != 1 else ""}</h2><table width="100%">{rows}</table>'
            f'<p><a href="{panel_url}">Open Lookout</a> (works on the computer running Lookout)</p></div>')
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = f"Lookout: {len(items)} new", s["smtp_user"], s["mail_to"]
    msg.attach(MIMEText("\n".join(f"- {i['title']} {i['url']}" for i in items), "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(s["smtp_host"], int(s["smtp_port"] or 587), timeout=25) as srv:
            srv.starttls()
            srv.login(s["smtp_user"], s["smtp_password"])
            srv.sendmail(s["smtp_user"], [s["mail_to"]], msg.as_string())
        return True, f"Sent to {s['mail_to']}"
    except Exception as e:
        return False, f"Could not send: {e}"


# ---------- daily schedule ----------
def schedule_status() -> Dict:
    if sys.platform != "win32":
        return {"supported": False, "enabled": False,
                "hint": f"Add to crontab: 30 8 * * * cd {db.ROOT} && {sys.executable} run.py scan"}
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"supported": True, "enabled": r.returncode == 0}


def set_schedule(enabled: bool, at: str) -> Tuple[bool, str]:
    if sys.platform != "win32":
        return False, schedule_status()["hint"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if not enabled:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, creationflags=flags)
        return True, "Daily scan turned off."
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    # PowerShell is used because schtasks cannot set "run as soon as possible after a missed start".
    ps = (f"$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '\"{db.ROOT / 'run.py'}\" scan --scheduled' "
          f"-WorkingDirectory '{db.ROOT}'; $t = New-ScheduledTaskTrigger -Daily -At {at}; "
          f"$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
          f"Register-ScheduledTask -TaskName {TASK_NAME} -Action $a -Trigger $t -Settings $s -Force | Out-Null")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=flags)
    return (r.returncode == 0, f"Lookout will scan every day at {at}." if r.returncode == 0 else r.stderr[-300:])
