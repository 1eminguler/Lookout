"""SQLite storage: watches, sources, filters, scans, discoveries, items, history, locks, settings and agent usage."""
import ctypes
import json
import os
import sqlite3
import sys
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LOOKOUT_DATA_DIR") or ROOT / "data") / "lookout.db"
TS = "%Y-%m-%d %H:%M:%S"
SCHEMA_VERSION = 2
_lock = threading.RLock()
_UNSET = object()
_TRACKING_EXACT = {"fbclid", "gclid", "mc_cid", "mc_eid"}

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

CREATE TABLE IF NOT EXISTS url_readers (
    url_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    extractor TEXT NOT NULL,
    last_good_extractor TEXT,
    extractor_ok INTEGER DEFAULT 1,
    extractor_error TEXT,
    extractor_updated TEXT,
    extractor_runs INTEGER DEFAULT 0,
    last_good_stats TEXT
);

CREATE TABLE IF NOT EXISTS item_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    scan_id INTEGER,
    changed_at TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- deadline_moved | closed | reopened | updated
    field TEXT,
    old_value TEXT,
    new_value TEXT
);

CREATE TABLE IF NOT EXISTS scan_locks (
    name TEXT PRIMARY KEY,
    owner_pid INTEGER NOT NULL,
    owner_token TEXT NOT NULL,
    scan_id INTEGER,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().strftime(TS)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
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

MIGRATIONS_V2 = {
    "sources": {
        "last_good_extractor": "TEXT",
        "last_good_updated": "TEXT",
        "last_good_stats": "TEXT",
        "fail_count": "INTEGER DEFAULT 0",
        "retry_after": "TEXT",
        "last_error_kind": "TEXT",
        "filter_hash": "TEXT",
    },
    "items": {
        "updated_at": "TEXT",
        "change_summary": "TEXT",
        "last_change_kind": "TEXT",
    },
    "scan_items": {
        "is_updated": "INTEGER DEFAULT 0",
    },
}

INDEXES_V2 = """
CREATE INDEX IF NOT EXISTS idx_item_history_item ON item_history(item_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_items_source_id ON items(source_id);
CREATE INDEX IF NOT EXISTS idx_items_watch_id ON items(watch_id);
CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_scan_sources_source_id ON scan_sources(source_id, scan_id DESC);
CREATE INDEX IF NOT EXISTS idx_sources_watch_id ON sources(watch_id);
"""


def _add_columns(c: sqlite3.Connection, spec: Dict[str, Dict[str, str]]) -> None:
    for table, cols in spec.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def init() -> None:
    with _lock, _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        ver = int(c.execute("PRAGMA user_version").fetchone()[0] or 0)
        if ver < 1:
            _add_columns(c, MIGRATIONS)
            ver = 1
        if ver < 2:
            _add_columns(c, MIGRATIONS_V2)
            c.execute(
                "UPDATE sources SET last_good_extractor = extractor, "
                "last_good_updated = COALESCE(last_good_updated, extractor_updated) "
                "WHERE extractor_ok = 1 AND extractor IS NOT NULL AND last_good_extractor IS NULL"
            )
            c.executescript(INDEXES_V2)
            ver = 2
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        c.commit()


# ---------- cross-process locks & recovery ----------
def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            k32.GetExitCodeProcess.restype = ctypes.c_int
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.CloseHandle.restype = ctypes.c_int
            handle = k32.OpenProcess(0x1000, 0, int(pid))
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == 259
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _is_lock_live(row: Dict[str, Any], stale_seconds: int = 120) -> bool:
    if not row or not _pid_alive(int(row.get("owner_pid") or 0)):
        return False
    hb = row.get("heartbeat_at") or ""
    try:
        dt = datetime.strptime(hb, TS)
    except ValueError:
        return False
    return (datetime.now() - dt).total_seconds() <= stale_seconds


def active_scan_lock(name: str = "scan", stale_seconds: int = 120) -> Optional[Dict[str, Any]]:
    try:
        row = _one("SELECT * FROM scan_locks WHERE name=?", (name,))
    except sqlite3.OperationalError:
        return None
    if row and _is_lock_live(row, stale_seconds=stale_seconds):
        return row
    return None


def acquire_scan_lock(owner_token: str, name: str = "scan", pid: Optional[int] = None,
                      stale_seconds: int = 120) -> bool:
    owner_pid = os.getpid() if pid is None else int(pid)
    for attempt in range(2):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM scan_locks WHERE name=?", (name,)).fetchone()
            t = now()
            if row:
                info = dict(row)
                if _is_lock_live(info, stale_seconds=stale_seconds):
                    if info["owner_token"] == owner_token:
                        conn.execute("UPDATE scan_locks SET heartbeat_at=? WHERE name=?", (t, name))
                        conn.execute("COMMIT")
                        return True
                    conn.execute("ROLLBACK")
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO scan_locks(name, owner_pid, owner_token, scan_id, acquired_at, heartbeat_at) "
                "VALUES(?, ?, ?, NULL, ?, ?)",
                (name, owner_pid, owner_token, t, t),
            )
            conn.execute("COMMIT")
            return True
        except sqlite3.OperationalError as e:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if attempt == 0 and "no such table" in str(e).lower():
                conn.close()
                init()
                continue
            raise
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
    return False


