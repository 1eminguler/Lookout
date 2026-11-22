"""SQLite storage: watches, sources, filters, scans, discoveries, items, history, locks, settings and agent usage."""
import ctypes
import hashlib
import json
import os
import sqlite3
import sys
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LOOKOUT_DATA_DIR") or ROOT / "data") / "lookout.db"
TS = "%Y-%m-%d %H:%M:%S"
SCHEMA_VERSION = 8
MAX_DELIVERY_ATTEMPTS = 4  # Initial delivery plus up to three retries.
_lock = threading.RLock()
_UNSET = object()
_TRACKING_EXACT = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def _tr_lower(v: Any) -> str:
    return str(v or "").replace("İ", "i").replace("I\u0307", "i").lower()

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
    expired INTEGER DEFAULT 0,            -- expiry decision: deadline passed, closed marker or stale year
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

CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    channel TEXT NOT NULL,
    ok INTEGER NOT NULL,
    message TEXT,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    event TEXT NOT NULL CHECK (event IN ('new', 'updated', 'reminder')),
    event_key TEXT NOT NULL,
    channel TEXT NOT NULL,
    delivered_at TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    snapshot TEXT,
    UNIQUE (item_id, event, event_key, channel)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_pending ON deliveries(channel, event, id) WHERE delivered_at IS NULL;
