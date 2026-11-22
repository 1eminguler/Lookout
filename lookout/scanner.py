"""Runs a scan: every active source (agent-driven or local plugin), filters, stores and records history."""
import hashlib
import html
import importlib.util
import inspect
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from lookout import agents, db, extractors, notify, tasks

PLUGIN_DIR = Path(os.environ.get("LOOKOUT_PLUGIN_DIR") or db.ROOT / "plugins")
progress: Dict[str, Any] = {
    "running": False, "scan_id": None, "done": 0, "total": 0, "current": [],
    "learning": [], "learning_total": 0, "learning_done": 0, "sources_state": {}, "cancel": False,
}
_lock = threading.Lock()
_cancel_event = threading.Event()
_url_locks_guard = threading.Lock()
_url_locks: Dict[str, threading.Lock] = {}
_url_generations: Dict[str, int] = {}
_active_lock = threading.Lock()
_active_scan: Optional[Dict[str, Any]] = None

_TRACKING_EXACT = {"fbclid", "gclid", "mc_cid", "mc_eid"}

_TRANSIENT_RE = re.compile(
    r"timed?\s*out|timeout|http\s*(?:408|429|500|502|503|504|52\d)|"
    r"too many requests|rate.?limit|quota|resource_exhausted|"
    r"connection\s*(?:refused|reset|aborted|closed)|remote end closed|"
    r"broken pipe|eof occurred|temporarily unavailable|"
    r"temporary failure in name resolution|name or service not known|"
    r"getaddrinfo failed|nodename nor servname|network is unreachable|"
    r"no route to host|ssl.*(?:eof|handshake|timeout)|"
    r"net::err_|err_connection|err_timed_out|err_name_not_resolved|err_network",
    re.I,
)
_STRUCTURAL_RE = re.compile(
    r"returned nothing|returned \d+ items|most items have no title|"
    r"invalid json|jsondecodeerror|syntax error|is not allowed|no fetch\(\)|"
    r"must return a list|zerodivisionerror|keyerror|indexerror|typeerror|"
    r"attributeerror|valueerror|http\s*(?:400|401|403|404|410|451)",
    re.I,
)


class TransientError(RuntimeError):
    """Raised when a reader hits a temporary network/rate-limit failure and should keep its code."""


def _url_learn_lock(url: str) -> threading.Lock:
    key = db.canonical_reader_url(url) or str(url)
    with _url_locks_guard:
        lk = _url_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _url_locks[key] = lk
        return lk


def _url_gen(url: str) -> int:
    key = db.canonical_reader_url(url) or str(url)
    with _url_locks_guard:
        return _url_generations.get(key, 0)


def _bump_url_gen(url: str) -> int:
    key = db.canonical_reader_url(url) or str(url)
    with _url_locks_guard:
        nxt = _url_generations.get(key, 0) + 1
        _url_generations[key] = nxt
        return nxt


def get_progress() -> Dict[str, Any]:
    with _lock:
        return {
            "running": bool(progress.get("running")),
            "scan_id": progress.get("scan_id"),
            "done": int(progress.get("done") or 0),
            "total": int(progress.get("total") or 0),
            "current": list(progress.get("current") or []),
            "learning": list(progress.get("learning") or []),
            "learning_total": int(progress.get("learning_total") or 0),
            "learning_done": int(progress.get("learning_done") or 0),
            "sources_state": dict(progress.get("sources_state") or {}),
        }


def cancel_scan(async_browser: bool = False) -> None:
    with _lock:
        progress["cancel"] = True
    _cancel_event.set()
    agents.cancel("scan", async_browser=async_browser)


# ---------- local plugins ----------
def load_plugins() -> Dict[str, Callable[[], list]]:
    """plugins/*.py may define SOURCES = [{"name", "url", "fetch"}] (or the legacy KAYNAKLAR = [{"ad", "fn"}])."""
    found: Dict[str, Callable[[], list]] = {}
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    if not PLUGIN_DIR.is_dir():
        return found
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"lookout_plugin_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"[plugin] {path.name} failed to load: {e}")
            continue
        entries = getattr(mod, "SOURCES", None)
        if entries is None:
            entries = [{"name": k["ad"], "fetch": k["fn"]} for k in getattr(mod, "KAYNAKLAR", [])]
        for e in entries:
            found[e["name"]] = e["fetch"]
            if not db.source_by_plugin(e["name"]):
                db.add_source(e["name"], e.get("url", f"plugin:{path.name}"), "Local plugin", method="plugin",
                              plugin=e["name"])
    return found


# ---------- normalisation ----------
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MONTHS.update({m: i + 1 for i, m in enumerate(
    ["oca", "şub", "mar", "nis", "may", "haz", "tem", "ağu", "eyl", "eki", "kas", "ara"])})