def update_scan_lock(owner_token: str, scan_id: Optional[int] = None, name: str = "scan") -> bool:
    t = now()
    if scan_id is not None:
        return _exec(
            "UPDATE scan_locks SET heartbeat_at=?, scan_id=? WHERE name=? AND owner_token=?",
            (t, scan_id, name, owner_token),
        ) > 0
    return _exec(
        "UPDATE scan_locks SET heartbeat_at=? WHERE name=? AND owner_token=?",
        (t, name, owner_token),
    ) > 0


def release_scan_lock(owner_token: str, name: str = "scan") -> None:
    try:
        _exec("DELETE FROM scan_locks WHERE name=? AND owner_token=?", (name, owner_token))
    except sqlite3.OperationalError:
        pass


def mark_interrupted() -> Tuple[int, int]:
    """Marks any scan or discovery left in 'running' state after an unclean exit, ignoring live DB locks."""
    t = now()
    scan_lock = active_scan_lock("scan")
    disc_lock = active_scan_lock("discover")
    with _lock, _conn() as c:
        if scan_lock is not None:
            if scan_lock.get("scan_id") is not None:
                s = c.execute(
                    "UPDATE scans SET status='interrupted', finished=? WHERE status='running' AND id != ?",
                    (t, scan_lock["scan_id"]),
                ).rowcount
            else:
                s = c.execute(
                    "UPDATE scans SET status='interrupted', finished=? WHERE status='running' AND started < ?",
                    (t, scan_lock["acquired_at"]),
                ).rowcount
        else:
            s = c.execute("UPDATE scans SET status='interrupted', finished=? WHERE status='running'", (t,)).rowcount

        if disc_lock is not None:
            if disc_lock.get("scan_id") is not None:
                d = c.execute(
                    "UPDATE discoveries SET status='interrupted', finished=? WHERE status='running' AND id != ?",
                    (t, disc_lock["scan_id"]),
                ).rowcount
            else:
                d = c.execute(
                    "UPDATE discoveries SET status='interrupted', finished=? WHERE status='running' AND started < ?",
                    (t, disc_lock["acquired_at"]),
                ).rowcount
        else:
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
    "rss_public": "0", "webhook_url": "", "webhook_allow_local": "0",
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


def watch_title(wid: int) -> Optional[str]:
    r = _one("SELECT title FROM watches WHERE id=?", (wid,))
    return r["title"] if r else None


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
        c.execute(
            "DELETE FROM item_history WHERE item_id IN ("
            "SELECT id FROM items WHERE watch_id=? OR source_id IN (SELECT id FROM sources WHERE watch_id=?))",
            (wid, wid),
        )
        c.execute(
            "DELETE FROM scan_items WHERE scan_id IN (SELECT id FROM scans WHERE watch_id=?) "
            "OR item_id IN (SELECT id FROM items WHERE watch_id=? OR source_id IN (SELECT id FROM sources WHERE watch_id=?))",
            (wid, wid, wid),
        )
        c.execute(
            "DELETE FROM items WHERE watch_id=? OR source_id IN (SELECT id FROM sources WHERE watch_id=?)",
            (wid, wid),
        )
        c.execute(
            "DELETE FROM scan_sources WHERE scan_id IN (SELECT id FROM scans WHERE watch_id=?) "
            "OR source_id IN (SELECT id FROM sources WHERE watch_id=?)",
            (wid, wid),
        )
        c.execute("DELETE FROM scans WHERE watch_id=?", (wid,))
        c.execute("DELETE FROM filters WHERE watch_id=?", (wid,))
        c.execute("DELETE FROM sources WHERE watch_id=?", (wid,))
        c.execute("DELETE FROM discoveries WHERE watch_id=?", (wid,))
        cur = c.execute("DELETE FROM watches WHERE id=?", (wid,))
        c.commit()
        return cur.rowcount > 0