"""


def now() -> str:
    return datetime.now().strftime(TS)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("norm_tr", 1, _tr_lower)
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

MIGRATIONS_V3 = {
    "sources": {
        "struct_fail_count": "INTEGER DEFAULT 0",
        "learn_fail_count": "INTEGER DEFAULT 0",
        "learn_retry_after": "TEXT",
        "paused": "INTEGER DEFAULT 0",
        "filters_changed": "INTEGER DEFAULT 0",
        "baseline_done": "INTEGER DEFAULT 0",
    },
    "url_readers": {
        "context_hash": "TEXT",
    },
}

INDEXES_V3 = """
CREATE INDEX IF NOT EXISTS idx_items_status_match ON items(status, match, first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_notification_log_channel ON notification_log(channel, id DESC);
"""

MIGRATIONS_V4 = {
    "items": {"expired": "INTEGER DEFAULT 0"},
}

MIGRATIONS_V5 = {
    "sources": {"empty_read_count": "INTEGER NOT NULL DEFAULT 0"},
}

MIGRATIONS_V6 = {
    "items": {"deadline_raw": "TEXT"},
}

MIGRATIONS_V7 = {
    "items": {"reminded_at": "TEXT"},
    "sources": {"last_successful_read": "TEXT"},
    "scans": {"sources_ok": "INTEGER NOT NULL DEFAULT 0", "sources_failed": "INTEGER NOT NULL DEFAULT 0",
              "sources_skipped": "INTEGER NOT NULL DEFAULT 0"},
}


def _migrate_scan_coverage(c: sqlite3.Connection) -> None:
    # Names can repeat across watches. Key source results by ID when available.
    c.execute("ALTER TABLE scan_sources RENAME TO scan_sources_v6")
    c.execute("""CREATE TABLE scan_sources (
        scan_id INTEGER NOT NULL, source_id INTEGER, source_name TEXT NOT NULL, ok INTEGER,
        count INTEGER DEFAULT 0, new INTEGER DEFAULT 0, error TEXT, ms INTEGER DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0, error_kind TEXT, last_successful_read TEXT
    )""")
    c.execute("""INSERT INTO scan_sources(scan_id, source_id, source_name, ok, count, new, error, ms, skipped)
                 SELECT scan_id, source_id, source_name, ok, count, new, error, ms,
                        CASE WHEN error='cancelled' OR error LIKE '%cooldown%' OR error LIKE '%retry backoff%'
                             THEN 1 ELSE 0 END
                 FROM scan_sources_v6""")
    c.execute("DROP TABLE scan_sources_v6")
    c.execute("CREATE UNIQUE INDEX idx_scan_sources_identity ON scan_sources"
              "(scan_id, COALESCE(source_id, 0), CASE WHEN source_id IS NULL THEN source_name ELSE '' END)")
    c.execute("CREATE INDEX idx_scan_sources_source_id ON scan_sources(source_id, scan_id DESC)")
    c.execute("""UPDATE scan_sources AS result SET last_successful_read=(
                   SELECT COALESCE(s.finished, s.started) FROM scan_sources prev JOIN scans s ON s.id=prev.scan_id
                   WHERE prev.source_id=result.source_id AND prev.ok=1 AND prev.scan_id<=result.scan_id
                   ORDER BY prev.scan_id DESC LIMIT 1
                 )""")
    c.execute("""UPDATE sources SET last_successful_read=(
                   SELECT r.last_successful_read FROM scan_sources r WHERE r.source_id=sources.id AND r.ok=1
                   ORDER BY r.scan_id DESC LIMIT 1
                 )""")
    c.execute("""UPDATE scans SET (sources_ok, sources_failed, sources_skipped)=(
                   SELECT COUNT(CASE WHEN ok=1 THEN 1 END), COUNT(CASE WHEN ok=0 AND skipped=0 THEN 1 END),
                          COUNT(CASE WHEN skipped=1 THEN 1 END) FROM scan_sources WHERE scan_id=scans.id
                 )""")

MIGRATIONS_V8 = {
    "sources": {"empty_reason": "TEXT"},
}


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
        if ver < 3:
            _add_columns(c, MIGRATIONS_V3)
            c.executescript(INDEXES_V3)
            c.execute(
                """UPDATE sources SET baseline_done = 1
                   WHERE baseline_done = 0 AND (
                       EXISTS (SELECT 1 FROM scan_sources ss WHERE ss.source_id = sources.id AND ss.ok = 1)
                       OR EXISTS (SELECT 1 FROM items i WHERE i.source_id = sources.id)
                   )"""
            )
            ver = 3
        if ver < 4:
            _add_columns(c, MIGRATIONS_V4)
            ver = 4
        if ver < 5:
            _add_columns(c, MIGRATIONS_V5)
        if ver < 6:
            _add_columns(c, MIGRATIONS_V6)
        if ver < 7:
            if not c.in_transaction:
                c.execute("BEGIN IMMEDIATE")
            if c.execute("PRAGMA user_version").fetchone()[0] < 7:
                _add_columns(c, MIGRATIONS_V7)
                _migrate_scan_coverage(c)
        if ver < 8:
            _add_columns(c, MIGRATIONS_V8)
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
    "custom_command": "", "mail_to": "", "mail_from": "", "smtp_host": "smtp.gmail.com", "smtp_port": "587",
    "smtp_user": "", "smtp_password": "", "smtp_starttls": "auto", "language_hint": "", "verify_weekly": "1",
    "rss_public": "0", "webhook_url": "", "webhook_format": "auto", "webhook_allow_local": "0",
    "telegram_chat_id": "", "notify_errors_dismissed_id": "0", "expired_cleanup_v1": "0",
    "expired_cleanup_v2": "0",
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
        item_scope = "(watch_id=? OR source_id IN (SELECT id FROM sources WHERE watch_id=?))"
        tracked_scope = f"{item_scope} AND (COALESCE(action,'') IN ('saved','applied') OR COALESCE(note,'')!='' OR remind_on IS NOT NULL)"
        c.execute(f"DELETE FROM scan_items WHERE item_id IN (SELECT id FROM items WHERE {item_scope})", (wid, wid))
        c.execute(f"UPDATE items SET source_id=NULL, watch_id=NULL, action=CASE WHEN action IN ('saved','applied') THEN action ELSE 'saved' END WHERE {tracked_scope}", (wid, wid))
        c.execute(
            "DELETE FROM item_history WHERE item_id IN ("
            f"SELECT id FROM items WHERE {item_scope} AND NOT (COALESCE(action,'') IN ('saved','applied') OR COALESCE(note,'')!='' OR remind_on IS NOT NULL))",
            (wid, wid),
        )
        c.execute("DELETE FROM scan_items WHERE scan_id IN (SELECT id FROM scans WHERE watch_id=?)", (wid,))
        c.execute(
            f"DELETE FROM items WHERE {item_scope}",
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


def filters_hash(fs: Optional[List[Dict[str, Any]]] = None) -> str:
    norm = sorted(
        [
            str(f.get("kind") or "").strip().lower(),
            " ".join(str(f.get("value") or "").strip().lower().split()),
        ]
        for f in (fs or [])
        if f.get("active", 1)
    )
    return hashlib.sha1(json.dumps(norm, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def reader_context_hash(notes: str = "", fs: Optional[List[Dict[str, Any]]] = None) -> str:
    payload = {
        "n": " ".join(str(notes or "").strip().lower().split()),
        "f": filters_hash(fs),
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def shared_reader_key(url: str, notes: str = "", fs: Optional[List[Dict[str, Any]]] = None) -> str:
    canon = canonical_reader_url(url)
    if not canon:
        return ""
    return f"{canon}#{reader_context_hash(notes, fs)}"


def get_shared_reader(url: str, notes: str = "", fs: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    canon = canonical_reader_url(url)
    if not canon:
        return None
    ctx_hash = reader_context_hash(notes, fs)
    key = f"{canon}#{ctx_hash}"
    row = _one("SELECT * FROM url_readers WHERE url_key=? AND extractor_ok=1 AND extractor IS NOT NULL", (key,))
    if row:
        return row
    for s in _rows("SELECT id, url, notes, watch_id, extractor, last_good_extractor, extractor_ok, extractor_error, "
                   "extractor_updated, last_good_updated, extractor_runs, last_good_stats "
                   "FROM sources WHERE method='agent' AND extractor_ok=1 AND extractor IS NOT NULL "
                   "ORDER BY COALESCE(extractor_updated, '') DESC, id DESC"):
        if canonical_reader_url(s["url"]) != canon:
            continue
        s_fs = filters_for_watch(s.get("watch_id"), active_only=True)
        if reader_context_hash(s.get("notes") or "", s_fs) == ctx_hash:
            return {
                "url_key": key,
                "url": s["url"],
                "context_hash": ctx_hash,
                "extractor": s["extractor"],
                "last_good_extractor": s.get("last_good_extractor") or s["extractor"],
                "extractor_ok": 1,
                "extractor_error": s.get("extractor_error"),
                "extractor_updated": s.get("extractor_updated"),
                "extractor_runs": s.get("extractor_runs") or 0,
                "last_good_stats": s.get("last_good_stats"),
            }
    return None


def invalidate_shared_reader(url: str, notes: str = "", fs: Optional[List[Dict[str, Any]]] = None,
                             error: str = "relearn requested") -> int:
    """Mark the reader for this URL + context as needing relearning everywhere it is used.

    Clears the shared row and every agent source that would otherwise hand the same reader back
    through get_shared_reader(); the last good reader is kept for fallback. Returns sources touched.
    """
    canon = canonical_reader_url(url)
    if not canon:
        return 0
    ctx_hash = reader_context_hash(notes, fs)
    key = f"{canon}#{ctx_hash}"
    touched = []
    for s in _rows("SELECT id, url, notes, watch_id FROM sources WHERE method='agent' AND extractor_ok=1"):
        if canonical_reader_url(s["url"]) != canon:
            continue
        if reader_context_hash(s.get("notes") or "", filters_for_watch(s.get("watch_id"), active_only=True)) == ctx_hash:
            touched.append(s["id"])
    with _conn() as c:
        c.execute("UPDATE url_readers SET extractor_ok=0, extractor_error=? WHERE url_key=?", (error[:300], key))
        for sid in touched:
            c.execute("UPDATE sources SET extractor_ok=0, extractor_error=? WHERE id=?", (error[:300], sid))
        c.commit()
    return len(touched)


def save_shared_reader(url: str, extractor: str, ok: bool = True, error: Optional[str] = None,
                       stats: Optional[str] = None, updated: Optional[str] = None,
                       propagate: bool = True, notes: str = "",
                       fs: Optional[List[Dict[str, Any]]] = None,
                       empty_reason: Any = _UNSET) -> None:
    canon = canonical_reader_url(url)
    if not canon or not extractor:
        return
    ctx_hash = reader_context_hash(notes, fs)
    key = f"{canon}#{ctx_hash}"
    f_hash = filters_hash(fs)
    ts = updated or now()
    with _lock, _conn() as c:
        existing = c.execute("SELECT * FROM url_readers WHERE url_key=?", (key,)).fetchone()
        last_good = extractor if ok else (existing["last_good_extractor"] if existing else None)
        eff_stats = stats if stats is not None else (existing["last_good_stats"] if existing else None)
        c.execute(
            """INSERT INTO url_readers(url_key, url, extractor, last_good_extractor, extractor_ok,
                                       extractor_error, extractor_updated, extractor_runs, last_good_stats, context_hash)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url_key) DO UPDATE SET
                   url=excluded.url,
                   context_hash=excluded.context_hash,
                   extractor=CASE WHEN excluded.extractor_ok=1 THEN excluded.extractor ELSE url_readers.extractor END,
                   last_good_extractor=CASE WHEN excluded.extractor_ok=1 THEN excluded.extractor ELSE url_readers.last_good_extractor END,
                   extractor_ok=CASE WHEN excluded.extractor_ok=1 THEN 1 ELSE url_readers.extractor_ok END,
                   extractor_error=excluded.extractor_error,
                   extractor_updated=excluded.extractor_updated,
                   extractor_runs=CASE WHEN excluded.extractor_ok=1 THEN url_readers.extractor_runs + 1 ELSE url_readers.extractor_runs END,
                   last_good_stats=COALESCE(excluded.last_good_stats, url_readers.last_good_stats)""",
            (key, str(url)[:500], extractor, last_good, int(bool(ok)), error, ts, 1 if ok else 0, eff_stats, ctx_hash),
        )
        if ok and propagate:
            rows = c.execute("SELECT id, url, notes, watch_id FROM sources WHERE method='agent'").fetchall()
            f_cache: Dict[Optional[int], List[Dict[str, Any]]] = {}
            match_ids: List[int] = []
            for r in rows:
                if canonical_reader_url(r["url"]) != canon:
                    continue
                wid = r["watch_id"]
                if wid not in f_cache:
                    clauses = ["watch_id IS NULL"] if wid is None else ["(watch_id IS NULL OR watch_id=?)"]
                    f_args = [] if wid is None else [wid]
                    clauses.append("active=1")
                    f_cache[wid] = [dict(fr) for fr in c.execute(f"SELECT * FROM filters WHERE {' AND '.join(clauses)}", f_args)]
                if reader_context_hash(r["notes"] or "", f_cache[wid]) == ctx_hash:
                    match_ids.append(r["id"])
            for sid in match_ids:
                if empty_reason is _UNSET:
                    c.execute(
                        """UPDATE sources SET extractor=?, last_good_extractor=?, extractor_ok=1,
                           extractor_error=NULL, extractor_updated=?, last_good_updated=?,
                           last_good_stats=COALESCE(?, last_good_stats),
                           fail_count=0, retry_after=NULL, last_error_kind=NULL,
                           struct_fail_count=0, learn_fail_count=0, learn_retry_after=NULL,
                           paused=0, filter_hash=?, filters_changed=0
                           WHERE id=?""",
                        (extractor, extractor, ts, ts, eff_stats, f_hash, sid),
                    )
                else:
                    c.execute(
                        """UPDATE sources SET extractor=?, last_good_extractor=?, extractor_ok=1,
                           extractor_error=NULL, extractor_updated=?, last_good_updated=?,
                           last_good_stats=COALESCE(?, last_good_stats),
                           fail_count=0, retry_after=NULL, last_error_kind=NULL,
                           struct_fail_count=0, learn_fail_count=0, learn_retry_after=NULL,
                           paused=0, filter_hash=?, filters_changed=0, empty_reason=?, empty_read_count=0
                           WHERE id=?""",
                        (extractor, extractor, ts, ts, eff_stats, f_hash, empty_reason, sid),
                    )
        c.commit()


def mark_filters_changed(watch_id: Optional[int] = None) -> int:
    """Flag learned agent sources whose effective active filters differ from their recorded filter_hash."""
    with _lock, _conn() as c:
        if watch_id is not None:
            rows = c.execute(
                "SELECT id, watch_id, filter_hash, filters_changed FROM sources "
                "WHERE method='agent' AND extractor IS NOT NULL AND watch_id=?",
                (watch_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, watch_id, filter_hash, filters_changed FROM sources "
                "WHERE method='agent' AND extractor IS NOT NULL"
            ).fetchall()
        if not rows:
            return 0
        hash_cache: Dict[Optional[int], str] = {}
        flagged = 0
        for r in rows:
            wid = r["watch_id"]
            if wid not in hash_cache:
                clauses = ["watch_id IS NULL"] if wid is None else ["(watch_id IS NULL OR watch_id=?)"]
                f_args = [] if wid is None else [wid]
                clauses.append("active=1")
                fs_rows = [dict(fr) for fr in c.execute(f"SELECT * FROM filters WHERE {' AND '.join(clauses)}", f_args)]
                hash_cache[wid] = filters_hash(fs_rows)
            changed = int(r["filter_hash"] != hash_cache[wid])
            if int(r["filters_changed"] or 0) != changed:
                c.execute("UPDATE sources SET filters_changed=? WHERE id=?", (changed, r["id"]))
            if changed:
                flagged += 1
        c.commit()
        return flagged


# ---------- sources ----------
def sources(active_only: bool = False, watch_id: Any = _UNSET, include_code: bool = True) -> List[Dict[str, Any]]:
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
            s["has_extractor"] = bool(s.get("extractor"))
            s["has_last_good_extractor"] = bool(s.get("last_good_extractor"))
            if not include_code:
                s.pop("extractor", None)
                s.pop("last_good_extractor", None)
    return out


def source_code(sid: int) -> Optional[Dict[str, Any]]:
    return _one(
        "SELECT id, extractor, last_good_extractor, extractor_ok, extractor_updated, extractor_runs, extractor_error "
        "FROM sources WHERE id=?",
        (sid,),
    )


def source_by_id(sid: int) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM sources WHERE id=?", (sid,))


def source_open_item_count(sid: int) -> int:
    r = _one("SELECT COUNT(*) n FROM items WHERE source_id=? AND status='open'", (sid,))
    return int(r["n"]) if r else 0


def source_tracked_item_count(sid: int) -> int:
    r = _one("SELECT COUNT(*) n FROM items WHERE source_id=? AND (action IN ('saved','applied') OR COALESCE(note,'')!='' OR remind_on IS NOT NULL)", (sid,))
    return int(r["n"]) if r else 0


def watch_tracked_item_count(wid: int) -> int:
    r = _one("SELECT COUNT(*) n FROM items WHERE (watch_id=? OR source_id IN (SELECT id FROM sources WHERE watch_id=?)) AND (action IN ('saved','applied') OR COALESCE(note,'')!='' OR remind_on IS NOT NULL)", (wid, wid))
    return int(r["n"]) if r else 0


def add_source(name: str, url: str, notes: str = "", method: str = "agent", plugin: Optional[str] = None,
               watch_id: Optional[int] = None) -> int:
    clean_url = url.strip()[:500]
    clean_notes = notes.strip()[:1000]
    sid = _exec("INSERT INTO sources(name, url, notes, method, plugin, watch_id, created) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (name.strip()[:80], clean_url, clean_notes, method, plugin, watch_id, now()))
    if method == "agent":
        fs = filters_for_watch(watch_id, active_only=True)
        shared = get_shared_reader(clean_url, notes=clean_notes, fs=fs)
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
                filter_hash=filters_hash(fs),
                filters_changed=0,
            )
    return sid


_SOURCE_UPDATABLE = {
    "name", "url", "notes", "active", "recipe", "extractor", "extractor_ok", "extractor_error",
    "extractor_updated", "extractor_runs", "watch_id", "last_good_extractor", "last_good_updated",
    "last_good_stats", "fail_count", "retry_after", "last_error_kind", "filter_hash",
    "struct_fail_count", "learn_fail_count", "learn_retry_after", "paused", "filters_changed",
    "baseline_done", "empty_read_count", "empty_reason",
}


def update_source(sid: int, **fields) -> bool:
    allowed = {k: v for k, v in fields.items() if k in _SOURCE_UPDATABLE}
    if not allowed:
        return False
    return _exec(f"UPDATE sources SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), sid)) > 0


def delete_source(sid: int) -> bool:
    with _lock, _conn() as c:
        c.execute("DELETE FROM scan_items WHERE item_id IN (SELECT id FROM items WHERE source_id=?)", (sid,))
        c.execute("UPDATE items SET source_id=NULL, watch_id=NULL, action=CASE WHEN action IN ('saved','applied') THEN action ELSE 'saved' END WHERE source_id=? AND (COALESCE(action,'') IN ('saved','applied') OR COALESCE(note,'')!='' OR remind_on IS NOT NULL)", (sid,))
        c.execute("DELETE FROM item_history WHERE item_id IN (SELECT id FROM items WHERE source_id=? AND NOT (COALESCE(action,'') IN ('saved','applied') OR COALESCE(note,'')!='' OR remind_on IS NOT NULL))", (sid,))
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


def filter_by_id(fid: int) -> Optional[Dict[str, Any]]:
    return _one("SELECT * FROM filters WHERE id=?", (fid,))


def add_filter(kind: str, value: str, watch_id: Optional[int] = None) -> int:
    val = (value or "").strip()[:500]
    if kind not in FILTER_KINDS or not val:
        raise ValueError("invalid filter")
    if kind == "deadline_within" and (not val.isdigit() or int(val) <= 0):
        raise ValueError("deadline_within must be a positive number of days")
    fid = _exec("INSERT INTO filters(kind, value, watch_id, created) VALUES(?, ?, ?, ?)",
                (kind, val, watch_id, now()))
    mark_filters_changed(watch_id)
    return fid


def update_filter(fid: int, active: Any = _UNSET, kind: Any = _UNSET, value: Any = _UNSET) -> bool:
    cur = filter_by_id(fid)
    if not cur:
        return False
    allowed: Dict[str, Any] = {}
    eff_kind = cur["kind"]
    if kind is not _UNSET:
        clean_kind = str(kind or "").strip()
        if clean_kind not in FILTER_KINDS:
            raise ValueError("invalid filter kind")
        allowed["kind"] = clean_kind
        eff_kind = clean_kind
    eff_val = cur["value"]
    if value is not _UNSET:
        clean_val = str(value or "").strip()[:500]
        if not clean_val:
            raise ValueError("filter value cannot be empty")
        allowed["value"] = clean_val
        eff_val = clean_val
    if eff_kind == "deadline_within" and (not str(eff_val).isdigit() or int(eff_val) <= 0):
        raise ValueError("deadline_within must be a positive number of days")
    if active is not _UNSET:
        allowed["active"] = int(bool(active))
    if not allowed:
        return False
    changed = _exec(f"UPDATE filters SET {', '.join(k + '=?' for k in allowed)} WHERE id=?", (*allowed.values(), fid)) > 0
    if changed:
        mark_filters_changed(cur.get("watch_id"))
    return changed


def delete_filter(fid: int) -> bool:
    cur = filter_by_id(fid)
    if not cur:
        return False
    deleted = _exec("DELETE FROM filters WHERE id=?", (fid,)) > 0
    if deleted:
        mark_filters_changed(cur.get("watch_id"))
    return deleted


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
    cov = _one("SELECT COUNT(CASE WHEN ok=1 THEN 1 END) ok, COUNT(CASE WHEN ok=0 AND skipped=0 THEN 1 END) failed, COUNT(CASE WHEN skipped=1 THEN 1 END) skipped FROM scan_sources WHERE scan_id=?", (scan_id,))
    if cov["failed"] and not cov["ok"] and status == "done":
        status = "failed"
    _exec("UPDATE scans SET finished=?, status=?, found=?, new=?, note=?, sources_ok=?, sources_failed=?, sources_skipped=? WHERE id=?",
          (now(), status, found, new, note, cov["ok"], cov["failed"], cov["skipped"], scan_id))


def record_source_result(scan_id: int, source: Dict[str, Any], ok: bool, count: int, new: int, error: str, ms: int, skipped: bool = False, error_kind: str = ""):
    sid = source.get("id")
    last_ok = now() if ok else source.get("last_successful_read")
    if ok and sid is not None:
        _exec("UPDATE sources SET last_successful_read=? WHERE id=?", (last_ok, sid))
    _exec("INSERT OR REPLACE INTO scan_sources(scan_id, source_id, source_name, ok, count, new, error, ms, skipped, error_kind, last_successful_read) "
          "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (scan_id, sid, source["name"], int(ok), count, new, error, ms, int(skipped), error_kind, last_ok))


def scans(limit: int = 60) -> List[Dict[str, Any]]:
    return _rows("SELECT id, started, finished, trigger, agent, model, status, found, new, note, label, watch_id, sources_ok, sources_failed, sources_skipped "
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
_TRACKED_ITEM_FIELDS = ("title", "deadline", "start_date", "location", "org", "category")
_CLASSIFY_ITEM_FIELDS = (*_TRACKED_ITEM_FIELDS, "summary")


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


def save_item(scan_id: int, source: Dict[str, Any], it: Dict[str, Any], *,
              match: Any = _UNSET, match_key: Any = _UNSET, notified: Any = _UNSET,
              expired: Any = _UNSET) -> bool:
    """Insert or refresh an item, recording field history. Never resurrects closed items as new.

    `expired` stores the precomputed expiry decision (scanner._is_expired) for the SQL path; an explicit
    0 on update clears the flag (e.g. the deadline moved to the future).
    """
    t = now()
    wid = source.get("watch_id")
    src_id = source.get("id")
    deadline_raw = it.get("deadline_raw")
    with _lock, _conn() as c:
        exists = c.execute("SELECT * FROM items WHERE id=?", (it["id"],)).fetchone()
        if exists is None and it.get("legacy_id"):
            exists = c.execute("SELECT * FROM items WHERE id=? AND title=?", (it["legacy_id"], it["title"])).fetchone()
        if exists is None and it.get("url"):
            listing_key = canonical_reader_url(source.get("url") or "")
            item_key = canonical_reader_url(it["url"])
            if item_key and item_key != listing_key:
                if src_id is not None:
                    exists = c.execute(
                        "SELECT * FROM items WHERE source_id=? AND url=? AND title=? LIMIT 1",
                        (src_id, it["url"], it["title"]),
                    ).fetchone()
                else:
                    exists = c.execute(
                        "SELECT * FROM items WHERE source_name=? AND url=? AND title=? LIMIT 1",
                        (source["name"], it["url"], it["title"]),
                    ).fetchone()
        if exists:
            ex = dict(exists)
            item_id = ex["id"]
            reopened = ex.get("status") == "gone" and match != 0
            changes: List[Tuple[str, Optional[str], Optional[str]]] = []
            if match != 0:
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
            if match is not _UNSET:
                new_match = match
                new_match_key = None if match_key is _UNSET else match_key
            elif ex.get("match_key") == "det_excluded" or any(
                ex.get(field) != it.get(field) for field in _CLASSIFY_ITEM_FIELDS
            ):
                new_match = None
                new_match_key = None
            else:
                new_match = ex.get("match")
                new_match_key = ex.get("match_key") if match_key is _UNSET else match_key
            new_notified = ex.get("notified", 0) if notified is _UNSET else int(bool(notified))
            new_expired = int(bool(expired)) if expired is not _UNSET else int(ex.get("expired") or 0)
            c.execute(
                """UPDATE items SET title=?, org=?, category=?, url=?, deadline=?, deadline_raw=?, start_date=?, location=?,
                   summary=?, extra=?, watch_id=?, status='open', last_scan=?, last_seen=?,
                   updated_at=?, change_summary=?, last_change_kind=?, match=?, match_key=?, notified=?,
                   expired=? WHERE id=?""",
                (it["title"], it.get("org"), it.get("category"), it["url"], it.get("deadline"), deadline_raw,
                 it.get("start_date"), it.get("location"), it.get("summary"), it.get("extra"), wid, scan_id, t,
                 updated_at, summary, change_kind, new_match, new_match_key, new_notified, new_expired, item_id),
            )
            c.execute(
                "INSERT OR REPLACE INTO scan_items(scan_id, item_id, is_new, is_updated) VALUES(?, ?, 0, ?)",
                (scan_id, item_id, 1 if hist_rows else 0),
            )
            c.commit()
            return False

        item_id = it["id"]
        ins_match = None if match is _UNSET else match
        ins_match_key = None if match_key is _UNSET else match_key
        ins_notified = 0 if notified is _UNSET else int(bool(notified))
        ins_expired = 0 if expired is _UNSET else int(bool(expired))
        c.execute(
            """INSERT INTO items(id, source_id, source_name, title, org, category, url, deadline, deadline_raw, start_date,
               location, summary, extra, watch_id, first_scan, last_scan, first_seen, last_seen,
               match, match_key, notified, expired)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (item_id, src_id, source["name"], it["title"], it.get("org"), it.get("category"),
             it["url"], it.get("deadline"), deadline_raw, it.get("start_date"), it.get("location"), it.get("summary"),
             it.get("extra"), wid, scan_id, scan_id, t, t, ins_match, ins_match_key, ins_notified, ins_expired),
        )
        c.execute("INSERT OR REPLACE INTO scan_items(scan_id, item_id, is_new, is_updated) VALUES(?, ?, ?, 0)",
                  (scan_id, item_id, 0 if ins_match == 0 else 1))
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


