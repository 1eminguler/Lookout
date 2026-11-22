"""Runs a scan: every active source (agent-driven or local plugin), filters, stores and records history."""
import hashlib
import html
import importlib.util
import json
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from lookout import db, extractors, tasks

PLUGIN_DIR = Path(__import__("os").environ.get("LOOKOUT_PLUGIN_DIR") or db.ROOT / "plugins")
progress = {"running": False, "scan_id": None, "done": 0, "total": 0, "current": [], "cancel": False}
_lock = threading.Lock()


# ---------- local plugins ----------
def load_plugins() -> Dict[str, Callable[[], list]]:
    """plugins/*.py may define SOURCES = [{"name", "url", "fetch"}] (or the legacy KAYNAKLAR = [{"ad", "fn"}])."""
    found: Dict[str, Callable[[], list]] = {}
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
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


def parse_date(v: Any) -> Optional[str]:
    if not v:
        return None
    s = str(v).strip().lower()
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
    if not title or not url.startswith("http"):
        return None
    extra = raw.get("extra")
    key = raw.get("id") or f"{source['name']}|{url}|{title}"
    return {
        "id": hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:20],
        "title": title,
        "org": blank_to_none(clean(raw.get("org") or raw.get("company"), 100)),
        "category": clean(raw.get("category") or raw.get("type"), 40) or None,
        "url": url,
        "deadline": parse_date(raw.get("deadline")),
        "start_date": parse_date(raw.get("start_date")),
        "location": clean(raw.get("location"), 80) or None,
        "summary": clean(raw.get("summary") or raw.get("description"), 400) or None,
        "extra": json.dumps(extra, ensure_ascii=False)[:2000] if isinstance(extra, dict) and extra else None,
    }


def passes(item: Dict[str, Any], fs: List[Dict[str, Any]]) -> bool:
    """Deterministic part of the filters; the agent also applies them while browsing."""
    today = date.today().isoformat()
    if item["deadline"] and item["deadline"] < today:
        return False
    text = " ".join(str(item.get(k) or "") for k in ("title", "org", "category", "summary", "location")).lower()
    for f in fs:
        v = f["value"].lower().strip()
        if f["kind"] == "exclude" and any(w.strip() and re.search(r"\b" + re.escape(w.strip()) + r"\b", text)
                                          for w in v.split(",")):
            return False
        if f["kind"] == "deadline_within" and v.isdigit() and item["deadline"]:
            if item["deadline"] > (date.today() + timedelta(days=int(v))).isoformat():
                return False
    return True


# ---------- agent sources ----------
SEMANTIC = ("include", "type", "location", "instruction")


def read_agent_source(source: Dict[str, Any], fs: List[Dict[str, Any]], force_learn: bool = False) -> List[Dict]:
    """Fast path: run the extractor the agent wrote earlier. Slow path: let the agent (re)learn the site."""
    # Weekly check: the agent looks at the site again in case it changed in ways the code cannot notice.
    stale = (db.get_settings().get("verify_weekly") == "1" and source.get("extractor_updated")
             and source["extractor_updated"] < (date.today() - timedelta(days=7)).isoformat())
    if stale and db.get_settings().get("agent"):
        force_learn = True
    if source.get("extractor") and source.get("extractor_ok") and not force_learn:
        try:
            items = extractors.run(source["extractor"])
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
        except Exception as e:
            db.update_source(source["id"], extractor_ok=0, extractor_error=str(e)[:300])
            source = {**source, "extractor_error": str(e)[:300]}
    with _lock:
        progress.setdefault("learning", []).append(source["name"])
    try:
        learned = tasks.learn_source(source, fs)
    finally:
        with _lock:
            if source["name"] in progress.get("learning", []):
                progress["learning"].remove(source["name"])
    seen, code = learned["items"], learned["code"]
    if code:
        try:
            got = extractors.run(code)
            ok = extractors.agrees(seen, got) and (got or not seen)
            db.update_source(source["id"], extractor=code, extractor_ok=int(bool(ok)), extractor_updated=db.now(),
                             extractor_error=None if ok else
                             f"code found {len(got)} items, agent saw {len(seen)}; using the agent's results")
            if ok:
                return got
        except Exception as e:
            db.update_source(source["id"], extractor=code, extractor_ok=0, extractor_updated=db.now(),
                             extractor_error=str(e)[:300])
    if not seen and not code:
        raise RuntimeError("The agent returned nothing. " + learned["log"][-200:].strip())
    return seen