# ---------- shared URL readers ----------
def canonical_reader_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parts = urllib.parse.urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return raw.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    kept = sorted((k, v) for k, v in pairs if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_EXACT)
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def get_shared_reader(url: str) -> Optional[Dict[str, Any]]:
    key = canonical_reader_url(url)
    if not key:
        return None
    row = _one("SELECT * FROM url_readers WHERE url_key=? AND extractor_ok=1 AND extractor IS NOT NULL", (key,))
    if row:
        return row
    for s in _rows("SELECT id, url, extractor, last_good_extractor, extractor_ok, extractor_error, "
                   "extractor_updated, last_good_updated, extractor_runs, last_good_stats "
                   "FROM sources WHERE method='agent' AND extractor_ok=1 AND extractor IS NOT NULL "
                   "ORDER BY COALESCE(extractor_updated, '') DESC, id DESC"):
        if canonical_reader_url(s["url"]) == key:
            return {
                "url_key": key,
                "url": s["url"],
                "extractor": s["extractor"],
                "last_good_extractor": s.get("last_good_extractor") or s["extractor"],
                "extractor_ok": 1,
                "extractor_error": s.get("extractor_error"),
                "extractor_updated": s.get("extractor_updated"),
                "extractor_runs": s.get("extractor_runs") or 0,
                "last_good_stats": s.get("last_good_stats"),
            }
    return None


def save_shared_reader(url: str, extractor: str, ok: bool = True, error: Optional[str] = None,
                       stats: Optional[str] = None, updated: Optional[str] = None,
                       propagate: bool = True) -> None:
    key = canonical_reader_url(url)
    if not key or not extractor:
        return
    ts = updated or now()
    with _lock, _conn() as c:
        existing = c.execute("SELECT * FROM url_readers WHERE url_key=?", (key,)).fetchone()
        last_good = extractor if ok else (existing["last_good_extractor"] if existing else None)
        eff_stats = stats if stats is not None else (existing["last_good_stats"] if existing else None)
        c.execute(
            """INSERT INTO url_readers(url_key, url, extractor, last_good_extractor, extractor_ok,
                                       extractor_error, extractor_updated, extractor_runs, last_good_stats)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url_key) DO UPDATE SET
                   url=excluded.url,
                   extractor=CASE WHEN excluded.extractor_ok=1 THEN excluded.extractor ELSE url_readers.extractor END,
                   last_good_extractor=CASE WHEN excluded.extractor_ok=1 THEN excluded.extractor ELSE url_readers.last_good_extractor END,
                   extractor_ok=CASE WHEN excluded.extractor_ok=1 THEN 1 ELSE url_readers.extractor_ok END,
                   extractor_error=excluded.extractor_error,
                   extractor_updated=excluded.extractor_updated,
                   extractor_runs=CASE WHEN excluded.extractor_ok=1 THEN url_readers.extractor_runs + 1 ELSE url_readers.extractor_runs END,
                   last_good_stats=COALESCE(excluded.last_good_stats, url_readers.last_good_stats)""",
            (key, str(url)[:500], extractor, last_good, int(bool(ok)), error, ts, 1 if ok else 0, eff_stats),
        )
        if ok and propagate:
            rows = c.execute("SELECT id, url FROM sources WHERE method='agent'").fetchall()
            match_ids = [r["id"] for r in rows if canonical_reader_url(r["url"]) == key]
            for sid in match_ids:
                c.execute(
                    """UPDATE sources SET extractor=?, last_good_extractor=?, extractor_ok=1,
                       extractor_error=NULL, extractor_updated=?, last_good_updated=?,
                       last_good_stats=COALESCE(?, last_good_stats),
                       fail_count=0, retry_after=NULL, last_error_kind=NULL
                       WHERE id=?""",
                    (extractor, extractor, ts, ts, eff_stats, sid),
                )
        c.commit()


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
    with _lock, _conn() as c:
        out = [dict(r) for r in c.execute(f"SELECT * FROM sources{where} ORDER BY name COLLATE NOCASE", args)]
        if not out:
            return out
        sids = [s["id"] for s in out]
        placeholders = ",".join("?" for _ in sids)
        last_rows = c.execute(
            f"""SELECT ss.source_id, ss.ok, ss.count, ss.error, ss.scan_id
                FROM scan_sources ss
                JOIN (
                    SELECT source_id, MAX(scan_id) AS max_scan_id
                    FROM scan_sources
                    WHERE source_id IN ({placeholders})
                    GROUP BY source_id
                ) latest ON latest.source_id = ss.source_id AND latest.max_scan_id = ss.scan_id""",
            sids,
        ).fetchall()
        last_map = {
            r["source_id"]: {"ok": r["ok"], "count": r["count"], "error": r["error"], "scan_id": r["scan_id"]}
            for r in last_rows
        }
        open_rows = c.execute(
            f"SELECT source_id, COUNT(*) AS n FROM items "
            f"WHERE source_id IN ({placeholders}) AND status='open' AND COALESCE(match,1)=1 GROUP BY source_id",
            sids,
        ).fetchall()
        open_map = {r["source_id"]: r["n"] for r in open_rows}
        for s in out:
            s["last"] = last_map.get(s["id"])
            s["open_items"] = open_map.get(s["id"], 0)
    return out