def close_source_items(source: Dict[str, Any], scan_id: Optional[int] = None) -> int:
    """Close all open items belonging to a verified-empty source (status='gone', never delete)."""
    t = now()
    with _lock, _conn() as c:
        if source.get("id") is None:
            gone_rows = c.execute(
                "SELECT id FROM items WHERE source_name=? AND status='open'",
                (source["name"],),
            ).fetchall()
        else:
            gone_rows = c.execute(
                "SELECT id FROM items WHERE source_id=? AND status='open'",
                (source["id"],),
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


def _items_where(
    watch_id: Any = _UNSET,
    *,
    status: Optional[str] = None,
    match_only: bool = False,
    match: Optional[str] = None,
    exclude_hidden: bool = False,
    exclude_applied: bool = False,
    exclude_expired: bool = False,
    snoozed: str = "all",
    only_updated: bool = False,
    categories: Optional[List[str]] = None,
    source_name: Optional[str] = None,
    source_id: Optional[int] = None,
    search: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    args: List[Any] = []
    today = now()[:10]
    if watch_id is None:
        clauses.append("COALESCE(s.watch_id, i.watch_id) IS NULL")
    elif watch_id is not _UNSET:
        clauses.append("COALESCE(s.watch_id, i.watch_id)=?")
        args.append(watch_id)
    if status and status != "all":
        clauses.append("i.status=?")
        args.append(status)
    if match == "0":
        clauses.append("i.match=0")
    elif match == "1" or match_only:
        clauses.append("COALESCE(i.match, 1)=1")
    if exclude_hidden:
        clauses.append("(i.action IS NULL OR i.action != 'hidden')")
    if exclude_applied:
        clauses.append("(i.action IS NULL OR i.action != 'applied')")
    if exclude_expired:
        clauses.append("COALESCE(i.expired, 0)=0")
        clauses.append(
            "(COALESCE(NULLIF(i.deadline,''), NULLIF(i.start_date,'')) IS NULL "
            "OR COALESCE(NULLIF(i.deadline,''), NULLIF(i.start_date,'')) >= ?)"
        )
        args.append(today)
    if snoozed == "exclude":
        clauses.append("(i.snoozed_until IS NULL OR i.snoozed_until <= ?)")
        args.append(today)
    elif snoozed == "only":
        clauses.append("(i.snoozed_until IS NOT NULL AND i.snoozed_until > ?)")
        args.append(today)
    if only_updated:
        recent_cutoff = (datetime.now() - timedelta(hours=72)).strftime(TS)
        clauses.append(
            "(i.updated_at IS NOT NULL AND i.last_change_kind IS NOT NULL "
            "AND i.last_change_kind != 'closed' AND i.updated_at >= ?)"
        )
        args.append(recent_cutoff)
    if categories:
        cats = [c for c in categories if c]
        if cats:
            placeholders = ",".join("?" for _ in cats)
            clauses.append(f"COALESCE(NULLIF(TRIM(i.category), ''), 'Other') IN ({placeholders})")
            args.extend(cats)
    if source_name:
        clauses.append("i.source_name=?")
        args.append(source_name)
    if source_id is not None:
        clauses.append("i.source_id=?")
        args.append(source_id)
    if search:
        for tok in str(search).strip().split():
            esc_tok = _tr_lower(tok).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pat = f"%{esc_tok}%"
            clauses.append(
                "(norm_tr(i.title) LIKE ? ESCAPE '\\' OR norm_tr(i.org) LIKE ? ESCAPE '\\' "
                "OR norm_tr(i.summary) LIKE ? ESCAPE '\\' OR norm_tr(i.category) LIKE ? ESCAPE '\\' "
                "OR norm_tr(i.location) LIKE ? ESCAPE '\\' OR norm_tr(i.source_name) LIKE ? ESCAPE '\\' "
                "OR norm_tr(i.change_summary) LIKE ? ESCAPE '\\')"
            )
            args.extend([pat] * 7)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def items(
    limit: Optional[int] = None,
    offset: int = 0,
    watch_id: Any = _UNSET,
    *,
    sort: str = "first_seen",
    **filters,
) -> List[Dict[str, Any]]:
    where, args = _items_where(watch_id=watch_id, **filters)
    if sort == "deadline":
        order = (
            "CASE WHEN COALESCE(NULLIF(i.deadline,''), NULLIF(i.start_date,'')) IS NULL THEN 1 ELSE 0 END, "
            "COALESCE(NULLIF(i.deadline,''), NULLIF(i.start_date,'')) ASC, i.first_seen DESC"
        )
    elif sort == "updated":
        order = "COALESCE(i.updated_at, i.first_seen) DESC, i.first_seen DESC"
    else:
        order = "i.first_seen DESC"
    sql = (
        f"SELECT i.*, COALESCE(s.watch_id, i.watch_id) AS watch_id "
        f"FROM items i LEFT JOIN sources s ON s.id = i.source_id{where} ORDER BY {order}"
    )
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args.extend([max(1, int(limit)), max(0, int(offset))])
    return _rows(sql, args)


def items_count(watch_id: Any = _UNSET, **filters) -> int:
    where, args = _items_where(watch_id=watch_id, **filters)
    row = _one(
        f"SELECT COUNT(*) n FROM items i LEFT JOIN sources s ON s.id = i.source_id{where}",
        args,
    )
    return int(row["n"]) if row else 0


def items_facets(
    watch_id: Any = _UNSET,
    *,
    status: Optional[str] = "open",
    match_only: bool = True,
    exclude_hidden: bool = True,
    exclude_applied: bool = True,
    exclude_expired: bool = True,
    snoozed: str = "exclude",
) -> Dict[str, Any]:
    today = now()[:10]
    recent_cutoff = (datetime.now() - timedelta(hours=72)).strftime(TS)
    where_base, args_base = _items_where(
        watch_id=watch_id,
        status=status,
        match_only=match_only,
        exclude_hidden=exclude_hidden,
        exclude_applied=exclude_applied,
        exclude_expired=exclude_expired,
        snoozed=snoozed,
    )
    where_scope, args_scope = _items_where(
        watch_id=watch_id,
        status=status,
        match_only=match_only,
        exclude_hidden=exclude_hidden,
        exclude_applied=exclude_applied,
        exclude_expired=exclude_expired,
        snoozed="only",
    )
    with _lock, _conn() as c:
        cat_rows = c.execute(
            f"SELECT COALESCE(NULLIF(TRIM(i.category), ''), 'Other') AS cat, COUNT(*) AS n "
            f"FROM items i LEFT JOIN sources s ON s.id = i.source_id{where_base} GROUP BY cat ORDER BY n DESC, cat ASC",
            args_base,
        ).fetchall()
        src_rows = c.execute(
            f"SELECT DISTINCT i.source_name FROM items i LEFT JOIN sources s ON s.id = i.source_id{where_base} "
            f"ORDER BY i.source_name COLLATE NOCASE",
            args_base,
        ).fetchall()
        upd_clause = (where_base + " AND " if where_base else " WHERE ") + (
            "(i.updated_at IS NOT NULL AND i.last_change_kind IS NOT NULL "
            "AND i.last_change_kind != 'closed' AND i.updated_at >= ?)"
        )
        upd_count = int(c.execute(
            f"SELECT COUNT(*) n FROM items i LEFT JOIN sources s ON s.id = i.source_id{upd_clause}",
            [*args_base, recent_cutoff],
        ).fetchone()["n"])
        sn_count = int(c.execute(
            f"SELECT COUNT(*) n FROM items i LEFT JOIN sources s ON s.id = i.source_id{where_scope}",
            args_scope,
        ).fetchone()["n"])
    return {
        "categories": {r["cat"]: r["n"] for r in cat_rows},
        "sources": [r["source_name"] for r in src_rows if r["source_name"]],
        "updated_count": upd_count,
        "snoozed_count": sn_count,
    }


def counts() -> Dict[str, Any]:
    today = now()[:10]
    week_end = (date.today() + timedelta(days=7)).isoformat()
    recent_cutoff = (datetime.now() - timedelta(hours=72)).strftime(TS)
    with _lock, _conn() as c:
        base_where = (
            "i.status='open' AND COALESCE(i.match,1)=1 AND COALESCE(i.expired,0)=0 "
            "AND (i.action IS NULL OR i.action NOT IN ('hidden','applied')) "
            "AND (COALESCE(NULLIF(i.deadline,''), NULLIF(i.start_date,'')) IS NULL "
            "OR COALESCE(NULLIF(i.deadline,''), NULLIF(i.start_date,'')) >= ?)"
        )
        rows = c.execute(
            f"""SELECT COALESCE(s.watch_id, i.watch_id) AS wid,
                       i.first_seen, i.updated_at, i.last_change_kind,
                       i.snoozed_until, i.deadline
                FROM items i LEFT JOIN sources s ON s.id = i.source_id
                WHERE {base_where}""",
            (today,),
        ).fetchall()
        feed_open = 0
        feed_new = 0
        feed_updated = 0
        feed_snoozed = 0
        closing_week = 0
        watch_map: Dict[str, Dict[str, int]] = {}
        for w in c.execute("SELECT id FROM watches").fetchall():
            watch_map[str(w["id"])] = {"open": 0, "new": 0, "snoozed": 0, "closing_this_week": 0}
        for r in rows:
            wid = r["wid"]
            wentry = (
                watch_map.setdefault(str(wid), {"open": 0, "new": 0, "snoozed": 0, "closing_this_week": 0})
                if wid is not None else None
            )
            sn = bool(r["snoozed_until"] and r["snoozed_until"] > today)
            if sn:
                feed_snoozed += 1
                if wentry is not None:
                    wentry["snoozed"] += 1
                continue
            feed_open += 1
            if wentry is not None:
                wentry["open"] += 1
            if r["first_seen"] and r["first_seen"] >= recent_cutoff:
                feed_new += 1
                if wentry is not None:
                    wentry["new"] += 1
            if r["updated_at"] and r["last_change_kind"] and r["last_change_kind"] != "closed" and r["updated_at"] >= recent_cutoff:
                feed_updated += 1
            dl = r["deadline"]
            if dl and today <= dl <= week_end:
                closing_week += 1
                if wentry is not None:
                    wentry["closing_this_week"] += 1
        agenda_saved = int(c.execute("SELECT COUNT(*) n FROM items WHERE action='saved'").fetchone()["n"])
        agenda_applied = int(c.execute("SELECT COUNT(*) n FROM items WHERE action='applied'").fetchone()["n"])
        sources_active = int(c.execute("SELECT COUNT(*) n FROM sources WHERE active=1").fetchone()["n"])
        filters_global_active = int(c.execute("SELECT COUNT(*) n FROM filters WHERE watch_id IS NULL AND active=1").fetchone()["n"])
        items_total = int(c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"])
    return {
        "feed_open": feed_open,
        "feed_new": feed_new,
        "feed_updated": feed_updated,
        "feed_snoozed": feed_snoozed,
        "closing_this_week": closing_week,
        "agenda_saved": agenda_saved,
        "agenda_applied": agenda_applied,
        "sources_active": sources_active,
        "filters_global_active": filters_global_active,
        "items_total": items_total,
        "watches": watch_map,
    }


def agenda_items() -> List[Dict[str, Any]]:
    return _rows("SELECT i.*, COALESCE(s.watch_id, i.watch_id) AS watch_id "
                 "FROM items i LEFT JOIN sources s ON s.id = i.source_id "
                 "WHERE i.action IN ('saved', 'applied') AND (i.action='applied' OR COALESCE(i.expired,0)=0) "
                 "ORDER BY CASE WHEN i.action='applied' THEN 1 ELSE 0 END, i.first_seen DESC")


def watch_feed_items(watch_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    return _rows(
        """SELECT i.*, COALESCE(s.watch_id, i.watch_id) AS watch_id
           FROM items i LEFT JOIN sources s ON s.id = i.source_id
           WHERE COALESCE(s.watch_id, i.watch_id)=?
           AND i.status='open' AND COALESCE(i.match, 1)=1 AND COALESCE(i.expired,0)=0
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
           WHERE si.scan_id=? AND COALESCE(i.match, 1)=1 AND COALESCE(i.expired,0)=0
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
                    WHERE notified=0 AND status='open' AND COALESCE(match,1)=1 AND COALESCE(expired,0)=0
                    AND (action IS NULL OR action!='hidden')
                    AND (snoozed_until IS NULL OR snoozed_until <= ?)""", (today,))


def mark_notified(ids: Iterable[str]) -> None:
    with _lock, _conn() as c:
        c.executemany("UPDATE items SET notified=1 WHERE id=?", [(i,) for i in ids])
        c.commit()


def due_reminders(today: Optional[str] = None) -> List[Dict[str, Any]]:
    day = today or now()[:10]
    return _rows("SELECT * FROM items WHERE action IN ('saved','applied') AND remind_on IS NOT NULL AND remind_on<=? AND reminded_at IS NULL ORDER BY remind_on, first_seen", (day,))


def queue_delivery(item_id: str, event: str, event_key: str, channel: str, snapshot: Dict[str, Any]) -> None:
    _exec("INSERT OR IGNORE INTO deliveries(item_id,event,event_key,channel,snapshot) VALUES(?,?,?,?,?)",
          (item_id, event, str(event_key), channel, json.dumps(snapshot, ensure_ascii=False)))


def pending_deliveries(channel: str) -> List[Dict[str, Any]]:
    rows = _rows("SELECT * FROM deliveries WHERE channel=? AND delivered_at IS NULL AND attempts<? ORDER BY id", (channel, MAX_DELIVERY_ATTEMPTS))
    for row in rows:
        try:
            row["item"] = json.loads(row.pop("snapshot") or "{}")
        except (TypeError, ValueError):
            row["item"] = {}
    return rows


def finish_deliveries(ids: Iterable[int], ok: bool, error: str = "") -> None:
    with _lock, _conn() as c:
        for did in ids:
            c.execute("UPDATE deliveries SET attempts=attempts+1, delivered_at=CASE WHEN ? THEN ? ELSE delivered_at END, error=? WHERE id=?",
                      (int(ok), now(), str(error)[:500], did))
        c.commit()


def mark_reminded(item_ids: Iterable[str]) -> None:
    with _lock, _conn() as c:
        c.executemany("UPDATE items SET reminded_at=? WHERE id=? AND reminded_at IS NULL", [(now(), i) for i in item_ids])
        c.commit()


def mark_source_scan_notified(source: Dict[str, Any], scan_id: int) -> None:
    with _lock, _conn() as c:
        if source.get("id") is not None:
            c.execute("UPDATE items SET notified=1 WHERE source_id=? AND last_scan=?", (source["id"], scan_id))
        else:
            c.execute("UPDATE items SET notified=1 WHERE source_name=? AND last_scan=?", (source["name"], scan_id))
        c.commit()


def log_notification(scan_id: Optional[int], channel: str, ok: bool, message: str = "") -> int:
    return _exec(
        "INSERT INTO notification_log(scan_id, channel, ok, message, created) VALUES(?, ?, ?, ?, ?)",
        (scan_id, str(channel)[:40], int(bool(ok)), str(message or "")[:500], now()),
    )


def recent_notify_errors() -> List[Dict[str, Any]]:
    try:
        dismissed_id = int(get_settings().get("notify_errors_dismissed_id") or 0)
    except (TypeError, ValueError):
        dismissed_id = 0
    rows = _rows(
        """SELECT nl.* FROM notification_log nl
           JOIN (
               SELECT channel, MAX(id) AS max_id FROM notification_log GROUP BY channel
           ) latest ON latest.channel = nl.channel AND latest.max_id = nl.id
           WHERE nl.ok=0 AND nl.id > ? ORDER BY nl.id DESC""",
        (dismissed_id,),
    )
    return rows


def dismiss_notify_errors() -> None:
    row = _one("SELECT COALESCE(MAX(id), 0) AS max_id FROM notification_log")
    max_id = int(row["max_id"]) if row else 0
    set_settings({"notify_errors_dismissed_id": str(max_id)})


# ---------- agent usage ----------
def log_agent_call(agent: str, model: str, ok: bool, error: str = "", resets_at: Optional[str] = None) -> None:
    t = now()
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO agent_usage(agent, model, day, calls, ok, last_call, last_error, resets_at)
               VALUES(?, ?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(agent, model, day) DO UPDATE SET
                   calls=calls+1,
                   ok=ok+excluded.ok,
                   last_call=excluded.last_call,
                   last_error=excluded.last_error,
                   resets_at=CASE
                       WHEN excluded.ok=1 THEN NULL
                       WHEN excluded.resets_at IS NOT NULL THEN excluded.resets_at
                       ELSE agent_usage.resets_at
                   END""",
            (agent, model or "", t[:10], int(ok), t, error[-400:], resets_at),
        )
        if ok:
            c.execute(
                "UPDATE agent_usage SET resets_at=NULL WHERE agent=? AND model=? AND resets_at IS NOT NULL",
                (agent, model or ""),
            )
        c.commit()


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
                        WHERE si.scan_id=? AND (i.match IS NULL OR i.match_key IS NULL OR i.match_key != ?)
                        AND COALESCE(i.match_key, '') != 'det_excluded'""", (scan_id, key))
    return _rows("""SELECT i.* FROM scan_items si
                    JOIN items i ON i.id = si.item_id
                    LEFT JOIN sources s ON s.id = i.source_id
                    WHERE si.scan_id=? AND COALESCE(s.watch_id, i.watch_id) IS ?
                    AND (i.match IS NULL OR i.match_key IS NULL OR i.match_key != ?)
                    AND COALESCE(i.match_key, '') != 'det_excluded'""", (scan_id, watch_id, key))


def open_items_for_scope(watch_id: Optional[int]) -> List[Dict[str, Any]]:
    if watch_id is None:
        return _rows(
            "SELECT i.*, COALESCE(s.watch_id, i.watch_id) AS watch_id "
            "FROM items i LEFT JOIN sources s ON s.id = i.source_id "
            "WHERE i.status='open' AND COALESCE(s.watch_id, i.watch_id) IS NULL"
        )
    return _rows(
        "SELECT i.*, COALESCE(s.watch_id, i.watch_id) AS watch_id "
        "FROM items i LEFT JOIN sources s ON s.id = i.source_id "
        "WHERE i.status='open' AND COALESCE(s.watch_id, i.watch_id)=?",
        (watch_id,),
    )


def open_items() -> List[Dict[str, Any]]:
    """Every stored OPEN item, for expiry re-evaluation."""
    return _rows("SELECT * FROM items WHERE status='open'")


def mark_items_expired(ids: Iterable[str]) -> int:
    """Flags items as expired (keeps them stored and open; nothing is deleted)."""
    batch = [i for i in ids]
    if not batch:
        return 0
    placeholders = ",".join("?" for _ in batch)
    return _exec(f"UPDATE items SET expired=1 WHERE id IN ({placeholders})", batch)


def open_items_needing_match(key: str, watch_id: Optional[int]) -> List[Dict[str, Any]]:
    return _rows(
        """SELECT i.* FROM items i
           LEFT JOIN sources s ON s.id = i.source_id
           WHERE i.status='open' AND COALESCE(s.watch_id, i.watch_id) IS ?
           AND (i.match IS NULL OR i.match_key IS NULL OR i.match_key != ?)
           AND COALESCE(i.match_key, '') != 'det_excluded'""",
        (watch_id, key),
    )


def update_item_matches(rows: Iterable[Tuple[Optional[int], Optional[str], str]]) -> None:
    batch = list(rows)
    if not batch:
        return
    with _lock, _conn() as c:
        c.executemany("UPDATE items SET match=?, match_key=? WHERE id=?", batch)
        c.commit()


def set_matches(decisions: Dict[str, bool], key: str) -> None:
    with _lock, _conn() as c:
        c.executemany("UPDATE items SET match=?, match_key=? WHERE id=?",
                      [(int(v), key, k) for k, v in decisions.items()])
        c.commit()
