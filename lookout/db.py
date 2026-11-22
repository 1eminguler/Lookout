"""SQLite storage: sources, filters, scans, items, settings and agent usage."""
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
import os
DB_PATH = Path(os.environ.get("LOOKOUT_DATA_DIR") or ROOT / "data") / "lookout.db"
TS = "%Y-%m-%d %H:%M:%S"
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    notes TEXT DEFAULT '',
    method TEXT DEFAULT 'agent',          -- agent | plugin
    plugin TEXT,                          -- plugin source name when method = plugin
    recipe TEXT,                          -- what the agent learned about the site (JSON)
    active INTEGER DEFAULT 1,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                   -- include | exclude | type | location | deadline_within | instruction
    value TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL,
    finished TEXT,
    trigger TEXT DEFAULT 'manual',        -- manual | scheduled
    agent TEXT,
    model TEXT,
    status TEXT DEFAULT 'running',        -- running | done | failed | cancelled
    found INTEGER DEFAULT 0,
    new INTEGER DEFAULT 0,
    filters TEXT,                         -- snapshot (JSON)
    note TEXT
);

CREATE TABLE IF NOT EXISTS scan_sources (
    scan_id INTEGER NOT NULL,
    source_id INTEGER,
    source_name TEXT NOT NULL,
    ok INTEGER,
    count INTEGER DEFAULT 0,
    new INTEGER DEFAULT 0,
    error TEXT,
    ms INTEGER DEFAULT 0,
    PRIMARY KEY (scan_id, source_name)
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source_id INTEGER,
    source_name TEXT NOT NULL,
    title TEXT NOT NULL,
    org TEXT,
    category TEXT,
    url TEXT NOT NULL,
    deadline TEXT,
    start_date TEXT,
    location TEXT,
    summary TEXT,
    extra TEXT,                           -- free JSON (price, salary, ...)
    status TEXT DEFAULT 'open',           -- open | gone
    first_scan INTEGER,
    last_scan INTEGER,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    notified INTEGER DEFAULT 0,
    action TEXT,                          -- saved | applied | hidden
    note TEXT,
    remind_on TEXT                        -- agenda date chosen by the user
);

CREATE TABLE IF NOT EXISTS scan_items (
    scan_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    is_new INTEGER DEFAULT 0,
    PRIMARY KEY (scan_id, item_id)
);

CREATE TABLE IF NOT EXISTS agent_usage (
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    day TEXT NOT NULL,
    calls INTEGER DEFAULT 0,
    ok INTEGER DEFAULT 0,
    last_call TEXT,
    last_error TEXT,
    resets_at TEXT,
    PRIMARY KEY (agent, model, day)
);