def apply_semantic_filters(scan_id: int, fs: List[Dict[str, Any]]) -> str:
    """Asks the agent once, for items not judged under the current filters yet."""
    sem = [f for f in fs if f["kind"] in SEMANTIC]
    key = hashlib.sha1(json.dumps([[f["kind"], f["value"]] for f in sem]).encode()).hexdigest()[:12] if sem else "none"
    todo = db.items_needing_match(scan_id, key)
    if not todo:
        return ""
    if not sem or not db.get_settings()["agent"]:
        db.set_matches({i["id"]: True for i in todo}, key)
        return ""
    progress["current"] = ["applying filters"]
    try:
        decisions = {}
        for k in range(0, len(todo), 80):
            part = todo[k:k + 80]
            got = tasks.classify(part, fs)
            decisions.update({i["id"]: got.get(i["id"], True) for i in part})
        db.set_matches(decisions, key)
        return ""
    except Exception as e:
        return f"Filters could not be applied by the agent ({str(e)[:120]}); all items are shown."


# ---------- scan ----------
def _scan_one(scan_id: int, source: Dict[str, Any], fs: List[Dict[str, Any]], plugins: Dict[str, Callable]) -> None:
    t0 = time.time()
    with _lock:
        progress["current"].append(source["name"])
    ok, count, new, error = False, 0, 0, ""
    try:
        if progress["cancel"]:
            raise RuntimeError("cancelled")
        if source["method"] == "plugin":
            fetch = plugins.get(source["plugin"])
            if not fetch:
                raise RuntimeError("plugin file not found in plugins/")
            raw, recipe = fetch() or [], None
        else:
            raw, recipe = read_agent_source(source, fs), None
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


def _scan_label(trigger: str, only: Optional[List[int]], srcs: List[Dict[str, Any]]) -> str:
    if trigger == "scheduled":
        return "Daily scan"
    if only:
        names = [s["name"] for s in srcs] or [s["name"] for s in db.sources() if s["id"] in only]
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} and {names[1]}"
        if len(names) > 2:
            return f"{names[0]}, {names[1]} and {len(names) - 2} more"
    return "All sources"


def run_scan(trigger: str = "manual", only: Optional[List[int]] = None) -> Optional[int]:
    """Blocking scan. Returns the scan id (None if one is already running)."""
    with _lock:
        if progress["running"]:
            return None
        progress.update(running=True, done=0, total=0, current=[], cancel=False)
    try:
        db.init()
        plugins = load_plugins()
        settings = db.get_settings()
        fs = db.filters(active_only=True)
        srcs = [s for s in db.sources(active_only=True) if not only or s["id"] in only]
        if not settings["agent"]:
            srcs = [s for s in srcs if s["method"] == "plugin"]
        label = _scan_label(trigger, only, srcs)
        scan_id = db.start_scan(trigger, settings["agent"], settings["model"], fs, label)
        progress.update(scan_id=scan_id, total=len(srcs))
        workers = max(1, int(settings.get("parallel") or 2))
        plugin_srcs = [s for s in srcs if s["method"] == "plugin"]
        agent_srcs = [s for s in srcs if s["method"] != "plugin"]
        with ThreadPoolExecutor(max_workers=8) as fast, ThreadPoolExecutor(max_workers=workers) as slow:
            jobs = [fast.submit(_scan_one, scan_id, s, fs, plugins) for s in plugin_srcs]
            jobs += [slow.submit(_scan_one, scan_id, s, fs, plugins) for s in agent_srcs]
            for j in jobs:
                j.result()
        note = "" if srcs else "No active sources" + ("" if settings["agent"] else " (no agent selected)")
        note = apply_semantic_filters(scan_id, fs) or note
        db.finish_scan(scan_id, "cancelled" if progress["cancel"] else "done", note)
        return scan_id
    finally:
        progress.update(running=False, current=[])


def start_scan_async(trigger: str = "manual", only: Optional[List[int]] = None) -> bool:
    if progress["running"]:
        return False
    threading.Thread(target=run_scan, args=(trigger, only), daemon=True).start()
    return True
