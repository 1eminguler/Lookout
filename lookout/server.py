"""Local web UI + JSON API. Listens on 127.0.0.1 only."""
import json
import mimetypes
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from lookout import agents, db, notify, scanner, tasks

WEB = db.ROOT / "web"
PORT = int(__import__("os").environ.get("LOOKOUT_PORT") or 8780)
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
SECRET_KEYS = {"smtp_password"}
discovery: Dict[str, Any] = {"id": None, "running": False, "request": "", "title": "", "results": [], "error": "",
                             "finished": None}
_disc_lock = threading.Lock()
last_hit = {"t": time.time()}


def public_settings() -> Dict[str, Any]:
    s = db.get_settings()
    out = {k: v for k, v in s.items() if k not in SECRET_KEYS}
    out["smtp_password_set"] = bool(s.get("smtp_password"))
    return out


def state() -> Dict[str, Any]:
    return {"items": db.items(), "scans": db.scans(), "discoveries": db.discoveries(), "sources": db.sources(),
            "filters": db.filters(),
            "progress": {k: scanner.progress.get(k) for k in ("running", "scan_id", "done", "total", "current", "learning")},
            "settings": public_settings(), "discovery": discovery, "schedule": notify.schedule_status()}


def run_discovery(did: int, request: str) -> None:
    results, error, status, title = [], "", "done", None
    try:
        out = tasks.discover_sources(request)
        results, title = out["sources"], out.get("title")
    except Exception as e:
        status, error = "failed", str(e)[:400]
    finally:
        db.finish_discovery(did, status, results, error, title=title)
        discovery.update(id=did, running=False, results=results, error=error, finished=db.now(),
                         title=title or discovery.get("title", ""))


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


class Handler(BaseHTTPRequestHandler):
    server_version = "Lookout"

    def log_message(self, *a):
        pass

    def send(self, code: int, body, ctype: str = "application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self) -> bool:
        # Blocks DNS-rebinding and cross-site requests from other web pages.
        if self.headers.get("Host") not in HOSTS:
            self.send(403, {"error": "forbidden"})
            return False
        origin = self.headers.get("Origin")
        if self.command == "POST" and origin and origin.split("//")[-1] not in HOSTS:
            self.send(403, {"error": "forbidden"})
            return False
        last_hit["t"] = time.time()
        return True

    def body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        data = json.loads(self.rfile.read(min(n, 200_000)) or b"{}")
        return data if isinstance(data, dict) else {}

    def do_GET(self):
        if not self.allowed():
            return
        path = self.path.split("?")[0]
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
            import csv, io as _io
            buf = _io.StringIO()
            w = csv.writer(buf)
            w.writerow(["title", "organisation", "category", "source", "deadline", "start", "location", "url",
                        "first seen", "status", "your mark", "note"])
            for i in db.items():
                if i.get("match") == 0:
                    continue
                w.writerow([i["title"], i["org"], i["category"], i["source_name"], i["deadline"], i["start_date"],
                            i["location"], i["url"], i["first_seen"], i["status"], i["action"], i["note"]])
            return self.send(200, ("﻿" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8")
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
        except (ValueError, json.JSONDecodeError):
            return self.send(400, {"error": "bad json"})
        p = self.path.split("?")[0]
        try:
            return self.route(p, b)
        except ValueError as e:
            return self.send(400, {"error": str(e)})

    def route(self, p: str, b: Dict[str, Any]):
        if p == "/api/scan":
            only = [int(x) for x in b.get("only") or []] or None
            return self.send(200, {"started": scanner.start_scan_async("manual", only)})
        if p == "/api/scan/cancel":
            scanner.progress["cancel"] = True
            return self.send(200, {"ok": True})
        if m := re.fullmatch(r"/api/scans/(\d+)/delete", p):
            db.delete_scan(int(m[1]))
            return self.send(200, {"ok": True})

        if p == "/api/sources":
            name, url = str(b.get("name", "")).strip(), str(b.get("url", "")).strip()
            if not name or not re.match(r"^https?://", url):
                raise ValueError("A name and an http(s) URL are required")
            return self.send(200, {"id": db.add_source(name, url, str(b.get("notes", "")))})
        if m := re.fullmatch(r"/api/sources/(\d+)", p):
            fields = {k: b[k] for k in ("name", "url", "notes") if k in b}
            if "active" in b:
                fields["active"] = int(bool(b["active"]))
            if b.get("relearn"):
                fields.update(extractor_ok=0, extractor_error="relearn requested")
            db.update_source(int(m[1]), **fields)
            return self.send(200, {"ok": True})
        if m := re.fullmatch(r"/api/sources/(\d+)/delete", p):
            db.delete_source(int(m[1]))
            return self.send(200, {"ok": True})

        if p == "/api/filters":
            return self.send(200, {"id": db.add_filter(str(b.get("kind", "")), str(b.get("value", "")))})
        if m := re.fullmatch(r"/api/filters/(\d+)", p):
            db.update_filter(int(m[1]), bool(b.get("active")))
            return self.send(200, {"ok": True})
        if m := re.fullmatch(r"/api/filters/(\d+)/delete", p):
            db.delete_filter(int(m[1]))
            return self.send(200, {"ok": True})

        if m := re.fullmatch(r"/api/items/([\w-]+)", p):
            fields = {k: (b[k] or None) for k in ("action", "note", "remind_on") if k in b}
            if fields.get("action") not in (None, "saved", "applied", "hidden"):
                raise ValueError("bad action")
            return self.send(200, {"ok": db.set_item(m[1], **fields)})

        if p == "/api/settings":
            vals = {k: v for k, v in b.items() if k in db.DEFAULTS and not (k in SECRET_KEYS and not v)}
            if vals.get("agent") not in (None, "") and vals["agent"] != "custom" and vals["agent"] not in agents.AGENTS:
                raise ValueError("unknown agent")
            db.set_settings(vals)
            return self.send(200, {"ok": True})
        if p == "/api/schedule":
            ok, msg = notify.set_schedule(bool(b.get("enabled")), str(b.get("at") or "08:30")[:5])
            if ok:
                db.set_settings({"schedule_time": str(b.get("at") or "08:30")[:5]})
            return self.send(200, {"ok": ok, "message": msg})
        if p == "/api/mail/test":
            ok, msg = notify.mail_digest([], f"http://127.0.0.1:{PORT}/")
            return self.send(200, {"ok": ok, "message": msg})

        if p == "/api/discover":
            req = str(b.get("request", "")).strip()
            if len(req) < 5:
                raise ValueError("Describe what you want to monitor")
            with _disc_lock:
                if discovery["running"]:
                    return self.send(200, {"started": False, "id": discovery.get("id")})
                st = db.get_settings()
                title = tasks.quick_title(req)
                did = db.start_discovery(req, title, st["agent"], st["model"])
                discovery.update(id=did, running=True, request=req, title=title, results=[], error="", finished=None)
            threading.Thread(target=run_discovery, args=(did, req), daemon=True).start()
            return self.send(200, {"started": True, "id": did})
        if m := re.fullmatch(r"/api/discoveries/(\d+)/delete", p):
            did = int(m[1])
            db.delete_discovery(did)
            if discovery.get("id") == did and not discovery["running"]:
                discovery.update(id=None, results=[], error="", request="", title="", finished=None)
            return self.send(200, {"ok": True})
        if p == "/api/discover/clear":
            discovery.update(id=None, results=[], error="", request="", title="", finished=None)
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


def serve(idle_minutes: int = 0) -> None:
    db.init()
    scanner.load_plugins()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
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