CREATE TABLE IF NOT EXISTS discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL,
    finished TEXT,
    request TEXT,
    title TEXT,
    status TEXT DEFAULT 'running',        -- running | done | failed
    agent TEXT,
    model TEXT,
    results TEXT,                         -- JSON array
    error TEXT
);
"""


def now() -> str:
    return datetime.now().strftime(TS)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


MIGRATIONS = {
    "sources": {"extractor": "TEXT", "extractor_ok": "INTEGER DEFAULT 0", "extractor_error": "TEXT",
                "extractor_updated": "TEXT", "extractor_runs": "INTEGER DEFAULT 0"},
    "items": {"match": "INTEGER", "match_key": "TEXT"},
    "scans": {"label": "TEXT"},
}


def init() -> None:
    with _lock, connect() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        c.commit()


def _rows(sql: str, args: Iterable = ()) -> List[Dict[str, Any]]:
    with _lock, connect() as c:
        return [dict(r) for r in c.execute(sql, tuple(args))]


def _one(sql: str, args: Iterable = ()) -> Optional[Dict[str, Any]]:
    r = _rows(sql, args)
    return r[0] if r else None


def _exec(sql: str, args: Iterable = ()) -> int:
    with _lock, connect() as c:
        cur = c.execute(sql, tuple(args))
        c.commit()
        return cur.lastrowid or cur.rowcount


# ---------- settings ----------
DEFAULTS = {
    "agent": "", "model": "", "headed": "0", "notify": "1", "schedule_time": "08:30",
    "agent_timeout_min": "12", "parallel": "2", "first_run_done": "0",
    "custom_command": "", "mail_to": "", "smtp_host": "smtp.gmail.com", "smtp_port": "587", "smtp_user": "",
    "smtp_password": "", "language_hint": "", "verify_weekly": "1",
}


def get_settings() -> Dict[str, str]:
    s = dict(DEFAULTS)
    s.update({r["key"]: r["value"] for r in _rows("SELECT key, value FROM settings")})
    return s


def set_settings(values: Dict[str, Any]) -> None:
    with _lock, connect() as c:
        c.executemany("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      [(k, str(v)) for k, v in values.items() if k in DEFAULTS])
        c.commit()


# ---------- sources ----------
def sources(active_only: bool = False) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM sources" + (" WHERE active=1" if active_only else "") + " ORDER BY name COLLATE NOCASE"
    out = _rows(sql)
    for s in out:
        last = _one("SELECT ok, count, error, scan_id FROM scan_sources WHERE source_id=? ORDER BY scan_id DESC LIMIT 1",
                    (s["id"],))
        s["last"] = last
        s["open_items"] = _one("SELECT COUNT(*) n FROM items WHERE source_id=? AND status='open'", (s["id"],))["n"]
    return out


def add_source(name: str, url: str, notes: str = "", method: str = "agent", plugin: Optional[str] = None) -> int:
    return _exec("INSERT INTO sources(name, url, notes, method, plugin, created) VALUES(?, ?, ?, ?, ?, ?)",
                 (name.strip()[:80], url.strip()[:500], notes.strip()[:1000], method, plugin, now()))


def update_source(sid: int, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in ("name", "url", "notes", "active", "recipe", "extractor",
                                                        "extractor_ok", "extractor_error", "extractor_updated",
                                                        "extractor_runs")}
    if allowed:
        _exec(f"UPDATE sources SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), sid))


def delete_source(sid: int) -> None:
    _exec("DELETE FROM sources WHERE id=?", (sid,))


def source_by_plugin(plugin: str) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM sources WHERE method='plugin' AND plugin=?", (plugin,))


# ---------- filters ----------
FILTER_KINDS = ("include", "exclude", "type", "location", "deadline_within", "instruction")


def filters(active_only: bool = False) -> List[Dict[str, Any]]:
    return _rows("SELECT * FROM filters" + (" WHERE active=1" if active_only else "") + " ORDER BY kind, id")


def add_filter(kind: str, value: str) -> int:
    if kind not in FILTER_KINDS or not value.strip():
        raise ValueError("invalid filter")
    return _exec("INSERT INTO filters(kind, value, created) VALUES(?, ?, ?)", (kind, value.strip()[:500], now()))


def update_filter(fid: int, active: bool) -> None:
    _exec("UPDATE filters SET active=? WHERE id=?", (int(active), fid))


def delete_filter(fid: int) -> None:
    _exec("DELETE FROM filters WHERE id=?", (fid,))


# ---------- scans ----------
def start_scan(trigger: str, agent: str, model: str, filter_snapshot: List[Dict[str, Any]],
               label: Optional[str] = None) -> int:
    return _exec("INSERT INTO scans(started, trigger, agent, model, filters, label) VALUES(?, ?, ?, ?, ?, ?)",
                 (now(), trigger, agent, model, json.dumps(filter_snapshot, ensure_ascii=False), label))


def finish_scan(scan_id: int, status: str, note: str = "") -> None:
    q = "SELECT COUNT(*) n FROM scan_items si JOIN items i ON i.id=si.item_id WHERE si.scan_id=? AND COALESCE(i.match,1)=1"
    found = _one(q, (scan_id,))["n"]
    new = _one(q + " AND si.is_new=1", (scan_id,))["n"]
    _exec("UPDATE scans SET finished=?, status=?, found=?, new=?, note=? WHERE id=?",
          (now(), status, found, new, note, scan_id))


def record_source_result(scan_id: int, source: Dict[str, Any], ok: bool, count: int, new: int, error: str, ms: int):
    _exec("INSERT OR REPLACE INTO scan_sources(scan_id, source_id, source_name, ok, count, new, error, ms) "
          "VALUES(?, ?, ?, ?, ?, ?, ?, ?)", (scan_id, source.get("id"), source["name"], int(ok), count, new, error, ms))


def scans(limit: int = 60) -> List[Dict[str, Any]]:
    return _rows("SELECT id, started, finished, trigger, agent, model, status, found, new, note, label FROM scans "
                 "ORDER BY id DESC LIMIT ?", (limit,))


def scan_detail(scan_id: int) -> Optional[Dict[str, Any]]:
    s = _one("SELECT * FROM scans WHERE id=?", (scan_id,))
    if not s:
        return None
    s["filters"] = json.loads(s["filters"] or "[]")
    s["sources"] = _rows("SELECT * FROM scan_sources WHERE scan_id=? ORDER BY ok, source_name", (scan_id,))
    s["items"] = _rows("SELECT i.*, si.is_new FROM scan_items si JOIN items i ON i.id = si.item_id "
                       "WHERE si.scan_id=? ORDER BY si.is_new DESC, i.deadline IS NULL, i.deadline", (scan_id,))
    return s


def delete_scan(scan_id: int) -> None:
    with _lock, connect() as c:
        for t in ("scan_items", "scan_sources", "scans"):
            c.execute(f"DELETE FROM {t} WHERE {'id' if t == 'scans' else 'scan_id'}=?", (scan_id,))
        c.commit()


def running_scan() -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM scans WHERE status='running' ORDER BY id DESC LIMIT 1")


# ---------- discoveries ----------
def start_discovery(request: str, title: str, agent: str, model: str) -> int:
    return _exec("INSERT INTO discoveries(started, request, title, agent, model) VALUES(?, ?, ?, ?, ?)",
                 (now(), request, title, agent, model))


def finish_discovery(discovery_id: int, status: str, results: List[Dict[str, Any]], error: str = "",
                     title: Optional[str] = None) -> None:
    raw = json.dumps(results or [], ensure_ascii=False)
    if title:
        _exec("UPDATE discoveries SET finished=?, status=?, results=?, error=?, title=? WHERE id=?",
              (now(), status, raw, error, title, discovery_id))
    else:
        _exec("UPDATE discoveries SET finished=?, status=?, results=?, error=? WHERE id=?",
              (now(), status, raw, error, discovery_id))


def discoveries(limit: int = 60) -> List[Dict[str, Any]]:
    rows = _rows("SELECT id, started, finished, request, title, status, agent, model, results, error "
                 "FROM discoveries ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        raw = r.pop("results")
        try:
            r["found"] = len(json.loads(raw)) if raw else 0
        except (TypeError, ValueError):
            r["found"] = 0
    return rows


def discovery_detail(discovery_id: int) -> Optional[Dict[str, Any]]:
    d = _one("SELECT * FROM discoveries WHERE id=?", (discovery_id,))
    if not d:
        return None
    try:
        d["results"] = json.loads(d["results"] or "[]")
    except (TypeError, ValueError):
        d["results"] = []
    return d


def delete_discovery(discovery_id: int) -> None:
    _exec("DELETE FROM discoveries WHERE id=?", (discovery_id,))


# ---------- items ----------
def save_item(scan_id: int, source: Dict[str, Any], it: Dict[str, Any]) -> bool:
    """Insert or refresh an item. Returns True when it was never seen before."""
    t = now()
    with _lock, connect() as c:
        exists = c.execute("SELECT id FROM items WHERE id=?", (it["id"],)).fetchone()
        if exists:
            c.execute("""UPDATE items SET title=?, org=?, category=?, url=?, deadline=?, start_date=?, location=?,
                         summary=?, extra=?, status='open', last_scan=?, last_seen=? WHERE id=?""",
                      (it["title"], it.get("org"), it.get("category"), it["url"], it.get("deadline"),
                       it.get("start_date"), it.get("location"), it.get("summary"), it.get("extra"), scan_id, t,
                       it["id"]))
        else:
            c.execute("""INSERT INTO items(id, source_id, source_name, title, org, category, url, deadline, start_date,
                         location, summary, extra, first_scan, last_scan, first_seen, last_seen)
                         VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (it["id"], source.get("id"), source["name"], it["title"], it.get("org"), it.get("category"),
                       it["url"], it.get("deadline"), it.get("start_date"), it.get("location"), it.get("summary"),
                       it.get("extra"), scan_id, scan_id, t, t))
        c.execute("INSERT OR REPLACE INTO scan_items(scan_id, item_id, is_new) VALUES(?, ?, ?)",
                  (scan_id, it["id"], 0 if exists else 1))
        c.commit()
        return not exists


