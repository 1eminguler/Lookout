"""Local web UI + JSON API. Listens on 127.0.0.1 only."""
import csv
import hmac
import html as _html
import inspect
import io as _io
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import date, datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape as _xesc

from lookout import agents, db, notify, scanner, tasks

WEB = db.ROOT / "web"
PORT = int(os.environ.get("LOOKOUT_PORT") or 8780)
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
SECRET_KEYS = {"smtp_password"}
API_TOKEN = secrets.token_urlsafe(32)
DEFAULT_ITEMS_LIMIT = 200
MAX_ITEMS_LIMIT = 500
discovery: Dict[str, Any] = {"id": None, "watch_id": None, "running": False, "request": "", "title": "",
                             "results": [], "error": "", "finished": None}
_disc_lock = threading.Lock()
_disc_cancel = threading.Event()
last_hit = {"t": time.time()}
_sched_cache: Dict[str, Any] = {"t": 0.0, "key": None, "val": None}


def _cached_schedule_status() -> Dict[str, Any]:
    key = (sys.platform, notify.schedule_status)
    now = time.monotonic()
    if _sched_cache["val"] is not None and _sched_cache["key"] == key and now - _sched_cache["t"] < 30:
        return dict(_sched_cache["val"])
    val = notify.schedule_status()
    _sched_cache.update(t=now, key=key, val=dict(val))
    return val


def public_settings() -> Dict[str, Any]:
    s = db.get_settings()
    out = {k: v for k, v in s.items() if k not in SECRET_KEYS}
    out["smtp_password_set"] = bool(s.get("smtp_password"))
    return out


def state(limit: int = DEFAULT_ITEMS_LIMIT, offset: int = 0) -> Dict[str, Any]:
    lim = max(1, min(MAX_ITEMS_LIMIT, int(limit)))
    off = max(0, int(offset))
    total = db.items_count()
    page_items = db.items(limit=lim, offset=off)
    return {
        "items": page_items,
        "items_total": total,
        "items_limit": lim,
        "items_offset": off,
        "items_has_more": (off + len(page_items)) < total,
        "agenda_items": db.agenda_items(),
        "scans": db.scans(),
        "discoveries": db.discoveries(),
        "watches": db.watches(),
        "sources": db.sources(),
        "filters": db.filters(),
        "progress": scanner.get_progress(),
        "settings": public_settings(),
        "discovery": dict(discovery),
        "schedule": _cached_schedule_status(),
    }


def cancel_discovery(async_browser: bool = False) -> None:
    with _disc_lock:
        if not discovery["running"]:
            return
        _disc_cancel.set()
    agents.cancel("discover", async_browser=async_browser)


def run_discovery(did: int, wid: Optional[int], request: str) -> None:
    results: List[Dict[str, Any]] = []
    error, status, title = "", "done", None
    added_ids: List[int] = []
    scanned_ids: set = set()
    sync_lock = threading.Lock()
    disc_token = uuid.uuid4().hex
    have_f = {(f["kind"], f["value"].strip().lower()) for f in db.filters(watch_id=wid)} if wid is not None else set()
    have_u = {s["url"].rstrip("/").lower(): s["id"] for s in db.sources(watch_id=wid)} if wid is not None else {}

    def _apply_partial(partial: Dict[str, Any], start_early: bool) -> None:
        nonlocal results, title
        p_sources = partial.get("sources") or []
        p_filters = partial.get("filters") or []
        p_title = partial.get("title")
        new_sids: List[int] = []
        with sync_lock:
            results = list(p_sources)
            if p_title:
                title = p_title
            with _disc_lock:
                if discovery.get("id") == did:
                    discovery["results"] = list(p_sources)
                    if p_title:
                        discovery["title"] = p_title
            if wid is not None and db.watch_detail(wid):
                if p_title:
                    db.update_watch(wid, title=p_title)
                for f in p_filters:
                    key = (f["kind"], f["value"].strip().lower())
                    if key not in have_f:
                        try:
                            db.add_filter(f["kind"], f["value"], watch_id=wid)
                            have_f.add(key)
                        except ValueError:
                            pass
                for c in p_sources:
                    u = c["url"].rstrip("/").lower()
                    if u not in have_u:
                        sid = db.add_source(c["name"], c["url"], c.get("notes") or "", watch_id=wid)
                        have_u[u] = sid
                        added_ids.append(sid)
                        new_sids.append(sid)
        if start_early and new_sids and wid is not None and not _disc_cancel.is_set():
            if scanner.enqueue_scan_sources(new_sids, watch_id=wid):
                scanned_ids.update(new_sids)
            elif scanner.start_scan_async("manual", only=new_sids, watch_id=wid):
                scanned_ids.update(new_sids)

    try:
        if db.acquire_scan_lock(disc_token, name="discover"):
            db.update_scan_lock(disc_token, scan_id=did, name="discover")
        kw: Dict[str, Any] = {"cancel_event": _disc_cancel, "session": f"lookout-discover-{did}"}
        sig_params = inspect.signature(tasks.discover_sources).parameters
        if "on_progress" in sig_params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values()):
            kw["on_progress"] = lambda p: _apply_partial(p, start_early=True)
        out = tasks.discover_sources(request, **kw)
        if _disc_cancel.is_set():
            status = "cancelled"
        else:
            _apply_partial(out, start_early=False)
    except agents.CancelledError:
        status, error = "cancelled", ""
    except Exception as e:
        if _disc_cancel.is_set():
            status, error = "cancelled", ""
        else:
            status, error = "failed", str(e)[:400]
    finally:
        db.release_scan_lock(disc_token, name="discover")
        db.finish_discovery(did, status, results, error, title=title)
        remaining = [sid for sid in added_ids if sid not in scanned_ids]
        if status == "done" and remaining and wid is not None and db.watch_detail(wid):
            if not scanned_ids:
                scanner.start_scan_async("manual", only=remaining, watch_id=wid)
            elif not scanner.enqueue_scan_sources(remaining, watch_id=wid):
                scanner.start_scan_async("manual", only=remaining, watch_id=wid)
        with _disc_lock:
            if discovery.get("id") == did:
                discovery.update(id=did, watch_id=wid, running=False, results=results, error=error,
                                 finished=db.now(), title=title or discovery.get("title", ""))