def clean(v: Any, limit: int) -> str:
    if not v:
        return ""
    s = html.unescape(str(v))
    s = re.sub(r"(?is)<(script|style)\b.*?</(script|style)>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


def blank_to_none(v: str) -> Optional[str]:
    """Sites often fill empty fields with placeholders like 'n/a' or '-'."""
    return None if not v or v.strip().lower() in {"n/a", "na", "none", "null", "-", "—", "unknown", "tbd"} else v


def _clean_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_EXACT]
    if len(kept) == len(pairs):
        return url
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), parts.fragment))


def parse_date(v: Any) -> Optional[str]:
    if not v:
        return None
    s = str(v).strip().replace("İ", "i").lower()
    try:
        if m := re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        if m := re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s):
            return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        if m := re.search(r"(\d{1,2})\s+([a-zçğıöşü]{3})[a-zçğıöşü]*\.?,?\s+(\d{4})", s):
            if m[2] in MONTHS:
                return date(int(m[3]), MONTHS[m[2]], int(m[1])).isoformat()
        if m := re.search(r"([a-zçğıöşü]{3})[a-zçğıöşü]*\.?\s+(\d{1,2}),?\s+(\d{4})", s):
            if m[1] in MONTHS:
                return date(int(m[3]), MONTHS[m[1]], int(m[2])).isoformat()
    except ValueError:
        return None
    return None


def normalise(raw: Dict[str, Any], source: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = clean(raw.get("title"), 160)
    url = str(raw.get("url") or "").strip()
    if not title or not re.match(r"^https?://", url, re.I):
        return None
    url = _clean_url(url)
    extra = raw.get("extra")
    src_key = f"src:{source['id']}" if source.get("id") is not None else source["name"]
    item_key = db.canonical_reader_url(url)
    list_key = db.canonical_reader_url(source.get("url") or "")
    if raw.get("id"):
        key = f"{src_key}|{raw['id']}"
    elif item_key and item_key != list_key:
        key = f"{src_key}|{item_key}"
    else:
        key = f"{src_key}|{url}|{title}"
    return {
        "id": hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:20],
        "title": title,
        "org": blank_to_none(clean(raw.get("org") or raw.get("company"), 100)),
        "category": blank_to_none(clean(raw.get("category") or raw.get("type"), 40)),
        "url": url,
        "deadline": parse_date(raw.get("deadline")),
        "start_date": parse_date(raw.get("start_date")),
        "location": blank_to_none(clean(raw.get("location"), 80)),
        "summary": blank_to_none(clean(raw.get("summary") or raw.get("description"), 400)),
        "extra": json.dumps(extra, ensure_ascii=False)[:2000] if isinstance(extra, dict) and extra else None,
    }


def passes(item: Dict[str, Any], fs: List[Dict[str, Any]]) -> bool:
    """Deterministic part of the filters; the agent also applies them while browsing."""
    today = date.today().isoformat()
    dl = item.get("deadline")
    if dl and dl < today:
        return False
    text = " ".join(str(item.get(k) or "") for k in ("title", "org", "category", "summary", "location")).lower()
    for f in fs:
        v = f["value"].lower().strip()
        if f["kind"] == "exclude":
            for w in (x.strip() for x in v.split(",") if x.strip()):
                pat = (r"\b" if w[0].isalnum() else "") + re.escape(w) + (r"\b" if w[-1].isalnum() else "")
                if re.search(pat, text):
                    return False
        if f["kind"] == "deadline_within" and v.isdigit() and dl:
            if dl > (date.today() + timedelta(days=int(v))).isoformat():
                return False
    return True


# ---------- failure classification, anomaly detection & verification ----------
def classify_error(exc: Exception) -> str:
    """Classify a reader failure as 'transient' (keep code, retry later) or 'structural' (relearn)."""
    if isinstance(exc, TransientError):
        return "transient"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "transient"
    msg = str(exc or "")
    if _STRUCTURAL_RE.search(msg):
        return "structural"
    if _TRANSIENT_RE.search(msg):
        return "transient"
    return "structural"


def _backoff_until(fail_count: int) -> str:
    minutes = min(360, 5 * (2 ** max(0, fail_count - 1)))
    return (datetime.now() + timedelta(minutes=minutes)).strftime(db.TS)


def _url_pattern(url: str) -> str:
    parts = urllib.parse.urlsplit(str(url or "").strip())
    host = (parts.hostname or "").lower()
    segs = [s for s in parts.path.split("/") if s]
    first = segs[0].lower() if segs else ""
    return f"{host}/{first}" if host else ""