def mark_gone(source: Dict[str, Any], scan_id: int) -> None:
    """Items a successfully scanned source no longer lists are marked gone."""
    if source.get("id") is None:
        _exec("UPDATE items SET status='gone' WHERE source_name=? AND status='open' AND last_scan<?",
              (source["name"], scan_id))
    else:
        _exec("UPDATE items SET status='gone' WHERE source_id=? AND status='open' AND last_scan<?",
              (source["id"], scan_id))


def items() -> List[Dict[str, Any]]:
    return _rows("SELECT * FROM items ORDER BY first_seen DESC")


def set_item(item_id: str, **fields) -> bool:
    allowed = {k: v for k, v in fields.items() if k in ("action", "note", "remind_on")}
    if not allowed:
        return False
    return _exec(f"UPDATE items SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), item_id)) > 0


def unnotified() -> List[Dict[str, Any]]:
    return _rows("SELECT * FROM items WHERE notified=0 AND status='open' AND (action IS NULL OR action!='hidden')")


def mark_notified(ids: Iterable[str]) -> None:
    with _lock, connect() as c:
        c.executemany("UPDATE items SET notified=1 WHERE id=?", [(i,) for i in ids])
        c.commit()


# ---------- agent usage ----------
def log_agent_call(agent: str, model: str, ok: bool, error: str = "", resets_at: Optional[str] = None) -> None:
    t = now()
    _exec("""INSERT INTO agent_usage(agent, model, day, calls, ok, last_call, last_error, resets_at)
             VALUES(?, ?, ?, 1, ?, ?, ?, ?)
             ON CONFLICT(agent, model, day) DO UPDATE SET calls=calls+1, ok=ok+excluded.ok, last_call=excluded.last_call,
             last_error=excluded.last_error, resets_at=excluded.resets_at""",
          (agent, model or "", t[:10], int(ok), t, error[-400:], resets_at))


def agent_blocked_until(agent: str, model: str) -> Optional[str]:
    r = _one("SELECT resets_at FROM agent_usage WHERE agent=? AND model=? ORDER BY last_call DESC LIMIT 1",
             (agent, model or ""))
    return r["resets_at"] if r and r["resets_at"] and r["resets_at"] > now() else None


def agent_usage(agent: str) -> List[Dict[str, Any]]:
    since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    out = _rows("""SELECT model, SUM(calls) calls, SUM(ok) ok, SUM(CASE WHEN day=? THEN calls ELSE 0 END) today,
                   MAX(last_call) last_call FROM agent_usage WHERE agent=? AND day>=? GROUP BY model
                   ORDER BY last_call DESC""", (now()[:10], agent, since))
    for r in out:
        r["blocked_until"] = agent_blocked_until(agent, r["model"])
    return out


def items_needing_match(scan_id: int, key: str) -> List[Dict[str, Any]]:
    return _rows("""SELECT i.* FROM scan_items si JOIN items i ON i.id = si.item_id
                    WHERE si.scan_id=? AND (i.match_key IS NULL OR i.match_key != ?)""", (scan_id, key))


def set_matches(decisions: Dict[str, bool], key: str) -> None:
    with _lock, connect() as c:
        c.executemany("UPDATE items SET match=?, match_key=? WHERE id=?",
                      [(int(v), key, k) for k, v in decisions.items()])
        c.commit()