def _csv_cell(v: Any) -> str:
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _ics_fold(line: str) -> str:
    if len(line.encode("utf-8")) <= 75:
        return line
    parts: List[str] = []
    cur = bytearray()
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 75:
            parts.append(cur.decode("utf-8"))
            cur = bytearray(b" " + b)
        else:
            cur.extend(b)
    if cur:
        parts.append(cur.decode("utf-8"))
    return "\r\n".join(parts)


def agenda_ics() -> str:
    """Saved items as all-day calendar events: one for the deadline, one for the user's reminder."""
    def text(v):
        s = str(v or "").replace("\r\n", "\n").replace("\r", "\n")
        return s.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Lookout//Agenda//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:Lookout"]
    for i in db.agenda_items():
        for kind, day in (("Deadline", i["deadline"]), ("Reminder", i["remind_on"])):
            if not day:
                continue
            d = day.replace("-", "")
            clean_url = str(i["url"] or "").replace("\r", "").replace("\n", "")
            lines += [
                "BEGIN:VEVENT",
                f"UID:{i['id']}-{kind.lower()}@lookout",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{d}",
                f"SUMMARY:{text(kind + ': ' + i['title'])}",
                f"URL:{text(clean_url)}",
                f"DESCRIPTION:{text(' - '.join(x for x in (i['org'], i['source_name'], i['note']) if x))}",
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_ics_fold(ln) for ln in lines) + "\r\n"


def _rfc2822_ts(ts: str) -> str:
    try:
        dt = datetime.strptime(ts, db.TS).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        dt = datetime.now(timezone.utc)
    return format_datetime(dt, usegmt=True)


def _rfc3339_ts(ts: str) -> str:
    try:
        dt = datetime.strptime(ts, db.TS).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _watch_rss(wid: int, base_url: str) -> Optional[str]:
    w = db.watch_detail(wid)
    if not w:
        return None
    items = db.watch_feed_items(wid, limit=100)
    chan_link = f"{base_url}/#/watch/{wid}"
    entries = []
    for it in items:
        desc_parts = [x for x in (
            it.get("org"),
            it.get("location"),
            f"Deadline: {it['deadline']}" if it.get("deadline") else None,
            it.get("change_summary"),
            it.get("summary"),
        ) if x]
        entries.append(
            "<item>"
            f"<title>{_xesc(it['title'])}</title>"
            f"<link>{_xesc(it['url'])}</link>"
            f"<guid isPermaLink=\"false\">{_xesc(it['id'])}</guid>"
            f"<pubDate>{_xesc(_rfc2822_ts(it.get('updated_at') or it['first_seen']))}</pubDate>"
            f"<description>{_xesc(' · '.join(desc_parts))}</description>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<rss version="2.0"><channel>'
        f"<title>{_xesc('Lookout — ' + w['title'])}</title>"
        f"<link>{_xesc(chan_link)}</link>"
        f"<description>{_xesc(w.get('request') or w['title'])}</description>"
        + "".join(entries)
        + "</channel></rss>\n"
    )


