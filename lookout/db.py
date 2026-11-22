"""SQLite storage: watches, sources, filters, scans, discoveries, items, settings and agent usage."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LOOKOUT_DATA_DIR") or ROOT / "data") / "lookout.db"
TS = "%Y-%m-%d %H:%M:%S"
_lock = threading.RLock()
_UNSET = object()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    request TEXT DEFAULT '',
    created TEXT NOT NULL,
    active INTEGER DEFAULT 1
);

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
    status TEXT DEFAULT 'running',        -- running | done | failed | cancelled | interrupted
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
    status TEXT DEFAULT 'running',        -- running | done | failed | cancelled | interrupted
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


@contextmanager
def _conn():
    c = connect()
    try:
        yield c
    finally:
        c.close()


MIGRATIONS = {
    "sources": {"extractor": "TEXT", "extractor_ok": "INTEGER DEFAULT 0", "extractor_error": "TEXT",
                "extractor_updated": "TEXT", "extractor_runs": "INTEGER DEFAULT 0", "watch_id": "INTEGER"},
    "filters": {"watch_id": "INTEGER"},
    "items": {"match": "INTEGER", "match_key": "TEXT", "watch_id": "INTEGER", "snoozed_until": "TEXT"},
    "scans": {"label": "TEXT", "watch_id": "INTEGER"},
    "discoveries": {"watch_id": "INTEGER"},
}


def init() -> None:
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        c.commit()


def mark_interrupted() -> Tuple[int, int]:
    """Marks any scan or discovery left in 'running' state after an unclean exit."""
    t = now()
    with _lock, _conn() as c:
        s = c.execute("UPDATE scans SET status='interrupted', finished=? WHERE status='running'", (t,)).rowcount
        d = c.execute("UPDATE discoveries SET status='interrupted', finished=? WHERE status='running'", (t,)).rowcount
        c.commit()
        return s, d


def _rows(sql: str, args: Iterable = ()) -> List[Dict[str, Any]]:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(sql, tuple(args))]


def _one(sql: str, args: Iterable = ()) -> Optional[Dict[str, Any]]:
    r = _rows(sql, args)
    return r[0] if r else None


def _exec(sql: str, args: Iterable = ()) -> int:
    with _lock, _conn() as c:
        cur = c.execute(sql, tuple(args))
        c.commit()
        if sql.lstrip().upper().startswith("INSERT"):
            return cur.lastrowid or cur.rowcount
        return cur.rowcount


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
    with _lock, _conn() as c:
        c.executemany("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      [(k, str(v)) for k, v in values.items() if k in DEFAULTS])
        c.commit()


# ---------- watches ----------
def watches(active_only: bool = False) -> List[Dict[str, Any]]:
    where = " WHERE w.active=1" if active_only else ""
    return _rows(f"""SELECT w.*,
                            (SELECT COUNT(*) FROM sources s WHERE s.watch_id = w.id) AS source_count,
                            (SELECT COUNT(*) FROM filters f WHERE f.watch_id = w.id) AS filter_count
                     FROM watches w{where} ORDER BY w.id DESC""")


def watch_detail(wid: int) -> Optional[Dict[str, Any]]:
    w = _one("SELECT * FROM watches WHERE id=?", (wid,))
    if not w:
        return None
    w["sources"] = sources(watch_id=wid)
    w["filters"] = filters(watch_id=wid)
    return w


def add_watch(title: str, request: str = "", active: int = 1) -> int:
    clean = (title or "").strip()[:120]
    if not clean:
        raise ValueError("title is required")
    return _exec("INSERT INTO watches(title, request, created, active) VALUES(?, ?, ?, ?)",
                 (clean, (request or "").strip()[:2000], now(), int(bool(active))))


def update_watch(wid: int, **fields) -> bool:
    allowed: Dict[str, Any] = {}
    if "title" in fields:
        clean = str(fields["title"] or "").strip()[:120]
        if not clean:
            raise ValueError("title cannot be empty")
        allowed["title"] = clean
    if "request" in fields:
        allowed["request"] = str(fields["request"] or "").strip()[:2000]
    if "active" in fields:
        allowed["active"] = int(bool(fields["active"]))
    if not allowed:
        return False
    return _exec(f"UPDATE watches SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), wid)) > 0


def delete_watch(wid: int) -> bool:
    with _lock, _conn() as c:
        c.execute("DELETE FROM filters WHERE watch_id=?", (wid,))
        c.execute("DELETE FROM sources WHERE watch_id=?", (wid,))
        c.execute("UPDATE discoveries SET watch_id=NULL WHERE watch_id=?", (wid,))
        cur = c.execute("DELETE FROM watches WHERE id=?", (wid,))
        c.commit()
        return cur.rowcount > 0


# ---------- sources ----------
def sources(active_only: bool = False, watch_id: Any = _UNSET) -> List[Dict[str, Any]]:
    clauses, args = [], []
    if active_only:
        clauses.append("active=1")
    if watch_id is None:
        clauses.append("watch_id IS NULL")
    elif watch_id is not _UNSET:
        clauses.append("watch_id=?")
        args.append(watch_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    out = _rows(f"SELECT * FROM sources{where} ORDER BY name COLLATE NOCASE", args)
    for s in out:
        last = _one("SELECT ok, count, error, scan_id FROM scan_sources WHERE source_id=? ORDER BY scan_id DESC LIMIT 1",
                    (s["id"],))
        s["last"] = last
        s["open_items"] = _one(
            "SELECT COUNT(*) n FROM items WHERE source_id=? AND status='open' AND COALESCE(match,1)=1", (s["id"],))["n"]
    return out


def source_by_id(sid: int) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM sources WHERE id=?", (sid,))


def add_source(name: str, url: str, notes: str = "", method: str = "agent", plugin: Optional[str] = None,
               watch_id: Optional[int] = None) -> int:
    return _exec("INSERT INTO sources(name, url, notes, method, plugin, watch_id, created) VALUES(?, ?, ?, ?, ?, ?, ?)",
                 (name.strip()[:80], url.strip()[:500], notes.strip()[:1000], method, plugin, watch_id, now()))


def update_source(sid: int, **fields) -> bool:
    allowed = {k: v for k, v in fields.items() if k in ("name", "url", "notes", "active", "recipe", "extractor",
                                                        "extractor_ok", "extractor_error", "extractor_updated",
                                                        "extractor_runs", "watch_id")}
    if not allowed:
        return False
    return _exec(f"UPDATE sources SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), sid)) > 0


def delete_source(sid: int) -> bool:
    return _exec("DELETE FROM sources WHERE id=?", (sid,)) > 0


def source_by_plugin(plugin: str) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM sources WHERE method='plugin' AND plugin=?", (plugin,))


# ---------- filters ----------
FILTER_KINDS = ("include", "exclude", "type", "location", "deadline_within", "instruction")


def filters(active_only: bool = False, watch_id: Any = _UNSET) -> List[Dict[str, Any]]:
    clauses, args = [], []
    if active_only:
        clauses.append("active=1")
    if watch_id is None:
        clauses.append("watch_id IS NULL")
    elif watch_id is not _UNSET:
        clauses.append("watch_id=?")
        args.append(watch_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return _rows(f"SELECT * FROM filters{where} ORDER BY kind, id", args)


def filters_for_watch(watch_id: Optional[int], active_only: bool = True) -> List[Dict[str, Any]]:
    clauses = ["watch_id IS NULL"] if watch_id is None else ["(watch_id IS NULL OR watch_id=?)"]
    args = [] if watch_id is None else [watch_id]
    if active_only:
        clauses.append("active=1")
    return _rows(f"SELECT * FROM filters WHERE {' AND '.join(clauses)} ORDER BY watch_id IS NOT NULL, kind, id", args)


def add_filter(kind: str, value: str, watch_id: Optional[int] = None) -> int:
    val = (value or "").strip()[:500]
    if kind not in FILTER_KINDS or not val:
        raise ValueError("invalid filter")
    if kind == "deadline_within" and (not val.isdigit() or int(val) <= 0):
        raise ValueError("deadline_within must be a positive number of days")
    return _exec("INSERT INTO filters(kind, value, watch_id, created) VALUES(?, ?, ?, ?)",
                 (kind, val, watch_id, now()))


def update_filter(fid: int, active: bool) -> bool:
    return _exec("UPDATE filters SET active=? WHERE id=?", (int(active), fid)) > 0


def delete_filter(fid: int) -> bool:
    return _exec("DELETE FROM filters WHERE id=?", (fid,)) > 0


# ---------- scans ----------
def start_scan(trigger: str, agent: str, model: str, filter_snapshot: List[Dict[str, Any]],
               label: Optional[str] = None, watch_id: Optional[int] = None) -> int:
    return _exec("INSERT INTO scans(started, trigger, agent, model, filters, label, watch_id) VALUES(?, ?, ?, ?, ?, ?, ?)",
                 (now(), trigger, agent, model, json.dumps(filter_snapshot, ensure_ascii=False), label, watch_id))


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
    return _rows("SELECT id, started, finished, trigger, agent, model, status, found, new, note, label, watch_id "
                 "FROM scans ORDER BY id DESC LIMIT ?", (limit,))


def scan_detail(scan_id: int) -> Optional[Dict[str, Any]]:
    s = _one("SELECT * FROM scans WHERE id=?", (scan_id,))
    if not s:
        return None
    try:
        s["filters"] = json.loads(s["filters"] or "[]")
    except (TypeError, ValueError):
        s["filters"] = []
    s["sources"] = _rows("SELECT * FROM scan_sources WHERE scan_id=? ORDER BY ok, source_name", (scan_id,))
    s["items"] = _rows("SELECT i.*, si.is_new FROM scan_items si JOIN items i ON i.id = si.item_id "
                       "WHERE si.scan_id=? ORDER BY si.is_new DESC, i.deadline IS NULL, i.deadline", (scan_id,))
    return s


def delete_scan(scan_id: int) -> None:
    with _lock, _conn() as c:
        for t in ("scan_items", "scan_sources", "scans"):
            c.execute(f"DELETE FROM {t} WHERE {'id' if t == 'scans' else 'scan_id'}=?", (scan_id,))
        c.commit()


def running_scan() -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM scans WHERE status='running' ORDER BY id DESC LIMIT 1")


# ---------- discoveries ----------
def start_discovery(request: str, title: str, agent: str, model: str, watch_id: Optional[int] = None) -> int:
    return _exec("INSERT INTO discoveries(started, request, title, agent, model, watch_id) VALUES(?, ?, ?, ?, ?, ?)",
                 (now(), request, title, agent, model, watch_id))


def finish_discovery(discovery_id: int, status: str, results: List[Dict[str, Any]], error: str = "",
                     title: Optional[str] = None, watch_id: Any = _UNSET) -> None:
    raw = json.dumps(results or [], ensure_ascii=False)
    sets = ["finished=?", "status=?", "results=?", "error=?"]
    args: List[Any] = [now(), status, raw, error]
    if title:
        sets.append("title=?")
        args.append(title)
    if watch_id is not _UNSET:
        sets.append("watch_id=?")
        args.append(watch_id)
    args.append(discovery_id)
    _exec(f"UPDATE discoveries SET {', '.join(sets)} WHERE id=?", args)


def discoveries(limit: int = 60) -> List[Dict[str, Any]]:
    rows = _rows("SELECT id, started, finished, request, title, status, agent, model, results, error, watch_id "
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
    wid = source.get("watch_id")
    with _lock, _conn() as c:
        exists = c.execute("SELECT id FROM items WHERE id=?", (it["id"],)).fetchone()
        if exists:
            c.execute("""UPDATE items SET title=?, org=?, category=?, url=?, deadline=?, start_date=?, location=?,
                         summary=?, extra=?, watch_id=?, status='open', last_scan=?, last_seen=? WHERE id=?""",
                      (it["title"], it.get("org"), it.get("category"), it["url"], it.get("deadline"),
                       it.get("start_date"), it.get("location"), it.get("summary"), it.get("extra"), wid, scan_id, t,
                       it["id"]))
        else:
            c.execute("""INSERT INTO items(id, source_id, source_name, title, org, category, url, deadline, start_date,
                         location, summary, extra, watch_id, first_scan, last_scan, first_seen, last_seen)
                         VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (it["id"], source.get("id"), source["name"], it["title"], it.get("org"), it.get("category"),
                       it["url"], it.get("deadline"), it.get("start_date"), it.get("location"), it.get("summary"),
                       it.get("extra"), wid, scan_id, scan_id, t, t))
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
    return _rows("""SELECT i.*, COALESCE(s.watch_id, i.watch_id) AS watch_id
                    FROM items i LEFT JOIN sources s ON s.id = i.source_id
                    ORDER BY i.first_seen DESC""")


