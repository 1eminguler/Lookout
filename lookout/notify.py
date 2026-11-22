"""Desktop notification, optional e-mail digest, webhook output and daily scheduling (per OS)."""
import json
import os
import plistlib
import re
import shlex
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as xesc

from lookout import db, lookout_extract

TASK_NAME = "LookoutDailyScan"
SYSTEMD_UNIT = "lookout-scan"
LAUNCHD_LABEL = "com.lookout.dailyscan"
CRON_MARKER = "# lookout-daily-scan"
_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_schedule_cache: Dict[str, Any] = {"t": 0.0, "plat": None, "val": None}
_MAX_WEBHOOK_RESP = 4096
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
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=25, context=ctx) as srv:
                srv.login(s["smtp_user"], s["smtp_password"])
                srv.sendmail(s["smtp_user"], [s["mail_to"]], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=25) as srv:
                srv.ehlo()
                srv.starttls(context=ctx)
                srv.ehlo()
                srv.login(s["smtp_user"], s["smtp_password"])
                srv.sendmail(s["smtp_user"], [s["mail_to"]], msg.as_string())
        return True, f"Sent to {s['mail_to']}"
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed: check your e-mail address and app password."
    except ssl.SSLCertVerificationError as e:
        return False, f"TLS certificate verification failed for {host}:{port}: {e}"
    except ssl.SSLError as e:
        return False, f"TLS/SSL error for {host}:{port}: {e}"
    except (smtplib.SMTPConnectError, ConnectionRefusedError, socket.gaierror) as e:
        return False, f"Could not connect to SMTP server {host}:{port}: {e}"
    except (TimeoutError, socket.timeout):
        return False, f"Timed out connecting to SMTP server {host}:{port}."
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {e}"
    except Exception as e:
        return False, f"Could not send: {e}"