def _watch_atom(wid: int, base_url: str) -> Optional[str]:
    w = db.watch_detail(wid)
    if not w:
        return None
    items = db.watch_feed_items(wid, limit=100)
    updated = _rfc3339_ts(
        max((it.get("updated_at") or it["first_seen"] for it in items), default=db.now())
    )
    entries = []
    for it in items:
        desc_parts = [x for x in (
            it.get("org"),
            it.get("location"),
            f"Deadline: {it['deadline']}" if it.get("deadline") else None,
            it.get("change_summary"),
            it.get("summary"),
        ) if x]
        entries.append(
            "<entry>"
            f"<id>urn:lookout:item:{_xesc(it['id'])}</id>"
            f"<title>{_xesc(it['title'])}</title>"
            f"<link href=\"{_xesc(it['url'])}\"/>"
            f"<updated>{_xesc(_rfc3339_ts(it.get('updated_at') or it['first_seen']))}</updated>"
            f"<summary>{_xesc(' · '.join(desc_parts))}</summary>"
            "</entry>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<id>urn:lookout:watch:{wid}</id>"
        f"<title>{_xesc('Lookout — ' + w['title'])}</title>"
        f"<link href=\"{_xesc(base_url + '/#/watch/' + str(wid))}\"/>"
        f"<updated>{_xesc(updated)}</updated>"
        + "".join(entries)
        + "</feed>\n"
    )


def _schedule_snippets(at: str) -> Dict[str, Any]:
    if hasattr(notify, "schedule_preview"):
        prev = notify.schedule_preview(at)
        return {
            "at": at,
            "platform": sys.platform,
            "cron": prev.get("cron", ""),
            "crontab": prev.get("cron", ""),
            "systemd_service": prev.get("systemd_service", ""),
            "systemd_timer": prev.get("systemd_timer", ""),
            "launchd_plist": prev.get("launchd_plist", ""),
            "schedule": _cached_schedule_status(),
        }
    hh, mm = at.split(":")
    h_num, m_num = int(hh), int(mm)
    root = str(db.ROOT)
    script = str(db.ROOT / "run.py")
    py = sys.executable
    cron_line = f"{m_num} {h_num} * * * cd \"{root}\" && \"{py}\" \"{script}\" scan --scheduled"
    service = (
        "[Unit]\nDescription=Lookout daily scan\n\n[Service]\nType=oneshot\n"
        f"WorkingDirectory={root}\nExecStart=\"{py}\" \"{script}\" scan --scheduled\n"
    )
    timer = (
        "[Unit]\nDescription=Run Lookout daily scan\n\n[Timer]\n"
        f"OnCalendar=*-*-* {hh}:{mm}:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        '  <key>Label</key><string>com.lookout.dailyscan</string>\n'
        '  <key>ProgramArguments</key><array>\n'
        f'    <string>{_xesc(py)}</string><string>{_xesc(script)}</string>'
        '<string>scan</string><string>--scheduled</string>\n'
        '  </array>\n'
        f'  <key>WorkingDirectory</key><string>{_xesc(root)}</string>\n'
        '  <key>StartCalendarInterval</key><dict>\n'
        f'    <key>Hour</key><integer>{h_num}</integer>'
        f'<key>Minute</key><integer>{m_num}</integer>\n'
        '  </dict>\n'
        '</dict></plist>\n'
    )
    return {
        "at": at,
        "platform": sys.platform,
        "cron": cron_line,
        "crontab": cron_line,
        "systemd_service": service,
        "systemd_timer": timer,
        "launchd_plist": plist,
        "schedule": _cached_schedule_status(),
    }


def _parse_int(v: Any, name: str) -> int:
    if isinstance(v, bool):
        raise ValueError(f"{name} must be a number")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None


def _parse_date_field(v: Any, name: str) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ValueError(f"{name} must be YYYY-MM-DD") from None


def _backup_payload() -> Dict[str, Any]:
    return {
        "version": 1,
        "exported": db.now(),
        "watches": [{k: w[k] for k in ("id", "title", "request", "active")} for w in db.watches()],
        "sources": [{k: s[k] for k in ("name", "url", "notes", "active", "watch_id")}
                    for s in db.sources() if s.get("method") != "plugin"],
        "filters": [{k: f[k] for k in ("kind", "value", "active", "watch_id")} for f in db.filters()],
    }


