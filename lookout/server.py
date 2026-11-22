"""Local web UI + JSON API. Listens on 127.0.0.1 only."""
import csv
import io as _io
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from lookout import agents, db, notify, scanner, tasks

WEB = db.ROOT / "web"
PORT = int(os.environ.get("LOOKOUT_PORT") or 8780)
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
SECRET_KEYS = {"smtp_password"}
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


def state() -> Dict[str, Any]:
    return {"items": db.items(), "scans": db.scans(), "discoveries": db.discoveries(),
            "watches": db.watches(), "sources": db.sources(), "filters": db.filters(),
            "progress": {k: scanner.progress.get(k) for k in ("running", "scan_id", "done", "total", "current", "learning")},
            "settings": public_settings(), "discovery": dict(discovery), "schedule": _cached_schedule_status()}


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
    try:
        out = tasks.discover_sources(request, cancel_event=_disc_cancel, session=f"lookout-discover-{did}")
        results = out["sources"]
        title = out.get("title")
        new_filters = out.get("filters") or []
        if _disc_cancel.is_set():
            status = "cancelled"
        elif wid is not None and db.watch_detail(wid):
            if title:
                db.update_watch(wid, title=title)
            have_f = {(f["kind"], f["value"].strip().lower()) for f in db.filters(watch_id=wid)}
            for f in new_filters:
                key = (f["kind"], f["value"].strip().lower())
                if key not in have_f:
                    try:
                        db.add_filter(f["kind"], f["value"], watch_id=wid)
                        have_f.add(key)
                    except ValueError:
                        pass
            have_u = {s["url"].rstrip("/").lower() for s in db.sources(watch_id=wid)}
            for c in results:
                u = c["url"].rstrip("/").lower()
                if u not in have_u:
                    sid = db.add_source(c["name"], c["url"], c.get("notes") or "", watch_id=wid)
                    added_ids.append(sid)
                    have_u.add(u)
    except agents.CancelledError:
        status, error = "cancelled", ""
    except Exception as e:
        if _disc_cancel.is_set():
            status, error = "cancelled", ""
        else:
            status, error = "failed", str(e)[:400]
    finally:
        db.finish_discovery(did, status, results, error, title=title)
        with _disc_lock:
            if discovery.get("id") == did:
                discovery.update(id=did, watch_id=wid, running=False, results=results, error=error,
                                 finished=db.now(), title=title or discovery.get("title", ""))
        if status == "done" and added_ids and wid is not None and db.watch_detail(wid):
            scanner.start_scan_async("manual", only=added_ids, watch_id=wid)


def agenda_ics() -> str:
    """Saved items as all-day calendar events: one for the deadline, one for the user's reminder."""
    def text(v):
        return str(v or "").replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", " ")
    stamp = db.now().replace("-", "").replace(":", "").replace(" ", "T")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Lookout//Agenda//EN", "CALSCALE:GREGORIAN", "X-WR-CALNAME:Lookout"]
    for i in db.items():
        if i["action"] not in ("saved", "applied"):
            continue
        for kind, day in (("Deadline", i["deadline"]), ("Reminder", i["remind_on"])):
            if not day:
                continue
            d = day.replace("-", "")
            lines += ["BEGIN:VEVENT", f"UID:{i['id']}-{kind.lower()}@lookout", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{d}",
                      f"SUMMARY:{text(kind + ': ' + i['title'])}", f"URL:{text(i['url'])}",
                      f"DESCRIPTION:{text(' - '.join(x for x in (i['org'], i['source_name'], i['note']) if x))}",
                      "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


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
            self.send(403, {"error": "forbidden"})
            return False
        origin = self.headers.get("Origin")
        if self.command == "POST" and origin:
            p = urllib.parse.urlparse(origin)
            if (p.scheme not in ("http", "https") or p.netloc not in allowed_hosts
                    or p.path not in ("", "/") or p.query or p.fragment):
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
        try:
            self._get(self.path.split("?")[0])
        except ValueError as e:
            self.send(400, {"error": str(e)})
        except Exception:
            traceback.print_exc()
            self.send(500, {"error": "Internal server error"})

    def _get(self, path: str):
        if path in ("/", "/index.html"):
            return self.send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if re.fullmatch(r"/[\w.-]+\.(js|css|svg|ico|png)", path) and (WEB / path[1:]).is_file():
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            return self.send(200, (WEB / path[1:]).read_bytes(), ctype)
        if path == "/api/state":
            return self.send(200, state())
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
                w.writerow([i["title"], i["org"], i["category"], i["source_name"], i["deadline"], i["start_date"],
                            i["location"], i["url"], i["first_seen"], i["status"], i["action"], i["note"]])
            return self.send(200, ("\ufeff" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8")
        if path == "/api/backup.json":
            return self.send(200, _backup_payload())
        if m := re.fullmatch(r"/api/watches/(\d+)", path):
            w = db.watch_detail(int(m[1]))
            return self.send(200 if w else 404, w or {"error": "not found"})
        if m := re.fullmatch(r"/api/scans/(\d+)", path):
            d = db.scan_detail(int(m[1]))
            return self.send(200 if d else 404, d or {"error": "not found"})
        if m := re.fullmatch(r"/api/discoveries/(\d+)", path):
            d = db.discovery_detail(int(m[1]))
            return self.send(200 if d else 404, d or {"error": "not found"})
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
        try:
            b = self.body()
            return self.route(self.path.split("?")[0], b)
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
            vals = {k: v for k, v in b.items() if k in db.DEFAULTS and not (k in SECRET_KEYS and not v)}
            if vals.get("agent") not in (None, "") and vals["agent"] != "custom" and vals["agent"] not in agents.AGENTS:
                raise ValueError("unknown agent")
            for k, lo, hi in (("agent_timeout_min", 1, 120), ("parallel", 1, 8), ("smtp_port", 1, 65535)):
                if k in vals and str(vals[k]).strip() != "":
                    n = _parse_int(vals[k], k)
                    if n < lo or n > hi:
                        raise ValueError(f"{k} must be between {lo} and {hi}")
                    vals[k] = str(n)
            for k in ("headed", "notify", "verify_weekly", "first_run_done"):
                if k in vals:
                    if isinstance(vals[k], bool):
                        vals[k] = "1" if vals[k] else "0"
                    elif str(vals[k]) in ("0", "1"):
                        vals[k] = str(vals[k])
                    else:
                        raise ValueError(f"{k} must be 0 or 1")
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
            ok, msg = notify.set_schedule(enabled, at)
            _sched_cache.update(t=0.0, key=None, val=None)
            if ok:
                db.set_settings({"schedule_time": at})
            return self.send(200, {"ok": ok, "message": msg})
        if p == "/api/mail/test":
            port = self.server.server_address[1]
            ok, msg = notify.mail_digest([], f"http://127.0.0.1:{port}/")
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
    db.init()
    db.mark_interrupted()
    scanner.load_plugins()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT if port is None else port), Handler)
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
