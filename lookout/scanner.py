"""Runs a scan: every active source (agent-driven or local plugin), filters, stores and records history."""
import hashlib
import html
import importlib.util
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from lookout import agents, db, extractors, tasks

PLUGIN_DIR = Path(os.environ.get("LOOKOUT_PLUGIN_DIR") or db.ROOT / "plugins")
progress: Dict[str, Any] = {"running": False, "scan_id": None, "done": 0, "total": 0, "current": [], "learning": [],
                            "cancel": False}
_lock = threading.Lock()
_cancel_event = threading.Event()
_TRACKING_EXACT = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def cancel_scan(async_browser: bool = False) -> None:
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
    key = raw.get("id") or f"{source['name']}|{url}|{title}"
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


# ---------- agent sources ----------
SEMANTIC = ("include", "type", "location", "instruction")


def filter_match_key(global_fs: List[Dict[str, Any]], watch_fs: List[Dict[str, Any]] = ()) -> str:
    g = [[f["kind"], f["value"]] for f in global_fs if f["kind"] in SEMANTIC]
    w = [[f["kind"], f["value"]] for f in watch_fs if f["kind"] in SEMANTIC]
    if not g and not w:
        return "none"
    return hashlib.sha1(json.dumps({"g": g, "w": w}, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def read_agent_source(source: Dict[str, Any], fs: List[Dict[str, Any]], force_learn: bool = False) -> List[Dict]:
    """Fast path: run the extractor the agent wrote earlier. Slow path: let the agent (re)learn the site."""
    if progress["cancel"] or _cancel_event.is_set():
        raise agents.CancelledError("cancelled")
    # Weekly check: the agent looks at the site again in case it changed in ways the code cannot notice.
    stale = (db.get_settings().get("verify_weekly") == "1" and source.get("extractor_updated")
             and source["extractor_updated"] < (date.today() - timedelta(days=7)).isoformat())
    if stale and db.get_settings().get("agent"):
        force_learn = True
    if source.get("extractor") and source.get("extractor_ok") and not force_learn:
        try:
            items = extractors.run(source["extractor"], tag="scan", cancel_event=_cancel_event)
            had_items = db._one("SELECT COUNT(*) n FROM items WHERE source_id=? AND status='open'", (source["id"],))["n"]
            usable = [i for i in items if str(i.get("title") or "").strip() and str(i.get("url") or "").startswith("http")]
            # Signs that the site changed under the code: nothing, a sharp drop, or rows without titles/links.
            if not items and had_items:
                raise RuntimeError("returned nothing although the site had listings before")
            if had_items >= 5 and len(items) < had_items * 0.3:
                raise RuntimeError(f"returned {len(items)} items, {had_items} were listed last time")
            if items and len(usable) < len(items) * 0.8:
                raise RuntimeError("most items have no title or link")
            db.update_source(source["id"], extractor_error=None,
                             extractor_runs=(source.get("extractor_runs") or 0) + 1)
            return items
        except agents.CancelledError:
            raise
        except Exception as e:
            db.update_source(source["id"], extractor_ok=0, extractor_error=str(e)[:300])
            source = {**source, "extractor_error": str(e)[:300]}
    with _lock:
        progress.setdefault("learning", []).append(source["name"])
    try:
        learned = tasks.learn_source(source, fs, cancel_event=_cancel_event)
    finally:
        with _lock:
            if source["name"] in progress.get("learning", []):
                progress["learning"].remove(source["name"])
    seen, code = learned["items"], learned["code"]
    if code:
        try:
            got = extractors.run(code, tag="scan", cancel_event=_cancel_event)
            ok = extractors.agrees(seen, got) and (got or not seen)
            db.update_source(source["id"], extractor=code, extractor_ok=int(bool(ok)), extractor_updated=db.now(),
                             extractor_error=None if ok else
                             f"code found {len(got)} items, agent saw {len(seen)}; using the agent's results")
            if ok:
                return got
        except agents.CancelledError:
            raise
        except Exception as e:
            db.update_source(source["id"], extractor=code, extractor_ok=0, extractor_updated=db.now(),
                             extractor_error=str(e)[:300])
    if not seen and not code:
        raise RuntimeError("The agent returned nothing. " + learned["log"][-200:].strip())
    return seen


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


# ---------- scan ----------
def _scan_one(scan_id: int, source: Dict[str, Any], fs: List[Dict[str, Any]], plugins: Dict[str, Callable]) -> None:
    t0 = time.time()
    with _lock:
        progress["current"].append(source["name"])
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
            raw = read_agent_source(source, fs)
        if progress["cancel"] or _cancel_event.is_set():
            raise agents.CancelledError("cancelled")
        seen = set()
        for r in raw:
            it = normalise(r, source) if isinstance(r, dict) else None
            if not it or it["id"] in seen or not passes(it, fs):
                continue
            seen.add(it["id"])
            count += 1
            new += db.save_item(scan_id, source, it)
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
        with _lock:
            progress["done"] += 1
            if source["name"] in progress["current"]:
                progress["current"].remove(source["name"])


def _scan_label(trigger: str, only: Optional[List[int]], srcs: List[Dict[str, Any]],
                watch_id: Optional[int] = None) -> str:
    if trigger == "scheduled":
        return "Daily scan"
    if watch_id is not None:
        w = db._one("SELECT title FROM watches WHERE id=?", (watch_id,))
        if w:
            return w["title"]
    if only:
        names = [s["name"] for s in srcs] or [s["name"] for s in db.sources() if s["id"] in only]
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} and {names[1]}"
        if len(names) > 2:
            return f"{names[0]}, {names[1]} and {len(names) - 2} more"
    return "All sources"


def _run_scan_body(trigger: str, only: Optional[List[int]], watch_id: Optional[int]) -> Optional[int]:
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
        progress.update(scan_id=scan_id, total=len(srcs))
        workers = max(1, int(settings.get("parallel") or 2))
        plugin_srcs = [s for s in srcs if s["method"] == "plugin"]
        agent_srcs = [s for s in srcs if s["method"] != "plugin"]
        with ThreadPoolExecutor(max_workers=8) as fast, ThreadPoolExecutor(max_workers=workers) as slow:
            jobs = [fast.submit(_scan_one, scan_id, s, global_fs + watch_fs_map.get(s.get("watch_id"), []), plugins)
                    for s in plugin_srcs]
            jobs += [slow.submit(_scan_one, scan_id, s, global_fs + watch_fs_map.get(s.get("watch_id"), []), plugins)
                     for s in agent_srcs]
            for j in jobs:
                j.result()
        note = "" if srcs else "No active sources" + ("" if settings["agent"] else " (no agent selected)")
        cancelled = progress["cancel"] or _cancel_event.is_set()
        if not cancelled:
            watch_ids = [s.get("watch_id") for s in srcs] if srcs else []
            note = apply_semantic_filters(scan_id, global_fs, watch_fs_map, watch_ids) or note
        db.finish_scan(scan_id, "cancelled" if (cancelled or progress["cancel"] or _cancel_event.is_set()) else "done",
                       note)
        return scan_id
    finally:
        progress.update(running=False, current=[], learning=[])


def run_scan(trigger: str = "manual", only: Optional[List[int]] = None,
             watch_id: Optional[int] = None) -> Optional[int]:
    """Blocking scan. Returns the scan id (None if one is already running)."""
    with _lock:
        if progress["running"]:
            return None
        _cancel_event.clear()
        progress.update(running=True, scan_id=None, done=0, total=0, current=[], learning=[], cancel=False)
    return _run_scan_body(trigger, only, watch_id)


def start_scan_async(trigger: str = "manual", only: Optional[List[int]] = None,
                     watch_id: Optional[int] = None) -> bool:
    with _lock:
        if progress["running"]:
            return False
        _cancel_event.clear()
        progress.update(running=True, scan_id=None, done=0, total=0, current=[], learning=[], cancel=False)
    threading.Thread(target=_run_scan_body, args=(trigger, only, watch_id), daemon=True).start()
    return True