def _import_payload(b: Dict[str, Any]) -> Dict[str, int]:
    in_watches = b.get("watches") or []
    in_sources = b.get("sources") or []
    in_filters = b.get("filters") or []
    if not all(isinstance(x, list) for x in (in_watches, in_sources, in_filters)):
        raise ValueError("watches, sources and filters must be lists")
    added = {"watches": 0, "sources": 0, "filters": 0}
    watch_map: Dict[int, int] = {}
    existing_w = {(w["title"].strip().lower(), (w.get("request") or "").strip().lower()): w["id"] for w in db.watches()}
    for w in in_watches:
        if not isinstance(w, dict):
            raise ValueError("invalid watch entry")
        title = str(w.get("title") or "").strip()[:120]
        req = str(w.get("request") or "").strip()[:2000]
        if not title:
            raise ValueError("watch title cannot be empty")
        key = (title.lower(), req.lower())
        wid = existing_w.get(key)
        if wid is None:
            wid = db.add_watch(title, req, int(bool(w.get("active", 1))))
            existing_w[key] = wid
            added["watches"] += 1
        if isinstance(w.get("id"), int):
            watch_map[w["id"]] = wid

    def _map_wid(raw_wid: Any) -> Optional[int]:
        if raw_wid is None:
            return None
        wid_int = _parse_int(raw_wid, "watch_id")
        if wid_int in watch_map:
            return watch_map[wid_int]
        if db.watch_detail(wid_int):
            return wid_int
        return None

    existing_s = {(s["url"].rstrip("/").lower(), s.get("watch_id")) for s in db.sources()}
    for s in in_sources:
        if not isinstance(s, dict):
            raise ValueError("invalid source entry")
        name = str(s.get("name") or "").strip()
        url = str(s.get("url") or "").strip()
        if not name or not re.match(r"^https?://", url, re.I):
            raise ValueError("each source needs a name and an http(s) URL")
        wid = _map_wid(s.get("watch_id"))
        key = (url.rstrip("/").lower(), wid)
        if key not in existing_s:
            sid = db.add_source(name, url, str(s.get("notes") or ""), watch_id=wid)
            if s.get("active") in (0, False):
                db.update_source(sid, active=0)
            existing_s.add(key)
            added["sources"] += 1

    existing_f = {(f["kind"], f["value"].strip().lower(), f.get("watch_id")) for f in db.filters()}
    for f in in_filters:
        if not isinstance(f, dict):
            raise ValueError("invalid filter entry")
        kind = str(f.get("kind") or "").strip()
        val = str(f.get("value") or "").strip()
        wid = _map_wid(f.get("watch_id"))
        key = (kind, val.lower(), wid)
        if key not in existing_f:
            fid = db.add_filter(kind, val, watch_id=wid)
            if f.get("active") in (0, False):
                db.update_filter(fid, False)
            existing_f.add(key)
            added["filters"] += 1
    return added