def source_by_id(sid: int) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM sources WHERE id=?", (sid,))


def source_open_item_count(sid: int) -> int:
    r = _one("SELECT COUNT(*) n FROM items WHERE source_id=? AND status='open'", (sid,))
    return int(r["n"]) if r else 0


def add_source(name: str, url: str, notes: str = "", method: str = "agent", plugin: Optional[str] = None,
               watch_id: Optional[int] = None) -> int:
    clean_url = url.strip()[:500]
    sid = _exec("INSERT INTO sources(name, url, notes, method, plugin, watch_id, created) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (name.strip()[:80], clean_url, notes.strip()[:1000], method, plugin, watch_id, now()))
    if method == "agent":
        shared = get_shared_reader(clean_url)
        if shared and shared.get("extractor"):
            ts = shared.get("extractor_updated") or now()
            update_source(
                sid,
                extractor=shared["extractor"],
                last_good_extractor=shared.get("last_good_extractor") or shared["extractor"],
                extractor_ok=1,
                extractor_error=None,
                extractor_updated=ts,
                last_good_updated=ts,
                last_good_stats=shared.get("last_good_stats"),
            )
    return sid


_SOURCE_UPDATABLE = {
    "name", "url", "notes", "active", "recipe", "extractor", "extractor_ok", "extractor_error",
    "extractor_updated", "extractor_runs", "watch_id", "last_good_extractor", "last_good_updated",
    "last_good_stats", "fail_count", "retry_after", "last_error_kind", "filter_hash",
}


def update_source(sid: int, **fields) -> bool:
    allowed = {k: v for k, v in fields.items() if k in _SOURCE_UPDATABLE}
    if not allowed:
        return False
    return _exec(f"UPDATE sources SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), sid)) > 0


def delete_source(sid: int) -> bool:
    with _lock, _conn() as c:
        c.execute("DELETE FROM item_history WHERE item_id IN (SELECT id FROM items WHERE source_id=?)", (sid,))
        c.execute("DELETE FROM scan_items WHERE item_id IN (SELECT id FROM items WHERE source_id=?)", (sid,))
        c.execute("DELETE FROM items WHERE source_id=?", (sid,))
        c.execute("DELETE FROM scan_sources WHERE source_id=?", (sid,))
        cur = c.execute("DELETE FROM sources WHERE id=?", (sid,))
        c.commit()
        return cur.rowcount > 0


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


def refresh_scan_counts(scan_id: int) -> None:
    q = "SELECT COUNT(*) n FROM scan_items si JOIN items i ON i.id=si.item_id WHERE si.scan_id=? AND COALESCE(i.match,1)=1"
    with _lock, _conn() as c:
        found = c.execute(q, (scan_id,)).fetchone()["n"]
        new = c.execute(q + " AND si.is_new=1", (scan_id,)).fetchone()["n"]
        c.execute("UPDATE scans SET found=?, new=? WHERE id=?", (found, new, scan_id))
        c.commit()


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
    s["items"] = _rows("SELECT i.*, si.is_new, COALESCE(si.is_updated, 0) AS is_updated "
                       "FROM scan_items si JOIN items i ON i.id = si.item_id "
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


# ---------- items & history ----------
_TRACKED_ITEM_FIELDS = ("title", "deadline", "start_date", "location", "org", "category", "summary", "extra")


def _norm_field(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _build_change_summary(reopened: bool, changes: List[Tuple[str, Optional[str], Optional[str]]]) -> Tuple[str, str]:
    parts: List[str] = []
    if reopened:
        parts.append("Reopened")
    dl_change = next((c for c in changes if c[0] == "deadline"), None)
    if dl_change:
        _, old_dl, new_dl = dl_change
        if old_dl and new_dl:
            parts.append(f"Deadline moved {old_dl} → {new_dl}")
        elif new_dl:
            parts.append(f"Deadline set to {new_dl}")
        else:
            parts.append("Deadline removed")
    others = [f for f, _, _ in changes if f != "deadline"]
    if others:
        parts.append("Updated " + ", ".join(others[:4]))
    kind = "deadline_moved" if dl_change else ("reopened" if reopened else "updated")
    return "; ".join(parts)[:240], kind


def save_item(scan_id: int, source: Dict[str, Any], it: Dict[str, Any]) -> bool:
    """Insert or refresh an item, recording field history. Never resurrects closed items as new."""
    t = now()
    wid = source.get("watch_id")
    src_id = source.get("id")
    with _lock, _conn() as c:
        exists = c.execute("SELECT * FROM items WHERE id=?", (it["id"],)).fetchone()
        if exists is None and it.get("url"):
            listing_key = canonical_reader_url(source.get("url") or "")
            item_key = canonical_reader_url(it["url"])
            if item_key and item_key != listing_key:
                if src_id is not None:
                    exists = c.execute(
                        "SELECT * FROM items WHERE source_id=? AND url=? LIMIT 1", (src_id, it["url"])
                    ).fetchone()
                else:
                    exists = c.execute(
                        "SELECT * FROM items WHERE source_name=? AND url=? LIMIT 1", (source["name"], it["url"])
                    ).fetchone()
        if exists:
            ex = dict(exists)
            item_id = ex["id"]
            reopened = ex.get("status") == "gone"
            changes: List[Tuple[str, Optional[str], Optional[str]]] = []
            for field in _TRACKED_ITEM_FIELDS:
                old_v = _norm_field(ex.get(field))
                new_v = _norm_field(it.get(field))
                if old_v != new_v:
                    changes.append((field, old_v, new_v))
            hist_rows: List[Tuple[str, int, str, str, str, Optional[str], Optional[str]]] = []
            if reopened:
                hist_rows.append((item_id, scan_id, t, "reopened", "status", "gone", "open"))
            for field, old_v, new_v in changes:
                hist_kind = "deadline_moved" if field == "deadline" else "updated"
                hist_rows.append((item_id, scan_id, t, hist_kind, field, old_v, new_v))
            if hist_rows:
                c.executemany(
                    "INSERT INTO item_history(item_id, scan_id, changed_at, kind, field, old_value, new_value) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    hist_rows,
                )
                summary, change_kind = _build_change_summary(reopened, changes)
                updated_at = t
            else:
                summary = ex.get("change_summary")
                change_kind = ex.get("last_change_kind")
                updated_at = ex.get("updated_at")
            c.execute(
                """UPDATE items SET title=?, org=?, category=?, url=?, deadline=?, start_date=?, location=?,
                   summary=?, extra=?, watch_id=?, status='open', last_scan=?, last_seen=?,
                   updated_at=?, change_summary=?, last_change_kind=? WHERE id=?""",
                (it["title"], it.get("org"), it.get("category"), it["url"], it.get("deadline"),
                 it.get("start_date"), it.get("location"), it.get("summary"), it.get("extra"), wid, scan_id, t,
                 updated_at, summary, change_kind, item_id),
            )
            c.execute(
                "INSERT OR REPLACE INTO scan_items(scan_id, item_id, is_new, is_updated) VALUES(?, ?, 0, ?)",
                (scan_id, item_id, 1 if hist_rows else 0),
            )
            c.commit()
            return False

        item_id = it["id"]
        c.execute(
            """INSERT INTO items(id, source_id, source_name, title, org, category, url, deadline, start_date,
               location, summary, extra, watch_id, first_scan, last_scan, first_seen, last_seen)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, src_id, source["name"], it["title"], it.get("org"), it.get("category"),
             it["url"], it.get("deadline"), it.get("start_date"), it.get("location"), it.get("summary"),
             it.get("extra"), wid, scan_id, scan_id, t, t),
        )
        c.execute("INSERT OR REPLACE INTO scan_items(scan_id, item_id, is_new, is_updated) VALUES(?, ?, 1, 0)",
                  (scan_id, item_id))
        c.commit()
        return True


def mark_gone(source: Dict[str, Any], scan_id: int) -> int:
    """Items a successfully scanned source no longer lists are marked gone and recorded in history."""
    t = now()
    with _lock, _conn() as c:
        if source.get("id") is None:
            gone_rows = c.execute(
                "SELECT id FROM items WHERE source_name=? AND status='open' AND last_scan<?",
                (source["name"], scan_id),
            ).fetchall()
        else:
            gone_rows = c.execute(
                "SELECT id FROM items WHERE source_id=? AND status='open' AND last_scan<?",
                (source["id"], scan_id),
            ).fetchall()
        if not gone_rows:
            return 0
        ids = [r["id"] for r in gone_rows]
        placeholders = ",".join("?" for _ in ids)
        c.execute(
            f"UPDATE items SET status='gone', updated_at=?, change_summary='Closed', last_change_kind='closed' "
            f"WHERE id IN ({placeholders})",
            [t, *ids],
        )
        c.executemany(
            "INSERT INTO item_history(item_id, scan_id, changed_at, kind, field, old_value, new_value) "
            "VALUES(?, ?, ?, 'closed', 'status', 'open', 'gone')",
            [(iid, scan_id, t) for iid in ids],
        )
        c.commit()
        return len(ids)


def items(limit: Optional[int] = None, offset: int = 0, watch_id: Any = _UNSET) -> List[Dict[str, Any]]:
    clauses, args = [], []
    if watch_id is None:
        clauses.append("COALESCE(s.watch_id, i.watch_id) IS NULL")
    elif watch_id is not _UNSET:
        clauses.append("COALESCE(s.watch_id, i.watch_id)=?")
        args.append(watch_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (f"SELECT i.*, COALESCE(s.watch_id, i.watch_id) AS watch_id "
           f"FROM items i LEFT JOIN sources s ON s.id = i.source_id{where} ORDER BY i.first_seen DESC")
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args.extend([max(1, int(limit)), max(0, int(offset))])
    return _rows(sql, args)


def items_count(watch_id: Any = _UNSET) -> int:
    if watch_id is _UNSET:
        return int(_one("SELECT COUNT(*) n FROM items")["n"])
    if watch_id is None:
        return int(_one("SELECT COUNT(*) n FROM items i LEFT JOIN sources s ON s.id = i.source_id "
                        "WHERE COALESCE(s.watch_id, i.watch_id) IS NULL")["n"])
    return int(_one("SELECT COUNT(*) n FROM items i LEFT JOIN sources s ON s.id = i.source_id "
                    "WHERE COALESCE(s.watch_id, i.watch_id)=?", (watch_id,))["n"])


def agenda_items() -> List[Dict[str, Any]]:
    return _rows("SELECT i.*, COALESCE(s.watch_id, i.watch_id) AS watch_id "
                 "FROM items i LEFT JOIN sources s ON s.id = i.source_id "
                 "WHERE i.action IN ('saved', 'applied') ORDER BY i.first_seen DESC")


def watch_feed_items(watch_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    return _rows(
        """SELECT i.*, COALESCE(s.watch_id, i.watch_id) AS watch_id
           FROM items i LEFT JOIN sources s ON s.id = i.source_id
           WHERE COALESCE(s.watch_id, i.watch_id)=?
           AND i.status='open' AND COALESCE(i.match, 1)=1
           AND (i.action IS NULL OR i.action != 'hidden')
           ORDER BY COALESCE(i.updated_at, i.first_seen) DESC, i.first_seen DESC
           LIMIT ?""",
        (watch_id, max(1, int(limit))),
    )


def item_by_id(item_id: str) -> Optional[Dict[str, Any]]:
    return _one("SELECT i.*, COALESCE(s.watch_id, i.watch_id) AS watch_id "
                "FROM items i LEFT JOIN sources s ON s.id = i.source_id WHERE i.id=?", (item_id,))


def item_history(item_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    return _rows("SELECT * FROM item_history WHERE item_id=? ORDER BY id DESC LIMIT ?", (item_id, max(1, int(limit))))


def scan_changes(scan_id: int) -> List[Dict[str, Any]]:
    return _rows(
        """SELECT i.*, si.is_new, COALESCE(si.is_updated, 0) AS is_updated
           FROM scan_items si JOIN items i ON i.id = si.item_id
           WHERE si.scan_id=? AND COALESCE(i.match, 1)=1
           AND (si.is_new=1 OR COALESCE(si.is_updated, 0)=1)
           ORDER BY si.is_new DESC, i.first_seen DESC""",
        (scan_id,),
    )


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
