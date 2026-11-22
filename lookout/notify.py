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
WEBHOOK_FORMATS = ("json", "slack", "discord", "telegram", "ntfy")
_DEFAULT_USER_AGENT = "Lookout/0.3.0 (+https://github.com/1eminguler/lookout)"
_JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json, */*"}
_HEADER_INJECT_RE = re.compile(r"^[a-zA-Z0-9-]+\s*:")
_PS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument; $x.LoadXml($env:LOOKOUT_TOAST)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:LOOKOUT_APPID).Show((New-Object Windows.UI.Notifications.ToastNotification $x))
"""


def _as_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def _decode_stream(stream: Any) -> str:
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace").strip()
    if isinstance(stream, str):
        return stream.strip()
    return ""


def bare_panel_url(url: Optional[str] = None) -> str:
    """Strip any one-time launch secret (?k=...) so e-mail links remain a bare panel URL."""
    raw = str(url or "").strip()
    if not raw:
        port = int(os.environ.get("LOOKOUT_PORT") or 8780)
        return f"http://127.0.0.1:{port}/"
    parts = urllib.parse.urlsplit(raw)
    if not parts.query:
        return raw
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "k"]
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path or "/", urllib.parse.urlencode(kept), parts.fragment
    ))


def desktop_launch_url(url: Optional[str] = None) -> str:
    """Resolve a bare localhost URL to include the current one-time launch secret (?k=...) when available."""
    raw = str(url or "").strip()
    if not raw:
        port = int(os.environ.get("LOOKOUT_PORT") or 8780)
        raw = f"http://127.0.0.1:{port}/"
    parts = urllib.parse.urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host in ("127.0.0.1", "localhost") and not parts.query:
        sec_file = db.DB_PATH.parent / ".launch_secret"
        if sec_file.is_file():
            try:
                sec = sec_file.read_text(encoding="utf-8", errors="replace").strip()
                if sec:
                    return urllib.parse.urlunsplit((
                        parts.scheme or "http",
                        parts.netloc,
                        parts.path or "/",
                        urllib.parse.urlencode([("k", sec)]),
                        parts.fragment,
                    ))
            except OSError:
                pass
    return raw


def desktop(title: str, lines: List[str], url: str) -> Tuple[bool, str]:
    """Send an OS desktop notification and return (ok, status_message)."""
    lines = [str(l) for l in (lines or [])][:2]
    launch_url = desktop_launch_url(url)
    try:
        if sys.platform == "win32":
            ps_exe = shutil.which("powershell.exe")
            if not ps_exe:
                return False, "Desktop notification tool is not available."
            body = "".join(f"<text>{xesc(t)}</text>" for t in [str(title), *lines])
            xml = (f'<toast activationType="protocol" launch="{xesc(str(launch_url))}"><visual><binding template="ToastGeneric">'
                   f'{body}</binding></visual></toast>')
            env = dict(os.environ, LOOKOUT_TOAST=xml,
                       LOOKOUT_APPID=r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe")
            r = subprocess.run([ps_exe, "-NoProfile", "-NonInteractive", "-Command", _PS_TOAST], env=env,
                               timeout=30, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif sys.platform == "darwin":
            osa = shutil.which("osascript")
            if not osa:
                return False, "Desktop notification tool is not available."
            msg = _as_escape(" — ".join(lines))
            t = _as_escape(title)
            r = subprocess.run([osa, "-e", f'display notification "{msg}" with title "{t}"'],
                               timeout=15, capture_output=True)
        else:
            ns = shutil.which("notify-send")
            if not ns:
                return False, "Desktop notification tool is not available."
            r = subprocess.run([ns, str(title), "\n".join(lines)], timeout=15, capture_output=True)

        rc = getattr(r, "returncode", None)
        if isinstance(rc, int) and not isinstance(rc, bool) and rc != 0:
            err = _decode_stream(getattr(r, "stderr", None)) or _decode_stream(getattr(r, "stdout", None))
            return False, f"Desktop notification failed: {err or f'exit code {rc}'}"
        return True, "Desktop notification sent."
    except Exception as e:
        return False, f"Desktop notification failed: {e}"


def _item_meta(item: Dict[str, Any], *, deadline_prefix: bool = False) -> str:
    dl = item.get("deadline")
    dl_text = f"deadline {dl}" if (dl and deadline_prefix) else dl
    parts = [item.get("org"), item.get("source_name") or item.get("source"), dl_text]
    return " · ".join(str(x) for x in parts if x)


def _mail_html_section(heading: str, rows_items: List[Dict[str, Any]]) -> str:
    rows = []
    for item in rows_items[:40]:
        meta = _item_meta(item, deadline_prefix=True)
        meta_html = (
            f'<br><span style="color:#666;font-size:13px">{escape(meta, quote=True)}</span>'
            if meta else ""
        )
        chg = item.get("change_summary")
        chg_html = (
            f'<br><span style="color:#444;font-size:13px">{escape(str(chg), quote=True)}</span>'
            if chg else ""
        )
        rows.append(
            '<tr><td style="padding:10px 0;border-bottom:1px solid #eee">'
            f'<a href="{escape(str(item.get("url", "")), quote=True)}" '
            f'style="color:#111;font-weight:600;text-decoration:none">{escape(str(item.get("title", "")), quote=True)}</a>'
            f'{meta_html}{chg_html}</td></tr>'
        )
    return f'<h2 style="margin:14px 0 6px">{escape(heading, quote=True)}</h2><table width="100%">{"".join(rows)}</table>'


def _mail_text_section(heading: str, rows_items: List[Dict[str, Any]]) -> str:
    lines = [f"{heading}:"]
    for item in rows_items:
        line = f"- {item.get('title', '')} {item.get('url', '')}".rstrip()
        if item.get("change_summary"):
            line += f" [{item['change_summary']}]"
        lines.append(line)
    return "\n".join(lines)


def mail_digest(
    items: List[Dict],
    panel_url: str,
    *,
    changed_items: Optional[List[Dict]] = None,
) -> Tuple[bool, str]:
    """Send an HTML + plain-text e-mail digest for new and updated items."""
    panel_url = bare_panel_url(panel_url)
    s = db.get_settings()
    raw_from = str(s.get("mail_from") or s.get("smtp_from") or s.get("smtp_user") or "")
    if "\r" in raw_from or "\n" in raw_from:
        return False, "Invalid e-mail address."
    from_addr = raw_from.strip()

    raw_to = str(s.get("mail_to") or "")
    recipients: List[str] = []
    for part in re.split(r"[,;\n]+", raw_to):
        if "\r" in part or "\n" in part:
            return False, "Invalid e-mail address."
        addr = part.strip()
        if not addr:
            continue
        if _HEADER_INJECT_RE.match(addr):
            return False, "Invalid e-mail address."
        recipients.append(addr)

    smtp_user = (s.get("smtp_user") or "").strip()
    smtp_password = s.get("smtp_password") or ""
    if not recipients or not from_addr or (smtp_user and not smtp_password):
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

    if changed_items is None:
        raw_items = list(items or [])
        upd_list = [i for i in raw_items if i.get("is_updated") and not i.get("is_new")]
        new_list = [i for i in raw_items if not (i.get("is_updated") and not i.get("is_new"))]
    else:
        new_list = list(items or [])
        upd_list = list(changed_items or [])

    if new_list and upd_list:
        subject = f"Lookout: {len(new_list)} new, {len(upd_list)} updated"
    elif new_list:
        subject = f"Lookout: {len(new_list)} new"
    elif upd_list:
        subject = f"Lookout: {len(upd_list)} updated"
    else:
        subject = "Lookout: digest"

    html_parts: List[str] = []
    text_parts: List[str] = []
    if new_list:
        h_new = f"{len(new_list)} new item{'s' if len(new_list) != 1 else ''}"
        html_parts.append(_mail_html_section(h_new, new_list))
        text_parts.append(_mail_text_section(f"New items ({len(new_list)})", new_list))
    if upd_list:
        h_upd = f"{len(upd_list)} updated item{'s' if len(upd_list) != 1 else ''}"
        html_parts.append(_mail_html_section(h_upd, upd_list))
        text_parts.append(_mail_text_section(f"Updated items ({len(upd_list)})", upd_list))
    if not html_parts:
        html_parts.append('<h2 style="margin:0 0 6px">Lookout digest</h2><p style="color:#666">No new or updated items.</p>')
        text_parts.append("No new or updated items.")

    html = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;max-width:620px">'
        f'{"".join(html_parts)}'
        f'<p><a href="{escape(str(panel_url), quote=True)}">Open Lookout</a> (works on the computer running Lookout)</p>'
        '</div>'
    )
    plain = "\n\n".join(text_parts) + f"\n\nOpen Lookout: {panel_url}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    starttls_mode = (s.get("smtp_starttls") or "auto").strip().lower()
    use_auth = bool(smtp_user and smtp_password)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=25, context=ctx) as srv:
                if use_auth:
                    srv.login(smtp_user, smtp_password)
                srv.sendmail(from_addr, recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=25) as srv:
                srv.ehlo()
                if starttls_mode in ("0", "false", "no", "never", "off"):
                    pass
                elif starttls_mode in ("1", "true", "yes", "always", "require"):
                    if not srv.has_extn("starttls"):
                        return False, f"SMTP server {host}:{port} does not offer STARTTLS."
                    srv.starttls(context=ctx)
                    srv.ehlo()
                else:
                    if srv.has_extn("starttls"):
                        srv.starttls(context=ctx)
                        srv.ehlo()
                if use_auth:
                    srv.login(smtp_user, smtp_password)
                srv.sendmail(from_addr, recipients, msg.as_string())
        return True, f"Sent to {', '.join(recipients)}"
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


# ---------- webhooks (JSON POST, Slack, Discord, Telegram, ntfy) ----------
def _allow_local_webhooks() -> bool:
    s = db.get_settings()
    for key in ("allow_local_webhooks", "webhook_allow_local", "allow_local_webhook"):
        if str(s.get(key) or "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


def _is_ntfy_topic(url: str) -> bool:
    p = urllib.parse.urlparse(str(url or "").strip())
    host = (p.hostname or "").lower()
    path = p.path.strip("/")
    if not path or "/" in path:
        return False
    return host == "ntfy.sh" or host.endswith(".ntfy.sh") or host.startswith("ntfy.")


def detect_webhook_format(url: str) -> str:
    """Detect webhook payload format from destination URL."""
    clean = str(url or "").strip()
    if not clean:
        return "json"
    if _is_ntfy_topic(clean):
        return "ntfy"
    p = urllib.parse.urlparse(clean)
    host = (p.hostname or "").lower()
    path = (p.path or "").lower()
    if host in ("hooks.slack.com", "hooks.slack-gov.com"):
        return "slack"
    if (host in ("discord.com", "discordapp.com") or host.endswith((".discord.com", ".discordapp.com"))) and "/api/webhooks/" in path:
        return "discord"
    if host == "api.telegram.org" and "/sendmessage" in path:
        return "telegram"
    return "json"


def _safe_header(value: Any) -> str:
    txt = str(value or "").replace("\r", " ").replace("\n", " ").replace("—", "-").replace("–", "-").strip()
    return txt.encode("latin-1", "replace").decode("latin-1")


def _slack_escape(text: Any) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _extract_webhook_parts(payload: Any) -> Tuple[str, str, str, List[Any]]:
    if isinstance(payload, dict):
        title = str(payload.get("title") or payload.get("subject") or "").strip()
        click = payload.get("click") or payload.get("url") or payload.get("panel_url")
        link_url = click.strip() if isinstance(click, str) and click.strip().startswith(("http://", "https://")) else ""
        body_raw = payload.get("message") or payload.get("text") or payload.get("body")
        body_text = str(body_raw).strip() if body_raw else ""
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        return title, link_url, body_text, items
    if isinstance(payload, list):
        return "Lookout update", "", "", payload
    return "Lookout notification", "", str(payload or "Lookout notification"), []


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


def _format_slack(payload: Any) -> Tuple[bytes, Dict[str, str]]:
    title, link_url, body_text, items = _extract_webhook_parts(payload)
    header_text = (title or "Lookout notification")[:150]
    mrkdwn_lines: List[str] = []
    plain_lines: List[str] = [header_text]
    if items:
        for item in items[:10]:
            if isinstance(item, dict):
                it_title = str(item.get("title") or "Untitled")
                it_url = str(item.get("url") or "").strip()
                safe_label = _slack_escape(it_title).replace("|", " ")
                if it_url.startswith(("http://", "https://")):
                    safe_url = it_url.replace("<", "%3C").replace(">", "%3E")
                    entry = f"• <{safe_url}|{safe_label}>"
                else:
                    entry = f"• {safe_label}"
                meta = _slack_escape(_item_meta(item))
                if meta:
                    entry += f" ({meta})"
                if item.get("change_summary"):
                    entry += f" — _{_slack_escape(item['change_summary'])}_"
                mrkdwn_lines.append(entry)
                plain_lines.append(f"- {it_title}" + (f" ({it_url})" if it_url else ""))
            else:
                mrkdwn_lines.append(f"• {_slack_escape(item)}")
                plain_lines.append(f"- {item}")
        if len(items) > 10:
            more = f"...and {len(items) - 10} more"
            mrkdwn_lines.append(more)
            plain_lines.append(more)
    elif body_text:
        mrkdwn_lines.append(_slack_escape(body_text))
        plain_lines.append(body_text)

    if link_url:
        safe_link = link_url.replace("<", "%3C").replace(">", "%3E")
        mrkdwn_lines.append(f"<{safe_link}|Open Lookout>")
        plain_lines.append(link_url)

    section_text = ("\n".join(mrkdwn_lines).strip() or _slack_escape(header_text))[:3000]
    fallback_text = ("\n".join(plain_lines).strip() or header_text)[:3500]
    body = {
        "text": fallback_text,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": header_text}},
            {"type": "section", "text": {"type": "mrkdwn", "text": section_text}},
        ],
    }
    return json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"), dict(_JSON_HEADERS)


def _format_discord(payload: Any) -> Tuple[bytes, Dict[str, str]]:
    title, link_url, body_text, items = _extract_webhook_parts(payload)
    embed_title = (title or "Lookout notification")[:256]
    desc_lines: List[str] = []
    if items:
        for item in items[:10]:
            if isinstance(item, dict):
                it_title = str(item.get("title") or "Untitled")
                it_url = str(item.get("url") or "").strip()
                safe_title = it_title.replace("[", "\\[").replace("]", "\\]")
                line = f"- [{safe_title}]({it_url})" if it_url.startswith(("http://", "https://")) else f"- {safe_title}"
                meta = _item_meta(item)
                if meta:
                    line += f" ({meta})"
                if item.get("change_summary"):
                    line += f" — {item['change_summary']}"
                desc_lines.append(line)
            else:
                desc_lines.append(f"- {item}")
        if len(items) > 10:
            desc_lines.append(f"...and {len(items) - 10} more")
    elif body_text:
        desc_lines.append(body_text)

    embed: Dict[str, Any] = {
        "title": embed_title,
        "description": ("\n".join(desc_lines).strip() or embed_title)[:4096],
    }
    if link_url:
        embed["url"] = link_url
    summary = embed_title if items or not body_text else f"{embed_title} — {body_text}"
    body = {"content": summary[:2000], "embeds": [embed]}
    return json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"), dict(_JSON_HEADERS)


def _format_telegram(payload: Any, url: str) -> Tuple[bytes, Dict[str, str]]:
    title, link_url, body_text, items = _extract_webhook_parts(payload)
    lines: List[str] = [title] if title else []
    if items:
        for item in items[:10]:
            if isinstance(item, dict):
                it_title = str(item.get("title") or "Untitled")
                src = item.get("source_name") or item.get("source")
                line = f"- {it_title}" + (f" ({src})" if src else "")
                if item.get("change_summary"):
                    line += f" [{item['change_summary']}]"
                if item.get("url"):
                    line += f" — {item['url']}"
                lines.append(line)
            else:
                lines.append(f"- {item}")
        if len(items) > 10:
            lines.append(f"...and {len(items) - 10} more")
    elif body_text:
        lines.append(body_text)
    if link_url:
        lines.append(link_url)

    text = ("\n".join(lines).strip() or "Lookout notification")[:4096]
    body: Dict[str, Any] = {"text": text, "disable_web_page_preview": True}

    chat_id: Any = None
    if isinstance(payload, dict) and payload.get("chat_id") not in (None, ""):
        raw_cid = payload.get("chat_id")
        if isinstance(raw_cid, int) and not isinstance(raw_cid, bool):
            chat_id = raw_cid
        elif str(raw_cid).strip():
            chat_id = str(raw_cid).strip()
    if chat_id is None:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        q_vals = [v.strip() for v in qs.get("chat_id", []) if v.strip()]
        if q_vals:
            chat_id = q_vals[0]
    if chat_id is None:
        cfg_cid = str(db.get_settings().get("telegram_chat_id") or "").strip()
        if cfg_cid:
            chat_id = cfg_cid
    if chat_id is not None:
        body["chat_id"] = chat_id

    return json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"), dict(_JSON_HEADERS)


def webhook(
    url: str,
    payload: Any,
    fmt: Optional[str] = None,
    *,
    timeout: float = 15.0,
    allow_local: Optional[bool] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """Deliver a notification payload formatted as JSON, Slack, Discord, Telegram or ntfy."""
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

    fmt_norm = ("" if fmt is None else str(fmt)).strip().lower()
    if fmt_norm in ("", "auto"):
        detected = detect_webhook_format(clean)
        if detected != "json":
            fmt_norm = detected
        else:
            cfg_fmt = str(db.get_settings().get("webhook_format") or "").strip().lower()
            fmt_norm = cfg_fmt if cfg_fmt in WEBHOOK_FORMATS else "json"
    if fmt_norm not in WEBHOOK_FORMATS:
        return False, f"Unsupported webhook format: {fmt}"

    if fmt_norm == "ntfy":
        data, req_headers = _format_ntfy(payload)
    elif fmt_norm == "slack":
        data, req_headers = _format_slack(payload)
    elif fmt_norm == "discord":
        data, req_headers = _format_discord(payload)
    elif fmt_norm == "telegram":
        data, req_headers = _format_telegram(payload, clean)
    else:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        req_headers = dict(_JSON_HEADERS)

    ua_fn = getattr(lookout_extract, "get_user_agent", None)
    req_headers["User-Agent"] = ua_fn() if callable(ua_fn) else _DEFAULT_USER_AGENT
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
        snippet = ""
        try:
            raw_err = e.read(200)
            if raw_err:
                snippet = " ".join(raw_err.decode("utf-8", "replace").split())[:200]
        except Exception:
            pass
        try:
            e.close()
        except Exception:
            pass
        msg = f"Webhook returned HTTP {code}: {reason}"
        if snippet:
            msg += f" ({snippet})"
        return False, msg
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


def _schedule_env() -> Dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH") or os.defpath,
        "LOOKOUT_DATA_DIR": os.environ.get("LOOKOUT_DATA_DIR") or str(db.DB_PATH.parent),
    }
    plugin_dir = os.environ.get("LOOKOUT_PLUGIN_DIR")
    if plugin_dir and plugin_dir.strip():
        env["LOOKOUT_PLUGIN_DIR"] = plugin_dir
    return env


def _systemd_env_escape(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def _extract_cron_command(line: str) -> str:
    txt = str(line or "").strip()
    if CRON_MARKER in txt:
        txt = txt.split(CRON_MARKER, 1)[0].strip()
    parts = txt.split(None, 5)
    return parts[5].strip() if len(parts) == 6 else txt


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
    env_part = " ".join(f"{k}={shlex.quote(v)}" for k, v in _schedule_env().items())
    return f"{mm} {hh} * * * cd {root} && env {env_part} {exe} run.py scan --scheduled {CRON_MARKER}"


def systemd_units(at: str = "08:30") -> Tuple[str, str]:
    hh, mm = _parse_hhmm(at)
    exec_start = f"{shlex.quote(sys.executable)} {shlex.quote(str(db.ROOT / 'run.py'))} scan --scheduled"
    env_lines = "".join(f'Environment="{k}={_systemd_env_escape(v)}"\n' for k, v in _schedule_env().items())
    service = (
        "[Unit]\n"
        "Description=Lookout daily web monitor scan\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={db.ROOT}\n"
        f"{env_lines}"
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
        "EnvironmentVariables": _schedule_env(),
        "StartCalendarInterval": {"Hour": hh, "Minute": mm},
        "RunAtLoad": False,
    }
    return plistlib.dumps(data, fmt=plistlib.FMT_XML).decode("utf-8")


def schedule_test_command(backend: Optional[str] = None) -> str:
    """Return a shell command that exercises the scheduled scan immediately."""
    active_backend = str(backend).strip().lower() if backend else None
    if not active_backend:
        active_backend = schedule_status(use_cache=False).get("backend")
    if not active_backend:
        if sys.platform == "win32":
            active_backend = "task_scheduler"
        elif sys.platform == "darwin":
            active_backend = "launchd"
        else:
            active_backend = "crontab"

    if active_backend == "task_scheduler":
        return f"schtasks /Run /TN {TASK_NAME}"
    if active_backend == "systemd":
        return f"systemctl --user start {SYSTEMD_UNIT}.service"
    if active_backend == "launchd":
        uid = getattr(os, "getuid", lambda: 0)()
        return f"launchctl kickstart -k gui/{uid}/{LAUNCHD_LABEL}"
    home = shlex.quote(str(Path.home()))
    cmd = _extract_cron_command(cron_line())
    return f"env -i HOME={home} PATH=/usr/bin:/bin /bin/sh -c {shlex.quote(cmd)}"


def schedule_preview(at: str = "08:30") -> Dict[str, str]:
    svc, tmr = systemd_units(at)
    return {
        "cron": cron_line(at),
        "systemd_service": svc,
        "systemd_timer": tmr,
        "launchd_plist": launchd_plist(at),
        "test_command": schedule_test_command(),
    }


def test_schedule_env(env: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """Verify captured scheduler environment includes PATH, LOOKOUT_DATA_DIR and Python."""
    captured = dict(_schedule_env() if env is None else env)
    path_val = str(captured.get("PATH") or "").strip()
    data_dir = str(captured.get("LOOKOUT_DATA_DIR") or "").strip()
    if not path_val:
        return False, "Schedule environment is missing PATH."
    if not data_dir:
        return False, "Schedule environment is missing LOOKOUT_DATA_DIR."
    py = (
        shutil.which("python3", path=path_val)
        or shutil.which("python", path=path_val)
        or (sys.executable if sys.executable and os.path.exists(sys.executable) else None)
    )
    if not py:
        return False, "No Python executable found in schedule PATH."
    return True, f"Schedule environment ready (LOOKOUT_DATA_DIR={data_dir}, python={py})."


def run_schedule_now(backend: Optional[str] = None, timeout: int = 60, wait: bool = False) -> Tuple[bool, str]:
    """Trigger the configured daily scan immediately via the active OS scheduler backend."""
    if backend is None:
        st = schedule_status(use_cache=False)
        if not st.get("enabled"):
            return False, "Daily scan is not enabled."
        active_backend = st.get("backend")
    else:
        active_backend = str(backend).strip().lower()

    if not active_backend:
        return False, "Daily scan is not enabled."

    if active_backend == "task_scheduler":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            r = subprocess.run(
                ["schtasks", "/Run", "/TN", TASK_NAME],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=flags,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"Could not run scheduled task: {e}"
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or f"exit code {r.returncode}").strip()
            return False, f"Could not run scheduled task: {detail}"
        return True, "Triggered scheduled scan via Task Scheduler."

    if active_backend == "systemd":
        exe = shutil.which("systemctl")
        if not exe:
            return False, "systemctl is not installed."
        cmd = [exe, "--user", "start", *([] if wait else ["--no-block"]), f"{SYSTEMD_UNIT}.service"]
        try:
            r = _run_cmd(cmd, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"Could not run systemd service: {e}"
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or f"exit code {r.returncode}").strip()
            return False, f"Could not run systemd service: {detail}"
        return True, ("Scheduled scan finished via systemd." if wait else "Started scheduled scan via systemd.")

    if active_backend == "launchd":
        exe = shutil.which("launchctl")
        if not exe:
            return False, "launchctl is not installed."
        uid = getattr(os, "getuid", lambda: 0)()
        try:
            r = _run_cmd([exe, "kickstart", f"gui/{uid}/{LAUNCHD_LABEL}"], timeout=timeout)
            if r.returncode != 0:
                r2 = _run_cmd([exe, "start", LAUNCHD_LABEL], timeout=timeout)
                if r2.returncode != 0:
                    detail = (r2.stderr or r2.stdout or r.stderr or r.stdout or f"exit code {r2.returncode}").strip()
                    return False, f"Could not run launchd agent: {detail}"
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"Could not run launchd agent: {e}"
        return True, "Triggered scheduled scan via launchd."

    if active_backend == "crontab":
        cmd = ""
        ok_c, lines, _ = _read_crontab()
        if ok_c:
            for line in lines:
                if CRON_MARKER in line and not line.lstrip().startswith("#"):
                    cmd = _extract_cron_command(line)
                    break
        if not cmd:
            cmd = _extract_cron_command(cron_line())
        cron_env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "SHELL": "/bin/sh"}
        if wait:
            try:
                r = subprocess.run(
                    ["/bin/sh", "-c", cmd],
                    env=cron_env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
            except (OSError, subprocess.SubprocessError) as e:
                return False, f"Could not run scheduled scan via crontab: {e}"
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or f"exit code {r.returncode}").strip()
                return False, f"Scheduled scan failed via crontab: {detail}"
            return True, "Scheduled scan finished via crontab."
        try:
            popen_kw: Dict[str, Any] = {
                "env": cron_env,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform != "win32":
                popen_kw["start_new_session"] = True
            subprocess.Popen(["/bin/sh", "-c", cmd], **popen_kw)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"Could not start scheduled scan via crontab: {e}"
        return True, "Started scheduled scan via crontab."

    return False, f"Unsupported schedule backend: {active_backend}"


def _cron_hint(at: str = "08:30") -> str:
    try:
        line = cron_line(at)
    except ValueError:
        line = cron_line("08:30")
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