class Handler(BaseHTTPRequestHandler):
    server_version = "Lookout"

    def log_message(self, *a):
        pass

    def _expected_token(self) -> str:
        return getattr(self.server, "api_token", None) or API_TOKEN

    def _render_index(self) -> bytes:
        raw = (WEB / "index.html").read_text(encoding="utf-8")
        tok = _html.escape(self._expected_token(), quote=True)
        meta = f'<meta name="lookout-token" content="{tok}">'
        if '<meta name="lookout-token"' in raw:
            out = re.sub(r'<meta name="lookout-token" content="[^"]*">', meta, raw, count=1)
        else:
            out = raw.replace("</head>", f"{meta}\n</head>", 1)
        return out.encode("utf-8")

    def _token_exempt(self, method: str, path: str) -> bool:
        return (
            method == "GET"
            and bool(re.fullmatch(r"/api/watches/\d+/(?:feed|atom)\.xml", path))
            and db.get_settings().get("rss_public") == "1"
        )

    def _drain_small_body(self) -> None:
        if self.command != "POST":
            return
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return
        try:
            n = int(raw_len)
        except ValueError:
            return
        if 0 < n <= 65536:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def _require_token(self, method: str, path: str) -> bool:
        if not path.startswith("/api/"):
            return True
        if self._token_exempt(method, path):
            return True
        got = self.headers.get("X-Lookout-Token") or ""
        if not hmac.compare_digest(got, self._expected_token()):
            self._drain_small_body()
            self.send(401, {"error": "unauthorized"})
            return False
        return True

    def send(self, code: int, body, ctype: str = "application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def allowed(self) -> bool:
        # Blocks DNS-rebinding and cross-site requests from other web pages.
        port = self.server.server_address[1]
        default_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        allowed_hosts = set(HOSTS) if HOSTS != default_hosts else {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed_hosts:
            self._drain_small_body()
            self.send(403, {"error": "forbidden"})
            return False
        origin = self.headers.get("Origin")
        if self.command == "POST" and origin:
            p = urllib.parse.urlparse(origin)
            if (p.scheme not in ("http", "https") or p.netloc not in allowed_hosts
                    or p.path not in ("", "/") or p.query or p.fragment):
                self._drain_small_body()
                self.send(403, {"error": "forbidden"})
                return False
        last_hit["t"] = time.time()
        return True

    def body(self) -> Dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}
        try:
            n = int(raw_len)
        except ValueError:
            raise ValueError("invalid content length") from None
        if n < 0 or n > 1_000_000:
            raise ValueError("request body too large")
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        if not raw.strip():
            return {}
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        return data

    def do_GET(self):
        if not self.allowed():
            return
        parsed = urllib.parse.urlsplit(self.path)
        if not self._require_token("GET", parsed.path):
            return
        try:
            qs = urllib.parse.parse_qs(parsed.query)
            self._get(parsed.path, qs)
        except ValueError as e:
            self.send(400, {"error": str(e)})
        except Exception:
            traceback.print_exc()
            self.send(500, {"error": "Internal server error"})

    def _get(self, path: str, qs: Optional[Dict[str, List[str]]] = None):
        qs = qs or {}
        if path in ("/", "/index.html"):
            return self.send(200, self._render_index(), "text/html; charset=utf-8")
        if re.fullmatch(r"/[\w.-]+\.(js|css|svg|ico|png)", path) and (WEB / path[1:]).is_file():
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            return self.send(200, (WEB / path[1:]).read_bytes(), ctype)
        if path == "/api/state":
            lim = max(1, min(MAX_ITEMS_LIMIT, _parse_int(qs["limit"][0], "limit"))) if "limit" in qs else DEFAULT_ITEMS_LIMIT
            off = max(0, _parse_int(qs["offset"][0], "offset")) if "offset" in qs else 0
            return self.send(200, state(limit=lim, offset=off))
        if path == "/api/items":
            lim = max(1, min(MAX_ITEMS_LIMIT, _parse_int(qs["limit"][0], "limit"))) if "limit" in qs else DEFAULT_ITEMS_LIMIT
            off = max(0, _parse_int(qs["offset"][0], "offset")) if "offset" in qs else 0
            wid_arg: Any = db._UNSET
            if "watch_id" in qs and qs["watch_id"][0] != "":
                raw_w = qs["watch_id"][0]
                wid_arg = None if raw_w == "global" else _parse_int(raw_w, "watch_id")
            total = db.items_count(watch_id=wid_arg)
            rows = db.items(limit=lim, offset=off, watch_id=wid_arg)
            return self.send(200, {"items": rows, "total": total, "limit": lim, "offset": off,
                                   "has_more": (off + len(rows)) < total})
        if m := re.fullmatch(r"/api/items/([\w-]+)/history", path):
            if not db.item_by_id(m[1]):
                return self.send(404, {"error": "not found"})
            return self.send(200, {"history": db.item_history(m[1])})
        if path == "/api/agenda.ics":
            return self.send(200, agenda_ics().encode("utf-8"), "text/calendar; charset=utf-8")
        if path == "/api/export.csv":
            buf = _io.StringIO()
            w = csv.writer(buf)
            w.writerow(["title", "organisation", "category", "source", "deadline", "start", "location", "url",
                        "first seen", "status", "your mark", "note"])
            for i in db.items():
                if i.get("match") == 0:
                    continue
                w.writerow([_csv_cell(x) for x in (
                    i["title"], i["org"], i["category"], i["source_name"], i["deadline"], i["start_date"],
                    i["location"], i["url"], i["first_seen"], i["status"], i["action"], i["note"]
                )])
            return self.send(200, ("\ufeff" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8")
        if path == "/api/backup.json":
            return self.send(200, _backup_payload())
        if m := re.fullmatch(r"/api/watches/(\d+)", path):
            w = db.watch_detail(int(m[1]))
            return self.send(200 if w else 404, w or {"error": "not found"})
        if m := re.fullmatch(r"/api/watches/(\d+)/feed\.xml", path):
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            xml_txt = _watch_rss(int(m[1]), base)
            if xml_txt is None:
                return self.send(404, {"error": "not found"})
            return self.send(200, xml_txt.encode("utf-8"), "application/rss+xml; charset=utf-8")
        if m := re.fullmatch(r"/api/watches/(\d+)/atom\.xml", path):
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            xml_txt = _watch_atom(int(m[1]), base)
            if xml_txt is None:
                return self.send(404, {"error": "not found"})
            return self.send(200, xml_txt.encode("utf-8"), "application/atom+xml; charset=utf-8")
        if m := re.fullmatch(r"/api/scans/(\d+)", path):
            d = db.scan_detail(int(m[1]))
            return self.send(200 if d else 404, d or {"error": "not found"})
        if m := re.fullmatch(r"/api/discoveries/(\d+)", path):
            d = db.discovery_detail(int(m[1]))
            return self.send(200 if d else 404, d or {"error": "not found"})
        if path == "/api/schedule/preview":
            at = str((qs.get("at") or [db.get_settings().get("schedule_time") or "08:30"])[0]).strip()[:5]
            if not notify._TIME_RE.fullmatch(at):
                raise ValueError("Time must be in HH:MM format (e.g. 08:30)")
            return self.send(200, _schedule_snippets(at))
        if path == "/api/agents":
            return self.send(200, {"agents": agents.detect(), "browser": agents.browser_tool(),
                                   "settings": public_settings()})
        if m := re.fullmatch(r"/api/agents/([\w-]+)/models", path):
            if m[1] not in agents.AGENTS:
                return self.send(200, {"models": []})
            return self.send(200, {"models": agents.models(m[1]) if agents.which(m[1]) else []})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed():
            return
        path = self.path.split("?")[0]
        if not self._require_token("POST", path):
            return
        try:
            b = self.body()
            return self.route(path, b)
        except (ValueError, json.JSONDecodeError) as e:
            return self.send(400, {"error": str(e) if isinstance(e, ValueError) else "bad json"})
        except Exception:
            traceback.print_exc()
            return self.send(500, {"error": "Internal server error"})

    def route(self, p: str, b: Dict[str, Any]):
        if p == "/api/scan":
            only = None
            if b.get("only") is not None:
                if not isinstance(b["only"], list):
                    raise ValueError("only must be a list of source ids")
                only = [_parse_int(x, "source id") for x in b["only"]] or None
            wid = None
            if b.get("watch_id") is not None:
                wid = _parse_int(b["watch_id"], "watch_id")
                if not db.watch_detail(wid):
                    return self.send(404, {"error": "watch not found"})
            return self.send(200, {"started": scanner.start_scan_async("manual", only, watch_id=wid)})
        if p == "/api/scan/cancel":
            scanner.cancel_scan(async_browser=True)
            return self.send(200, {"ok": True})
        if m := re.fullmatch(r"/api/scans/(\d+)/delete", p):
            sid = int(m[1])
            if scanner.progress["running"] and scanner.progress["scan_id"] == sid:
                return self.send(409, {"error": "Cannot delete a running scan"})
            if not db.scan_detail(sid):
                return self.send(404, {"error": "not found"})
            db.delete_scan(sid)
            return self.send(200, {"ok": True})

        if p == "/api/watches":
            title = str(b.get("title") or "").strip()
            if not title:
                raise ValueError("A title is required")
            wid = db.add_watch(title, str(b.get("request") or ""), int(bool(b.get("active", True))))
            return self.send(200, {"id": wid})
        if m := re.fullmatch(r"/api/watches/(\d+)", p):
            wid = int(m[1])
            fields: Dict[str, Any] = {}
            for k in ("title", "request"):
                if k in b:
                    fields[k] = b[k]
            if "active" in b:
                fields["active"] = bool(b["active"])
            if not fields:
                raise ValueError("No fields to update")
            if not db.update_watch(wid, **fields):
                return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": True})
        if m := re.fullmatch(r"/api/watches/(\d+)/delete", p):
            wid = int(m[1])
            if discovery.get("running") and discovery.get("watch_id") == wid:
                cancel_discovery(async_browser=True)
            if not db.delete_watch(wid):
                return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": True})
        if m := re.fullmatch(r"/api/watches/(\d+)/scan", p):
            wid = int(m[1])
            if not db.watch_detail(wid):
                return self.send(404, {"error": "not found"})
            return self.send(200, {"started": scanner.start_scan_async("manual", watch_id=wid)})

        if p == "/api/sources":
            name, url = str(b.get("name") or "").strip(), str(b.get("url") or "").strip()
            if not name or not re.match(r"^https?://", url, re.I):
                raise ValueError("A name and an http(s) URL are required")
            wid = None
            if b.get("watch_id") is not None:
                wid = _parse_int(b["watch_id"], "watch_id")
                if not db.watch_detail(wid):
                    return self.send(404, {"error": "watch not found"})
            return self.send(200, {"id": db.add_source(name, url, str(b.get("notes") or ""), watch_id=wid)})
        if p == "/api/sources/test":
            url = str(b.get("url") or "").strip()
            if not re.match(r"^https?://", url, re.I):
                raise ValueError("An http(s) URL is required")
            wid = None
            if b.get("watch_id") is not None:
                wid = _parse_int(b["watch_id"], "watch_id")
            src = {
                "id": None,
                "name": (str(b.get("name") or "").strip() or "Preview")[:80],
                "url": url[:500],
                "notes": str(b.get("notes") or "").strip()[:1000],
                "method": "agent",
                "watch_id": wid,
            }
            return self.send(200, scanner.test_source(src))
        if m := re.fullmatch(r"/api/sources/(\d+)/test", p):
            src = db.source_by_id(int(m[1]))
            if not src:
                return self.send(404, {"error": "not found"})
            if "notes" in b and b["notes"] is not None:
                src = {**src, "notes": str(b["notes"]).strip()[:1000]}
            return self.send(200, scanner.test_source(src))
        if m := re.fullmatch(r"/api/sources/(\d+)", p):
            sid = int(m[1])
            fields: Dict[str, Any] = {}
            if "name" in b:
                name = str(b["name"] or "").strip()
                if not name:
                    raise ValueError("Source name cannot be empty")
                fields["name"] = name[:80]
            if "url" in b:
                url = str(b["url"] or "").strip()
                if not re.match(r"^https?://", url, re.I):
                    raise ValueError("An http(s) URL is required")
                fields["url"] = url[:500]
            if "notes" in b:
                fields["notes"] = str(b["notes"] or "").strip()[:1000]
            if "active" in b:
                fields["active"] = int(bool(b["active"]))
            if "watch_id" in b:
                wid = b["watch_id"]
                if wid is not None:
                    wid = _parse_int(wid, "watch_id")
                    if not db.watch_detail(wid):
                        return self.send(404, {"error": "watch not found"})
                fields["watch_id"] = wid
            if b.get("relearn"):
                fields.update(extractor_ok=0, extractor_error="relearn requested")
            if not fields:
                raise ValueError("No fields to update")
            if not db.update_source(sid, **fields):
                return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": True})
        if m := re.fullmatch(r"/api/sources/(\d+)/delete", p):
            if not db.delete_source(int(m[1])):
                return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": True})

        if p == "/api/filters":
            wid = None
            if b.get("watch_id") is not None:
                wid = _parse_int(b["watch_id"], "watch_id")
                if not db.watch_detail(wid):
                    return self.send(404, {"error": "watch not found"})
            return self.send(200, {"id": db.add_filter(str(b.get("kind") or ""), str(b.get("value") or ""), watch_id=wid)})
        if m := re.fullmatch(r"/api/filters/(\d+)", p):
            if "active" not in b:
                raise ValueError("active is required")
            if not db.update_filter(int(m[1]), bool(b["active"])):
                return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": True})
        if m := re.fullmatch(r"/api/filters/(\d+)/delete", p):
            if not db.delete_filter(int(m[1])):
                return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": True})

        if m := re.fullmatch(r"/api/items/([\w-]+)", p):
            fields: Dict[str, Any] = {}
            if "action" in b:
                if b["action"] not in (None, "saved", "applied", "hidden"):
                    raise ValueError("bad action")
                fields["action"] = b["action"]
            if "note" in b:
                fields["note"] = str(b["note"]).strip()[:2000] if b["note"] else None
            if "remind_on" in b:
                fields["remind_on"] = _parse_date_field(b["remind_on"], "remind_on")
            if "snoozed_until" in b:
                fields["snoozed_until"] = _parse_date_field(b["snoozed_until"], "snoozed_until")
            if not fields:
                raise ValueError("No fields to update")
            if not db.set_item(m[1], **fields):
                return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": True})

        if p == "/api/settings":
            if "custom_command" in b:
                if b.get("confirm_custom_command") is not True:
                    return self.send(403, {"error": "Changing custom_command requires confirmation from the Settings form"})
            vals = {k: v for k, v in b.items() if k in db.DEFAULTS and not (k in SECRET_KEYS and not v)}
            if "custom_command" in vals:
                vals["custom_command"] = agents.validate_custom_command(str(vals["custom_command"] or ""))
            if vals.get("agent") not in (None, "") and vals["agent"] != "custom" and vals["agent"] not in agents.AGENTS:
                raise ValueError("unknown agent")
            for k, lo, hi in (("agent_timeout_min", 1, 120), ("parallel", 1, 8), ("smtp_port", 1, 65535)):
                if k in vals and str(vals[k]).strip() != "":
                    n = _parse_int(vals[k], k)
                    if n < lo or n > hi:
                        raise ValueError(f"{k} must be between {lo} and {hi}")
                    vals[k] = str(n)
            for k in ("headed", "notify", "verify_weekly", "first_run_done", "rss_public", "webhook_allow_local"):
                if k in vals:
                    if isinstance(vals[k], bool):
                        vals[k] = "1" if vals[k] else "0"
                    elif str(vals[k]) in ("0", "1"):
                        vals[k] = str(vals[k])
                    else:
                        raise ValueError(f"{k} must be 0 or 1")
            if "webhook_url" in vals:
                wu = str(vals["webhook_url"] or "").strip()[:500]
                if wu and not re.match(r"^https?://", wu, re.I):
                    raise ValueError("webhook_url must start with http:// or https://")
                vals["webhook_url"] = wu
            if "schedule_time" in vals:
                st_val = str(vals["schedule_time"]).strip()[:5]
                if not notify._TIME_RE.fullmatch(st_val):
                    raise ValueError("schedule_time must be HH:MM")
                vals["schedule_time"] = st_val
            db.set_settings(vals)
            return self.send(200, {"ok": True})
        if p == "/api/schedule":
            enabled = bool(b.get("enabled"))
            at = str(b.get("at") or "08:30").strip()[:5]
            if enabled and not notify._TIME_RE.fullmatch(at):
                raise ValueError("Time must be in HH:MM format (e.g. 08:30)")
            set_params = inspect.signature(notify.set_schedule).parameters
            accepts_kw = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in set_params.values())
            kw = {}
            if "backend" in set_params or accepts_kw:
                val = b.get("backend") if b.get("backend") is not None else b.get("method")
                if val is not None:
                    kw["backend"] = str(val)
            ok, msg = notify.set_schedule(enabled, at, **kw)
            _sched_cache.update(t=0.0, key=None, val=None)
            if ok:
                db.set_settings({"schedule_time": at})
            return self.send(200, {"ok": ok, "message": msg})
        if p == "/api/schedule/preview":
            at = str(b.get("at") or db.get_settings().get("schedule_time") or "08:30").strip()[:5]
            if not notify._TIME_RE.fullmatch(at):
                raise ValueError("Time must be in HH:MM format (e.g. 08:30)")
            return self.send(200, _schedule_snippets(at))
        if p == "/api/mail/test":
            port = self.server.server_address[1]
            ok, msg = notify.mail_digest([], f"http://127.0.0.1:{port}/")
            return self.send(200, {"ok": ok, "message": msg})
        if p == "/api/webhook/test":
            st = db.get_settings()
            url = str(b.get("url") or st.get("webhook_url") or "").strip()
            if not url:
                raise ValueError("Webhook URL is empty")
            if not re.match(r"^https?://", url, re.I):
                raise ValueError("Webhook URL must start with http:// or https://")
            fn = getattr(notify, "webhook", None)
            if not callable(fn):
                return self.send(200, {"ok": False, "message": "Webhook helper is unavailable."})
            allow_local = (
                bool(b["allow_local"]) if "allow_local" in b else (st.get("webhook_allow_local") == "1")
            )
            payload = {
                "event": "test",
                "title": "Lookout test webhook",
                "message": "Webhook notifications from Lookout are working.",
                "items": [],
            }
            try:
                wh_params = inspect.signature(fn).parameters
                wh_kw = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in wh_params.values())
                if "allow_local" in wh_params or wh_kw:
                    res = fn(url, payload, allow_local=allow_local)
                else:
                    res = fn(url, payload)
                if isinstance(res, tuple):
                    ok, msg = bool(res[0]), str(res[1])
                else:
                    ok, msg = bool(res), ("Webhook sent." if res else "Webhook failed.")
            except Exception as e:
                ok, msg = False, str(e)[:300]
            return self.send(200, {"ok": ok, "message": msg})
        if p == "/api/import":
            return self.send(200, {"ok": True, "added": _import_payload(b)})

        if p == "/api/discover":
            req = str(b.get("request") or "").strip()
            if len(req) < 5:
                raise ValueError("Describe what you want to monitor")
            with _disc_lock:
                if discovery["running"]:
                    return self.send(200, {"started": False, "id": discovery.get("id"),
                                           "watch_id": discovery.get("watch_id")})
                if b.get("watch_id") is not None:
                    wid = _parse_int(b["watch_id"], "watch_id")
                    w = db.watch_detail(wid)
                    if not w:
                        return self.send(404, {"error": "watch not found"})
                    title = w["title"]
                else:
                    title = tasks.quick_title(req)
                    wid = db.add_watch(title, req)
                _disc_cancel.clear()
                st = db.get_settings()
                did = db.start_discovery(req, title, st["agent"], st["model"], watch_id=wid)
                discovery.update(id=did, watch_id=wid, running=True, request=req, title=title, results=[],
                                 error="", finished=None)
            threading.Thread(target=run_discovery, args=(did, wid, req), daemon=True).start()
            return self.send(200, {"started": True, "id": did, "watch_id": wid})
        if p == "/api/discover/cancel":
            cancel_discovery(async_browser=True)
            return self.send(200, {"ok": True})
        if m := re.fullmatch(r"/api/discoveries/(\d+)/delete", p):
            did = int(m[1])
            with _disc_lock:
                if discovery["running"] and discovery.get("id") == did:
                    return self.send(409, {"error": "Cannot delete a running search"})
                if not db.discovery_detail(did):
                    return self.send(404, {"error": "not found"})
                db.delete_discovery(did)
                if discovery.get("id") == did:
                    discovery.update(id=None, watch_id=None, results=[], error="", request="", title="", finished=None)
            return self.send(200, {"ok": True})
        if p == "/api/discover/clear":
            with _disc_lock:
                if discovery["running"]:
                    return self.send(409, {"error": "Cannot clear while a search is running"})
                discovery.update(id=None, watch_id=None, results=[], error="", request="", title="", finished=None)
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


def make_server(port: Optional[int] = None) -> ThreadingHTTPServer:
    global API_TOKEN
    db.init()
    db.mark_interrupted()
    scanner.load_plugins()
    token = secrets.token_urlsafe(32)
    API_TOKEN = token
    srv = ThreadingHTTPServer(("127.0.0.1", PORT if port is None else port), Handler)
    srv.api_token = token
    srv.daemon_threads = True
    return srv


def serve(idle_minutes: int = 0) -> None:
    srv = make_server()
    if idle_minutes:
        def watchdog():
            while True:
                time.sleep(30)
                busy = scanner.progress["running"] or discovery["running"]
                if not busy and time.time() - last_hit["t"] > idle_minutes * 60:
                    srv.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
    srv.serve_forever()