def compute_item_stats(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(items)
    usable = sum(1 for i in items if str(i.get("title") or "").strip() and str(i.get("url") or "").startswith("http"))
    fill_rates: Dict[str, float] = {}
    for field in ("org", "deadline", "location", "summary", "category"):
        filled = sum(1 for i in items if str(i.get(field) or "").strip())
        fill_rates[field] = round(filled / n, 3) if n else 0.0
    patterns = sorted({_url_pattern(i.get("url") or "") for i in items if _url_pattern(i.get("url") or "")})[:20]
    return {
        "count": n,
        "usable_count": usable,
        "usable_rate": round(usable / n, 3) if n else 1.0,
        "fill_rates": fill_rates,
        "url_patterns": patterns,
    }


def _load_stats(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        return None


def detect_anomaly(items: List[Dict[str, Any]], prev_stats: Optional[Dict[str, Any]],
                   had_open_items: int = 0) -> Optional[str]:
    """Returns a human-readable anomaly reason if output drifted vs history, else None."""
    prev_count = max(int(had_open_items or 0), int((prev_stats or {}).get("count") or 0))
    if not items and prev_count > 0:
        return "returned nothing although the site had listings before"
    if prev_count >= 5 and len(items) < prev_count * 0.4:
        return f"count dropped sharply ({len(items)} vs {prev_count} previously)"
    cur_stats = compute_item_stats(items)
    if items and cur_stats["usable_rate"] < 0.8:
        return "most items have no title or link"
    if prev_stats and int(prev_stats.get("count") or 0) >= 3 and len(items) >= 3:
        prev_rates = prev_stats.get("fill_rates") or {}
        for field in ("org", "deadline", "location", "summary"):
            pr = float(prev_rates.get(field) or 0.0)
            cr = float(cur_stats["fill_rates"].get(field) or 0.0)
            if pr >= 0.5 and cr < pr * 0.3:
                return f"field '{field}' fill rate collapsed ({cr:.0%} vs {pr:.0%})"
        prev_pats = set(prev_stats.get("url_patterns") or [])
        cur_pats = set(cur_stats.get("url_patterns") or [])
        if prev_pats and cur_pats and not (prev_pats & cur_pats):
            return "item URL pattern changed"
    return None


def verify_reader_output(agent_items: List[Dict[str, Any]], code_items: List[Dict[str, Any]],
                       prev_stats: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """Stronger verification of learned extractor output beyond simple title/URL overlap."""
    if agent_items and not code_items:
        return False, f"code found 0 items, agent saw {len(agent_items)}"
    if not extractors.agrees(agent_items, code_items):
        return False, f"code found {len(code_items)} items, agent saw {len(agent_items)}"
    if not code_items:
        return True, ""
    cur_stats = compute_item_stats(code_items)
    if cur_stats["usable_rate"] < 0.8:
        return False, "most extracted items have no title or link"
    if len(agent_items) >= 3 and len(code_items) < max(1, int(len(agent_items) * 0.5)):
        return False, f"code returned only {len(code_items)} items vs agent {len(agent_items)}"
    if len(code_items) >= 4:
        uniq = {(str(i.get("title") or "").strip().lower(), str(i.get("url") or "").strip()) for i in code_items}
        if len(uniq) < len(code_items) * 0.5:
            return False, "code returned mostly duplicate items"
    if len(agent_items) >= 3:
        agent_stats = compute_item_stats(agent_items)
        for field in ("org", "deadline", "location", "summary"):
            ar = float(agent_stats["fill_rates"].get(field) or 0.0)
            cr = float(cur_stats["fill_rates"].get(field) or 0.0)
            if ar >= 0.6 and cr < 0.25:
                return False, f"code missed '{field}' values ({cr:.0%} vs agent {ar:.0%})"
    if prev_stats and int(prev_stats.get("count") or 0) >= 8 and len(agent_items) >= 5:
        if len(code_items) < int(prev_stats["count"]) * 0.25:
            return False, f"code dropped severely vs history ({len(code_items)} vs {prev_stats['count']})"
    return True, ""


def _check_structural_sanity(items: List[Dict[str, Any]], had_items: int) -> None:
    usable = [i for i in items if str(i.get("title") or "").strip() and str(i.get("url") or "").startswith("http")]
    if not items and had_items:
        raise RuntimeError("returned nothing although the site had listings before")
    if had_items >= 5 and len(items) < had_items * 0.3:
        raise RuntimeError(f"returned {len(items)} items, {had_items} were listed last time")
    if items and len(usable) < len(items) * 0.8:
        raise RuntimeError("most items have no title or link")


# ---------- agent sources ----------
SEMANTIC = ("include", "type", "location", "instruction")


def filter_match_key(global_fs: List[Dict[str, Any]], watch_fs: List[Dict[str, Any]] = ()) -> str:
    g = [[f["kind"], f["value"]] for f in global_fs if f["kind"] in SEMANTIC]
    w = [[f["kind"], f["value"]] for f in watch_fs if f["kind"] in SEMANTIC]
    if not g and not w:
        return "none"
    return hashlib.sha1(json.dumps({"g": g, "w": w}, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _run_fast_reader(source: Dict[str, Any], code: str) -> List[Dict[str, Any]]:
    return extractors.run(code, tag="scan", cancel_event=_cancel_event)


def read_agent_source(source: Dict[str, Any], fs: List[Dict[str, Any]], force_learn: bool = False,
                      ignore_backoff: bool = False) -> List[Dict]:
    """Fast path: run the learned reader. Weekly check only invokes the agent if output is anomalous."""
    if progress["cancel"] or _cancel_event.is_set():
        raise agents.CancelledError("cancelled")
    settings = db.get_settings()
    sid = source.get("id")
    if (not force_learn and not ignore_backoff and source.get("last_error_kind") == "transient"
            and source.get("retry_after") and source["retry_after"] > db.now()):
        raise TransientError(f"Waiting for retry backoff until {source['retry_after'][11:16]}")

    prev_good = source.get("last_good_extractor") or (source.get("extractor") if source.get("extractor_ok") else None)
    prev_good_updated = source.get("last_good_updated") or (
        source.get("extractor_updated") if source.get("extractor_ok") else None)
    prev_stats = _load_stats(source.get("last_good_stats"))

    if (not source.get("extractor") or not source.get("extractor_ok")) and not force_learn:
        shared = db.get_shared_reader(source["url"])
        if shared and shared.get("extractor"):
            ts = shared.get("extractor_updated") or db.now()
            source = {
                **source,
                "extractor": shared["extractor"],
                "last_good_extractor": shared.get("last_good_extractor") or shared["extractor"],
                "extractor_ok": 1,
                "extractor_error": None,
                "extractor_updated": ts,
                "last_good_updated": ts,
                "last_good_stats": shared.get("last_good_stats") or source.get("last_good_stats"),
            }
            if sid is not None:
                db.update_source(
                    sid,
                    extractor=source["extractor"],
                    last_good_extractor=source["last_good_extractor"],
                    extractor_ok=1,
                    extractor_error=None,
                    extractor_updated=ts,
                    last_good_updated=ts,
                    last_good_stats=source["last_good_stats"],
                )
            prev_good = source["last_good_extractor"]
            prev_good_updated = ts
            prev_stats = _load_stats(source["last_good_stats"])

    stale = bool(
        settings.get("verify_weekly") == "1"
        and source.get("extractor_updated")
        and source["extractor_updated"] < (date.today() - timedelta(days=7)).isoformat()
    )
    weekly_fallback_items: Optional[List[Dict[str, Any]]] = None
    had_items = db.source_open_item_count(sid) if sid is not None else 0

    if source.get("extractor") and source.get("extractor_ok") and not force_learn:
        try:
            items = _run_fast_reader(source, source["extractor"])
            _check_structural_sanity(items, had_items)
            cur_stats = compute_item_stats(items)
            stats_json = json.dumps(cur_stats, ensure_ascii=False)
            now_ts = db.now()
            if stale:
                anomaly = detect_anomaly(items, prev_stats, had_open_items=had_items)
                if anomaly is None or not settings.get("agent"):
                    if sid is not None:
                        db.update_source(
                            sid,
                            extractor_error=None,
                            extractor_updated=now_ts,
                            last_good_extractor=source["extractor"],
                            last_good_updated=now_ts,
                            last_good_stats=stats_json,
                            extractor_runs=(source.get("extractor_runs") or 0) + 1,
                            fail_count=0,
                            retry_after=None,
                            last_error_kind=None,
                        )
                    db.save_shared_reader(source["url"], source["extractor"], ok=True, stats=stats_json, updated=now_ts)
                    return items
                weekly_fallback_items = items
                source = {**source, "extractor_error": anomaly}
            else:
                if sid is not None:
                    db.update_source(
                        sid,
                        extractor_error=None,
                        last_good_extractor=prev_good or source["extractor"],
                        last_good_updated=prev_good_updated or source.get("extractor_updated") or now_ts,
                        last_good_stats=stats_json,
                        extractor_runs=(source.get("extractor_runs") or 0) + 1,
                        fail_count=0,
                        retry_after=None,
                        last_error_kind=None,
                    )
                db.save_shared_reader(source["url"], source["extractor"], ok=True, stats=stats_json,
                                      updated=source.get("extractor_updated") or now_ts, propagate=False)
                return items
        except agents.CancelledError:
            raise
        except Exception as e:
            kind = classify_error(e)
            err_str = str(e)[:300]
            if kind == "transient":
                fc = int(source.get("fail_count") or 0) + 1
                ra = _backoff_until(fc)
                if sid is not None:
                    db.update_source(
                        sid,
                        fail_count=fc,
                        retry_after=ra,
                        last_error_kind="transient",
                        extractor_error=err_str,
                    )
                raise TransientError(err_str) from None
            if sid is not None:
                db.update_source(
                    sid,
                    extractor_ok=0,
                    last_error_kind="structural",
                    extractor_error=err_str,
                )
            source = {**source, "extractor_ok": 0, "extractor_error": err_str}

    gen_before = _url_gen(source["url"])
    with _url_learn_lock(source["url"]):
        if progress["cancel"] or _cancel_event.is_set():
            raise agents.CancelledError("cancelled")
        if not force_learn and _url_gen(source["url"]) > gen_before:
            shared = db.get_shared_reader(source["url"])
            if shared and shared.get("extractor"):
                try:
                    items = _run_fast_reader(source, shared["extractor"])
                    _check_structural_sanity(items, had_items)
                    stats_json = json.dumps(compute_item_stats(items), ensure_ascii=False)
                    ts = shared.get("extractor_updated") or db.now()
                    if sid is not None:
                        db.update_source(
                            sid,
                            extractor=shared["extractor"],
                            last_good_extractor=shared.get("last_good_extractor") or shared["extractor"],
                            extractor_ok=1,
                            extractor_updated=ts,
                            last_good_updated=ts,
                            last_good_stats=stats_json,
                            extractor_error=None,
                            fail_count=0,
                            retry_after=None,
                            last_error_kind=None,
                        )
                    return items
                except agents.CancelledError:
                    raise
                except Exception:
                    pass

        with _lock:
            progress.setdefault("learning", [])
            if source["name"] not in progress["learning"]:
                progress["learning"].append(source["name"])
            progress["learning_total"] = max(int(progress.get("learning_total") or 0),
                                             int(progress.get("learning_done") or 0) + len(progress["learning"]))
            progress.setdefault("sources_state", {})[source["name"]] = "learning"
        try:
            learned = tasks.learn_source(source, fs, cancel_event=_cancel_event)
        except agents.CancelledError:
            raise
        except Exception as e:
            if prev_good and sid is not None:
                if weekly_fallback_items is not None:
                    db.update_source(
                        sid,
                        extractor=prev_good,
                        extractor_ok=1,
                        extractor_error=f"Weekly re-check failed ({str(e)[:200]}); kept previous working reader",
                    )
                    return weekly_fallback_items
                db.update_source(sid, extractor=prev_good, extractor_ok=0, extractor_error=str(e)[:300])
            raise
        finally:
            with _lock:
                if source["name"] in progress.get("learning", []):
                    progress["learning"].remove(source["name"])
                progress["learning_done"] = int(progress.get("learning_done") or 0) + 1

        seen, code = learned["items"], learned["code"]
        if code:
            try:
                got = extractors.run(code, tag="scan", cancel_event=_cancel_event)
                ok, reason = verify_reader_output(seen, got, prev_stats=prev_stats)
                if ok:
                    ts = db.now()
                    stats_json = json.dumps(compute_item_stats(got), ensure_ascii=False)
                    if sid is not None:
                        db.update_source(
                            sid,
                            extractor=code,
                            last_good_extractor=code,
                            extractor_ok=1,
                            extractor_updated=ts,
                            last_good_updated=ts,
                            last_good_stats=stats_json,
                            extractor_error=None,
                            fail_count=0,
                            retry_after=None,
                            last_error_kind=None,
                        )
                    db.save_shared_reader(source["url"], code, ok=True, stats=stats_json, updated=ts, propagate=True)
                    _bump_url_gen(source["url"])
                    return got
                err_msg = f"{reason}; keeping previous reader" if prev_good else reason
                if sid is not None:
                    if prev_good:
                        db.update_source(
                            sid,
                            extractor=prev_good,
                            extractor_ok=1 if weekly_fallback_items is not None else 0,
                            extractor_error=err_msg[:300],
                        )
                    else:
                        db.update_source(sid, extractor=code, extractor_ok=0, extractor_updated=db.now(),
                                         extractor_error=err_msg[:300])
            except agents.CancelledError:
                raise
            except Exception as e:
                err_msg = str(e)[:300]
                if sid is not None:
                    if prev_good:
                        db.update_source(
                            sid,
                            extractor=prev_good,
                            extractor_ok=1 if weekly_fallback_items is not None else 0,
                            extractor_error=f"{err_msg}; kept previous reader"[:300],
                        )
                    else:
                        db.update_source(sid, extractor=code, extractor_ok=0, extractor_updated=db.now(),
                                         extractor_error=err_msg)
        elif sid is not None and prev_good:
            db.update_source(
                sid,
                extractor=prev_good,
                extractor_ok=1 if weekly_fallback_items is not None else 0,
                extractor_error="Agent did not produce a new reader; kept previous reader",
            )
    if not seen and not code:
        if weekly_fallback_items is not None:
            return weekly_fallback_items
        raise RuntimeError("The agent returned nothing. " + learned["log"][-200:].strip())
    return seen if seen else (weekly_fallback_items or [])


def apply_semantic_filters(scan_id: int, global_fs: List[Dict[str, Any]],
                           watch_fs_map: Optional[Dict[int, List[Dict[str, Any]]]] = None,
                           watch_ids: Optional[List[Optional[int]]] = None) -> str:
    """Asks the agent once per distinct filter scope for items not judged under those filters yet."""
    if progress["cancel"] or _cancel_event.is_set():
        return ""
    if watch_ids is None:
        groups: List[tuple] = [(db._UNSET, [])]
    else:
        ordered = ([None] if None in watch_ids else []) + sorted({w for w in watch_ids if w is not None})
        groups = [(wid, (watch_fs_map or {}).get(wid, []) if wid is not None else []) for wid in ordered]
    has_agent = bool(db.get_settings().get("agent"))
    warnings: List[str] = []
    for wid, wfs in groups:
        if progress["cancel"] or _cancel_event.is_set():
            return ""
        key = filter_match_key(global_fs, wfs)
        todo = db.items_needing_match(scan_id, key, watch_id=wid)
        if not todo:
            continue
        if key == "none" or not has_agent:
            db.set_matches({i["id"]: True for i in todo}, key)
            continue
        with _lock:
            progress["current"] = ["applying filters"]
        effective_fs = list(global_fs) + list(wfs)
        try:
            decisions: Dict[str, bool] = {}
            for k in range(0, len(todo), 80):
                if progress["cancel"] or _cancel_event.is_set():
                    return ""
                part = todo[k:k + 80]
                got = tasks.classify(part, effective_fs, cancel_event=_cancel_event)
                decisions.update({i["id"]: got.get(i["id"], True) for i in part})
            db.set_matches(decisions, key)
        except agents.CancelledError:
            return ""
        except Exception as e:
            warnings.append(str(e)[:120])
    if warnings:
        return f"Filters could not be applied by the agent ({warnings[0]}); all items are shown."
    return ""


# ---------- source preview (Test this source) ----------
def test_source(source: Dict[str, Any], fs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Preview parsed items for a source without writing items or modifying saved extractors."""
    fs = fs if fs is not None else db.filters_for_watch(source.get("watch_id"), active_only=True)
    used_method = "reader"
    try:
        if source.get("method") == "plugin":
            used_method = "plugin"
            plugins = load_plugins()
            fetch = plugins.get(source.get("plugin"))
            if not fetch:
                raise RuntimeError("plugin file not found in plugins/")
            raw = fetch() or []
        else:
            code = source.get("extractor") if source.get("extractor_ok") else None
            if not code:
                shared = db.get_shared_reader(source["url"])
                if shared and shared.get("extractor"):
                    code = shared["extractor"]
                    used_method = "shared_reader"
            if code:
                raw = extractors.run(code, tag="test")
            elif db.get_settings().get("agent"):
                used_method = "agent_preview"
                raw = tasks.quick_read_source(source, fs)["items"]
            else:
                raise RuntimeError("No reader for this source yet and no AI agent selected")
        seen = set()
        out_items: List[Dict[str, Any]] = []
        for r in raw:
            it = normalise(r, source) if isinstance(r, dict) else None
            if not it or it["id"] in seen:
                continue
            seen.add(it["id"])
            it["passes_filters"] = passes(it, fs)
            out_items.append(it)
        return {
            "ok": True,
            "method": used_method,
            "count": len(out_items),
            "matching": sum(1 for i in out_items if i["passes_filters"]),
            "items": out_items[:50],
        }
    except Exception as e:
        return {"ok": False, "method": used_method, "count": 0, "matching": 0, "items": [],
                "error": f"{type(e).__name__}: {e}"[:400]}


# ---------- scan ----------
def _scan_one(scan_id: int, source: Dict[str, Any], fs: List[Dict[str, Any]], plugins: Dict[str, Callable],
              ignore_backoff: bool = False) -> None:
    t0 = time.time()
    with _lock:
        progress["current"].append(source["name"])
        progress.setdefault("sources_state", {})[source["name"]] = "reading"
    ok, count, new, error = False, 0, 0, ""
    try:
        if progress["cancel"] or _cancel_event.is_set():
            raise agents.CancelledError("cancelled")
        if source["method"] == "plugin":
            fetch = plugins.get(source["plugin"])
            if not fetch:
                raise RuntimeError("plugin file not found in plugins/")
            raw = fetch() or []
        else:
            raw = read_agent_source(source, fs, ignore_backoff=ignore_backoff)
        if progress["cancel"] or _cancel_event.is_set():
            raise agents.CancelledError("cancelled")
        seen = set()
        for r in raw:
            it = normalise(r, source) if isinstance(r, dict) else None
            if not it or it["id"] in seen or not passes(it, fs):
                continue
            seen.add(it["id"])
            count += 1
            new += int(db.save_item(scan_id, source, it))
        db.mark_gone(source, scan_id)
        ok = True
    except agents.CancelledError:
        error = "cancelled"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:400]
        if not isinstance(e, RuntimeError):
            traceback.print_exc()
    finally:
        db.record_source_result(scan_id, source, ok, count, new, error, int((time.time() - t0) * 1000))
        db.refresh_scan_counts(scan_id)
        with _lock:
            progress["done"] += 1
            if source["name"] in progress["current"]:
                progress["current"].remove(source["name"])
            progress.setdefault("sources_state", {})[source["name"]] = (
                "done" if ok else ("cancelled" if error == "cancelled" else "failed")
            )


def _scan_label(trigger: str, only: Optional[List[int]], srcs: List[Dict[str, Any]],
                watch_id: Optional[int] = None) -> str:
    if trigger == "scheduled":
        return "Daily scan"
    if watch_id is not None:
        title = db.watch_title(watch_id)
        if title:
            return title
    if only:
        names = [s["name"] for s in srcs] or [s["name"] for s in db.sources() if s["id"] in only]
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} and {names[1]}"
        if len(names) > 2:
            return f"{names[0]}, {names[1]} and {len(names) - 2} more"
    return "All sources"


def _notify_webhook(scan_id: int) -> None:
    settings = db.get_settings()
    url = (settings.get("webhook_url") or "").strip()
    if not url or settings.get("notify") != "1":
        return
    fn = getattr(notify, "webhook", None)
    if not callable(fn):
        return
    changes = db.scan_changes(scan_id)
    if not changes:
        return
    new_items = [i for i in changes if i.get("is_new")]
    upd_items = [i for i in changes if not i.get("is_new") and i.get("is_updated")]
    lines = []
    for it in changes[:8]:
        prefix = "NEW" if it.get("is_new") else (it.get("change_summary") or "UPDATED")
        lines.append(f"- [{prefix}] {it['title']} ({it['source_name']}) {it['url']}")
    payload = {
        "event": "scan_finished",
        "scan_id": scan_id,
        "title": f"Lookout: {len(new_items)} new, {len(upd_items)} updated",
        "message": "\n".join(lines),
        "new_count": len(new_items),
        "updated_count": len(upd_items),
        "items": [
            {
                "id": i["id"],
                "title": i["title"],
                "org": i.get("org"),
                "source": i["source_name"],
                "source_name": i["source_name"],
                "url": i["url"],
                "deadline": i.get("deadline"),
                "is_new": bool(i.get("is_new")),
                "is_updated": bool(i.get("is_updated")),
                "change_summary": i.get("change_summary"),
            }
            for i in changes[:20]
        ],
    }
    try:
        params = inspect.signature(fn).parameters
        accepts_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if "allow_local" in params or accepts_kw:
            fn(url, payload, allow_local=(settings.get("webhook_allow_local") == "1"))
        else:
            fn(url, payload)
    except Exception:
        pass


def enqueue_scan_sources(source_ids: List[int], watch_id: Optional[int] = None) -> bool:
    """Appends newly discovered sources to a currently running scan for the same watch."""
    if not source_ids:
        return False
    with _active_lock:
        ctx = _active_scan
        if not ctx or not ctx.get("accepting") or ctx.get("watch_id") != watch_id:
            return False
        want = set(source_ids) - ctx["submitted_ids"]
        if not want:
            return True
        srcs = [s for s in db.sources(watch_id=watch_id) if s["id"] in want and s["active"]]
        if not srcs:
            return True
        global_fs = ctx["global_fs"]
        watch_fs_map = ctx["watch_fs_map"]
        plugins = ctx["plugins"]
        scan_id = ctx["scan_id"]
        fast = ctx["fast"]
        slow = ctx["slow"]
        with _lock:
            progress["total"] += len(srcs)
            for s in srcs:
                progress.setdefault("sources_state", {})[s["name"]] = "queued"
        for s in srcs:
            ctx["submitted_ids"].add(s["id"])
            ctx["srcs"].append(s)
            fs = global_fs + watch_fs_map.get(s.get("watch_id"), [])
            pool = fast if s["method"] == "plugin" else slow
            ctx["futures"].append(pool.submit(_scan_one, scan_id, s, fs, plugins, ctx["ignore_backoff"]))
        return True


def _run_scan_body(trigger: str, only: Optional[List[int]], watch_id: Optional[int],
                   lock_token: str) -> Optional[int]:
    global _active_scan
    hb_stop = threading.Event()
    hb_thread: Optional[threading.Thread] = None
    try:
        db.init()
        plugins = load_plugins()
        settings = db.get_settings()
        global_fs = db.filters(active_only=True, watch_id=None)
        watch_fs_map: Dict[int, List[Dict[str, Any]]] = {}
        for f in db.filters(active_only=True):
            if f.get("watch_id") is not None:
                watch_fs_map.setdefault(f["watch_id"], []).append(f)
        active_watches = {w["id"] for w in db.watches(active_only=True)}
        if only:
            only_set = set(only)
            srcs = [s for s in db.sources() if s["id"] in only_set]
        elif watch_id is not None:
            srcs = db.sources(active_only=True, watch_id=watch_id)
        else:
            srcs = [s for s in db.sources(active_only=True)
                    if s.get("watch_id") is None or s["watch_id"] in active_watches]
        if not settings["agent"]:
            srcs = [s for s in srcs if s["method"] == "plugin"]
        label = _scan_label(trigger, only, srcs, watch_id=watch_id)
        snap_fs = (global_fs + watch_fs_map.get(watch_id, [])) if watch_id is not None else db.filters(active_only=True)
        scan_id = db.start_scan(trigger, settings["agent"], settings["model"], snap_fs, label, watch_id=watch_id)
        db.update_scan_lock(lock_token, scan_id=scan_id)

        def _heartbeat():
            while not hb_stop.wait(15):
                try:
                    db.update_scan_lock(lock_token, scan_id=scan_id)
                except Exception:
                    pass

        hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        hb_thread.start()

        ignore_backoff = bool(only)
        with _lock:
            progress.update(
                scan_id=scan_id,
                total=len(srcs),
                sources_state={s["name"]: "queued" for s in srcs},
            )
        workers = max(1, min(8, int(settings.get("parallel") or 2)))
        agent_count = sum(1 for s in srcs if s["method"] != "plugin")
        slow_workers = max(workers * 2, min(12, agent_count)) if agent_count else max(workers, 2)
        with ThreadPoolExecutor(max_workers=8) as fast, ThreadPoolExecutor(max_workers=slow_workers) as slow:
            ctx = {
                "accepting": True,
                "watch_id": watch_id,
                "scan_id": scan_id,
                "global_fs": global_fs,
                "watch_fs_map": watch_fs_map,
                "plugins": plugins,
                "fast": fast,
                "slow": slow,
                "ignore_backoff": ignore_backoff,
                "submitted_ids": {s["id"] for s in srcs if s.get("id") is not None},
                "srcs": list(srcs),
                "futures": [],
            }
            for s in srcs:
                fs = global_fs + watch_fs_map.get(s.get("watch_id"), [])
                pool = fast if s["method"] == "plugin" else slow
                ctx["futures"].append(pool.submit(_scan_one, scan_id, s, fs, plugins, ignore_backoff))
            with _active_lock:
                _active_scan = ctx
            try:
                while True:
                    with _active_lock:
                        batch = list(ctx["futures"])
                        ctx["futures"].clear()
                        if not batch:
                            ctx["accepting"] = False
                            break
                    for f in batch:
                        f.result()
                # Drain anything queued right before accepting became False
                with _active_lock:
                    tail = list(ctx["futures"])
                    ctx["futures"].clear()
                for f in tail:
                    f.result()
                all_srcs = list(ctx["srcs"])
            finally:
                with _active_lock:
                    if _active_scan is ctx:
                        _active_scan = None

        note = "" if all_srcs else "No active sources" + ("" if settings["agent"] else " (no agent selected)")
        cancelled = progress["cancel"] or _cancel_event.is_set()
        if not cancelled:
            watch_ids = [s.get("watch_id") for s in all_srcs] if all_srcs else []
            note = apply_semantic_filters(scan_id, global_fs, watch_fs_map, watch_ids) or note
        final_status = "cancelled" if (cancelled or progress["cancel"] or _cancel_event.is_set()) else "done"
        db.finish_scan(scan_id, final_status, note)
        if final_status == "done":
            _notify_webhook(scan_id)
        return scan_id
    finally:
        hb_stop.set()
        if hb_thread is not None:
            hb_thread.join(timeout=1.0)
        db.release_scan_lock(lock_token)
        with _lock:
            progress.update(running=False, current=[], learning=[], learning_total=0, learning_done=0,
                            sources_state={})


def run_scan(trigger: str = "manual", only: Optional[List[int]] = None,
             watch_id: Optional[int] = None) -> Optional[int]:
    """Blocking scan. Returns the scan id (None if one is already running in this or another process)."""
    lock_token = uuid.uuid4().hex
    with _lock:
        if progress["running"]:
            return None
        db.init()
        if not db.acquire_scan_lock(lock_token):
            return None
        _cancel_event.clear()
        progress.update(running=True, scan_id=None, done=0, total=0, current=[], learning=[],
                        learning_total=0, learning_done=0, sources_state={}, cancel=False)
    return _run_scan_body(trigger, only, watch_id, lock_token)


def start_scan_async(trigger: str = "manual", only: Optional[List[int]] = None,
                     watch_id: Optional[int] = None) -> bool:
    lock_token = uuid.uuid4().hex
    with _lock:
        if progress["running"]:
            return False
        db.init()
        if not db.acquire_scan_lock(lock_token):
            return False
        _cancel_event.clear()
        progress.update(running=True, scan_id=None, done=0, total=0, current=[], learning=[],
                        learning_total=0, learning_done=0, sources_state={}, cancel=False)
    threading.Thread(target=_run_scan_body, args=(trigger, only, watch_id, lock_token), daemon=True).start()
    return True