def set_item(item_id: str, **fields) -> bool:
    allowed = {k: v for k, v in fields.items() if k in ("action", "note", "remind_on", "snoozed_until")}
    if not allowed:
        return False
    return _exec(f"UPDATE items SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), item_id)) > 0


def unnotified() -> List[Dict[str, Any]]:
    today = now()[:10]
    return _rows("""SELECT * FROM items
                    WHERE notified=0 AND status='open' AND COALESCE(match,1)=1
                    AND (action IS NULL OR action!='hidden')
                    AND (snoozed_until IS NULL OR snoozed_until <= ?)""", (today,))


def mark_notified(ids: Iterable[str]) -> None:
    with _lock, _conn() as c:
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


def items_needing_match(scan_id: int, key: str, watch_id: Any = _UNSET) -> List[Dict[str, Any]]:
    if watch_id is _UNSET:
        return _rows("""SELECT i.* FROM scan_items si JOIN items i ON i.id = si.item_id
                        WHERE si.scan_id=? AND (i.match_key IS NULL OR i.match_key != ?)""", (scan_id, key))
    return _rows("""SELECT i.* FROM scan_items si
                    JOIN items i ON i.id = si.item_id
                    LEFT JOIN sources s ON s.id = i.source_id
                    WHERE si.scan_id=? AND COALESCE(s.watch_id, i.watch_id) IS ?
                    AND (i.match_key IS NULL OR i.match_key != ?)""", (scan_id, watch_id, key))


def set_matches(decisions: Dict[str, bool], key: str) -> None:
    with _lock, _conn() as c:
        c.executemany("UPDATE items SET match=?, match_key=? WHERE id=?",
                      [(int(v), key, k) for k, v in decisions.items()])
        c.commit()