# ---------- webhooks (JSON POST + ntfy) ----------
def _allow_local_webhooks() -> bool:
    s = db.get_settings()
    for key in ("allow_local_webhooks", "webhook_allow_local", "allow_local_webhook"):
        if str(s.get(key) or "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


def _is_ntfy_topic(url: str) -> bool:
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    path = p.path.strip("/")
    if not path or "/" in path:
        return False
    return host == "ntfy.sh" or host.endswith(".ntfy.sh") or host.startswith("ntfy.")


def _safe_header(value: Any) -> str:
    txt = str(value or "").replace("\r", " ").replace("\n", " ").replace("—", "-").replace("–", "-").strip()
    return txt.encode("latin-1", "replace").decode("latin-1")


def _format_ntfy(payload: Any) -> Tuple[bytes, Dict[str, str]]:
    hdrs: Dict[str, str] = {"Content-Type": "text/plain; charset=utf-8"}
    if isinstance(payload, dict):
        title = payload.get("title") or payload.get("subject")
        if title:
            hdrs["Title"] = _safe_header(title)[:250]
        click = payload.get("click") or payload.get("url") or payload.get("panel_url")
        if isinstance(click, str) and click.strip().startswith(("http://", "https://")):
            hdrs["Click"] = _safe_header(click.strip())[:500]
        tags = payload.get("tags")
        if isinstance(tags, (list, tuple)):
            clean_tags = [_safe_header(t) for t in tags if str(t or "").strip()]
            if clean_tags:
                hdrs["Tags"] = ",".join(clean_tags)[:200]
        elif isinstance(tags, str) and tags.strip():
            hdrs["Tags"] = _safe_header(tags)[:200]

        body = payload.get("message") or payload.get("text") or payload.get("body")
        lines: List[str] = [str(body)] if body else []
        items = payload.get("items")
        if isinstance(items, list) and items:
            for item in items[:10]:
                if isinstance(item, dict):
                    parts = [str(item.get("title") or "Untitled")]
                    if item.get("source_name"):
                        parts.append(str(item["source_name"]))
                    line = "- " + " — ".join(parts)
                    if item.get("url"):
                        line += f" ({item['url']})"
                    lines.append(line)
                else:
                    lines.append(f"- {item}")
            if len(items) > 10:
                lines.append(f"...and {len(items) - 10} more")
        text = "\n".join(lines).strip() or json.dumps(payload, ensure_ascii=False, default=str)
        return text.encode("utf-8"), hdrs
    if isinstance(payload, list):
        text = "\n".join(f"- {x.get('title', x) if isinstance(x, dict) else x}" for x in payload[:10]) or "Lookout update"
        return text.encode("utf-8"), hdrs
    return str(payload or "Lookout notification").encode("utf-8"), hdrs


def webhook(
    url: str,
    payload: Any,
    *,
    timeout: float = 15.0,
    allow_local: Optional[bool] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """Deliver a notification payload as JSON POST (or plain-text ntfy message for ntfy topic URLs)."""
    raw_url = str(url or "").strip()
    if not raw_url:
        return False, "Webhook URL is not configured."
    local_ok = _allow_local_webhooks() if allow_local is None else bool(allow_local)
    try:
        clean = lookout_extract.check_url(raw_url, allow_private=local_ok)
    except (ValueError, RuntimeError) as e:
        return False, str(e)

    try:
        t = max(0.5, min(float(timeout), 60.0))
    except (TypeError, ValueError):
        return False, "Invalid webhook timeout."

    if _is_ntfy_topic(clean):
        data, req_headers = _format_ntfy(payload)
    else:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        req_headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json, */*"}
    req_headers["User-Agent"] = "Lookout-Webhook/1.0"
    if headers:
        for k, v in headers.items():
            req_headers[str(k)] = _safe_header(v)

    req = urllib.request.Request(clean, data=data, method="POST", headers=req_headers)
    opener = urllib.request.build_opener(lookout_extract._SafeRedirectHandler(allow_private=local_ok))
    try:
        with opener.open(req, timeout=t) as resp:
            resp.read(_MAX_WEBHOOK_RESP)
            status = getattr(resp, "status", None) or resp.getcode() or 200
        return True, f"Webhook delivered (HTTP {status})."
    except ValueError as e:
        return False, str(e)
    except urllib.error.HTTPError as e:
        code, reason = e.code, e.reason
        try:
            e.close()
        except Exception:
            pass
        return False, f"Webhook returned HTTP {code}: {reason}"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return False, f"Webhook timed out after {t:g}s."
        return False, f"Webhook request failed: {reason}"
    except (TimeoutError, socket.timeout):
        return False, f"Webhook timed out after {t:g}s."
    except OSError as e:
        return False, f"Webhook request failed: {e}"


# ---------- daily schedule ----------
def _parse_hhmm(at: str) -> Tuple[int, int]:
    clean = str(at or "").strip()
    if not _TIME_RE.fullmatch(clean):
        raise ValueError("Time must be in HH:MM (24-hour) format.")
    hh, mm = clean.split(":", 1)
    return int(hh), int(mm)


def _systemd_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else (Path.home() / ".config")
    return root / "systemd" / "user"


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def cron_line(at: str = "08:30") -> str:
    hh, mm = _parse_hhmm(at)
    root = shlex.quote(str(db.ROOT))
    exe = shlex.quote(sys.executable)
    return f"{mm} {hh} * * * cd {root} && {exe} run.py scan --scheduled {CRON_MARKER}"


def systemd_units(at: str = "08:30") -> Tuple[str, str]:
    hh, mm = _parse_hhmm(at)
    exec_start = f"{shlex.quote(sys.executable)} {shlex.quote(str(db.ROOT / 'run.py'))} scan --scheduled"
    service = (
        "[Unit]\n"
        "Description=Lookout daily web monitor scan\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={db.ROOT}\n"
        f"ExecStart={exec_start}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run Lookout daily scan\n\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {hh:02d}:{mm:02d}:00\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def launchd_plist(at: str = "08:30") -> str:
    hh, mm = _parse_hhmm(at)
    data = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, str(db.ROOT / "run.py"), "scan", "--scheduled"],
        "WorkingDirectory": str(db.ROOT),
        "StartCalendarInterval": {"Hour": hh, "Minute": mm},
        "RunAtLoad": False,
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_XML).decode("utf-8")


def schedule_preview(at: str = "08:30") -> Dict[str, str]:
    svc, tmr = systemd_units(at)
    return {
        "cron": cron_line(at),
        "systemd_service": svc,
        "systemd_timer": tmr,
        "launchd_plist": launchd_plist(at),
    }


def _cron_hint(at: str = "08:30") -> str:
    try:
        line = cron_line(at)
    except ValueError:
        line = f"30 8 * * * cd {shlex.quote(str(db.ROOT))} && {shlex.quote(sys.executable)} run.py scan --scheduled {CRON_MARKER}"
    return f"No user scheduler detected. Add to crontab: {line}"


def _run_cmd(cmd: List[str], timeout: int = 15, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _systemd_user_ready() -> bool:
    exe = shutil.which("systemctl")
    if not exe:
        return False
    try:
        r = _run_cmd([exe, "--user", "show-environment"], timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _read_crontab() -> Tuple[bool, List[str], str]:
    exe = shutil.which("crontab")
    if not exe:
        return False, [], "crontab is not installed."
    try:
        r = _run_cmd([exe, "-l"], timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return False, [], str(e)
    out = r.stdout or ""
    err = (r.stderr or "").lower()
    if r.returncode != 0:
        if "no crontab" in err or "no crontab" in out.lower():
            return True, [], ""
        return False, [], (r.stderr or r.stdout or "crontab -l failed").strip()
    return True, out.splitlines(), ""


def _write_crontab(lines: List[str]) -> Tuple[bool, str]:
    exe = shutil.which("crontab")
    if not exe:
        return False, "crontab is not installed."
    payload = ("\n".join(lines).rstrip("\n") + "\n") if lines else ""
    try:
        r = _run_cmd([exe, "-"], timeout=10, input_text=payload)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Could not update crontab: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "crontab update failed").strip()
    return True, ""


def install_crontab(at: str) -> Tuple[bool, str]:
    try:
        line = cron_line(at)
    except ValueError as e:
        return False, str(e)
    ok, existing, err = _read_crontab()
    if not ok:
        return False, f"Could not read crontab: {err}"
    kept = [l for l in existing if CRON_MARKER not in l]
    kept.append(line)
    ok_w, err_w = _write_crontab(kept)
    if not ok_w:
        return False, err_w
    return True, f"Lookout will scan every day at {str(at).strip()} via crontab."


def remove_crontab() -> Tuple[bool, str]:
    ok, existing, err = _read_crontab()
    if not ok:
        return False, f"Could not read crontab: {err}"
    kept = [l for l in existing if CRON_MARKER not in l]
    if len(kept) == len(existing):
        return True, "Daily scan turned off."
    ok_w, err_w = _write_crontab(kept)
    if not ok_w:
        return False, err_w
    return True, "Daily scan removed from crontab."


def install_systemd_timer(at: str) -> Tuple[bool, str]:
    exe = shutil.which("systemctl")
    if not exe:
        return False, "systemctl is not installed."
    try:
        svc_txt, tmr_txt = systemd_units(at)
    except ValueError as e:
        return False, str(e)
    sdir = _systemd_dir()
    svc_path = sdir / f"{SYSTEMD_UNIT}.service"
    tmr_path = sdir / f"{SYSTEMD_UNIT}.timer"
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        svc_path.write_text(svc_txt, encoding="utf-8")
        tmr_path.write_text(tmr_txt, encoding="utf-8")
    except OSError as e:
        return False, f"Could not write systemd user unit: {e}"
    try:
        r1 = _run_cmd([exe, "--user", "daemon-reload"], timeout=15)
        if r1.returncode != 0:
            raise RuntimeError((r1.stderr or r1.stdout or "systemctl daemon-reload failed").strip())
        r2 = _run_cmd([exe, "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"], timeout=15)
        if r2.returncode != 0:
            raise RuntimeError((r2.stderr or r2.stdout or "systemctl enable failed").strip())
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        for p in (tmr_path, svc_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        return False, f"Could not enable systemd user timer: {e}"
    return True, f"Lookout will scan every day at {str(at).strip()} via systemd --user timer."


def remove_systemd_timer() -> Tuple[bool, str]:
    exe = shutil.which("systemctl")
    sdir = _systemd_dir()
    svc_path = sdir / f"{SYSTEMD_UNIT}.service"
    tmr_path = sdir / f"{SYSTEMD_UNIT}.timer"
    if exe:
        try:
            _run_cmd([exe, "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"], timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    for p in (tmr_path, svc_path):
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            return False, f"Could not remove {p.name}: {e}"
    if exe:
        try:
            _run_cmd([exe, "--user", "daemon-reload"], timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    return True, "Daily scan systemd timer removed."


def install_launchd(at: str) -> Tuple[bool, str]:
    exe = shutil.which("launchctl")
    if not exe:
        return False, "launchctl is not installed."
    try:
        plist_txt = launchd_plist(at)
    except ValueError as e:
        return False, str(e)
    plist_path = _launchd_path()
    try:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist_txt, encoding="utf-8")
    except OSError as e:
        return False, f"Could not write launchd plist: {e}"
    uid = getattr(os, "getuid", lambda: 0)()
    domain = f"gui/{uid}"
    try:
        _run_cmd([exe, "bootout", domain, str(plist_path)], timeout=15)
        r = _run_cmd([exe, "bootstrap", domain, str(plist_path)], timeout=15)
        if r.returncode != 0:
            r2 = _run_cmd([exe, "load", "-w", str(plist_path)], timeout=15)
            if r2.returncode != 0:
                msg = (r2.stderr or r2.stdout or r.stderr or r.stdout or "launchctl failed").strip()
                raise RuntimeError(msg)
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        try:
            plist_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"Could not load launchd agent: {e}"
    return True, f"Lookout will scan every day at {str(at).strip()} via launchd."


def remove_launchd() -> Tuple[bool, str]:
    exe = shutil.which("launchctl")
    plist_path = _launchd_path()
    uid = getattr(os, "getuid", lambda: 0)()
    if exe and plist_path.exists():
        try:
            r = _run_cmd([exe, "bootout", f"gui/{uid}", str(plist_path)], timeout=15)
            if r.returncode != 0:
                _run_cmd([exe, "unload", str(plist_path)], timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        plist_path.unlink(missing_ok=True)
    except OSError as e:
        return False, f"Could not remove launchd plist: {e}"
    return True, "Daily scan launchd agent removed."


def schedule_status(use_cache: bool = True) -> Dict:
    if (use_cache and _schedule_cache["plat"] == sys.platform
            and _schedule_cache["val"] is not None and time.monotonic() - _schedule_cache["t"] < 60):
        return dict(_schedule_cache["val"])
    sched_time = db.get_settings().get("schedule_time") or "08:30"
    if not _TIME_RE.fullmatch(str(sched_time).strip()):
        sched_time = "08:30"
    if sys.platform == "win32":
        try:
            r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            res = {"supported": True, "enabled": r.returncode == 0, "backend": "task_scheduler"}
        except (OSError, subprocess.SubprocessError):
            res = {"supported": True, "enabled": False, "backend": "task_scheduler"}
    elif sys.platform == "darwin":
        plist_path = _launchd_path()
        cron_ok, cron_lines, _ = _read_crontab()
        cron_enabled = cron_ok and any(CRON_MARKER in l for l in cron_lines)
        if plist_path.is_file():
            exe = shutil.which("launchctl")
            enabled = False
            if exe:
                try:
                    r = _run_cmd([exe, "list", LAUNCHD_LABEL], timeout=10)
                    enabled = r.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    enabled = False
            res = {"supported": True, "enabled": enabled, "backend": "launchd", "cron": cron_line(sched_time)}
        elif cron_enabled:
            res = {"supported": True, "enabled": True, "backend": "crontab", "cron": cron_line(sched_time)}
        elif shutil.which("launchctl"):
            res = {"supported": True, "enabled": False, "backend": "launchd", "cron": cron_line(sched_time)}
        elif shutil.which("crontab"):
            res = {"supported": True, "enabled": False, "backend": "crontab", "cron": cron_line(sched_time)}
        else:
            res = {"supported": False, "enabled": False, "backend": None, "cron": cron_line(sched_time),
                   "hint": _cron_hint(sched_time)}
    else:
        tmr_path = _systemd_dir() / f"{SYSTEMD_UNIT}.timer"
        cron_ok, cron_lines, _ = _read_crontab()
        cron_enabled = cron_ok and any(CRON_MARKER in l for l in cron_lines)
        if tmr_path.is_file():
            exe = shutil.which("systemctl")
            enabled = False
            if exe:
                try:
                    r = _run_cmd([exe, "--user", "is-enabled", f"{SYSTEMD_UNIT}.timer"], timeout=10)
                    enabled = r.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    enabled = False
            res = {"supported": True, "enabled": enabled, "backend": "systemd", "cron": cron_line(sched_time)}
        elif cron_enabled:
            res = {"supported": True, "enabled": True, "backend": "crontab", "cron": cron_line(sched_time)}
        elif _systemd_user_ready():
            res = {"supported": True, "enabled": False, "backend": "systemd", "cron": cron_line(sched_time)}
        elif shutil.which("crontab"):
            res = {"supported": True, "enabled": False, "backend": "crontab", "cron": cron_line(sched_time)}
        else:
            res = {"supported": False, "enabled": False, "backend": None, "cron": cron_line(sched_time),
                   "hint": _cron_hint(sched_time)}
    _schedule_cache.update(t=time.monotonic(), plat=sys.platform, val=dict(res))
    return res


def set_schedule(enabled: bool, at: str, backend: Optional[str] = None) -> Tuple[bool, str]:
    _schedule_cache["t"] = 0.0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
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

    if not enabled:
        if sys.platform == "darwin":
            ok1, msg1 = remove_launchd()
        else:
            ok1, msg1 = remove_systemd_timer()
        ok2, msg2 = (True, "")
        if shutil.which("crontab"):
            ok2, msg2 = remove_crontab()
        if not ok1:
            return False, msg1
        if not ok2:
            return False, msg2
        return True, "Daily scan turned off."

    try:
        _parse_hhmm(at)
    except ValueError as e:
        return False, str(e)

    if sys.platform == "darwin":
        if backend == "crontab":
            return install_crontab(at)
        ok, msg = install_launchd(at)
        if ok:
            if shutil.which("crontab"):
                remove_crontab()
            return True, msg
        if backend is None and shutil.which("crontab"):
            return install_crontab(at)
        return False, msg if shutil.which("launchctl") else _cron_hint(at)

    if backend == "crontab":
        return install_crontab(at)
    if _systemd_user_ready():
        ok, msg = install_systemd_timer(at)
        if ok:
            if shutil.which("crontab"):
                remove_crontab()
            return True, msg
        if backend is None and shutil.which("crontab"):
            return install_crontab(at)
        return False, msg
    if shutil.which("crontab"):
        return install_crontab(at)
    return False, _cron_hint(at)
