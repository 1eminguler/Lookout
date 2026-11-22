"""Unit tests for lookout.scanner."""
import json
import threading
import time
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from lookout import agents, db, notify, scanner, tasks
from tests.helpers import TempLookoutTestCase


class ScannerTests(TempLookoutTestCase):
    def test_parse_date_formats_and_turkish(self):
        self.assertEqual(scanner.parse_date("2027-01-15"), "2027-01-15")
        self.assertEqual(scanner.parse_date("15.01.2027"), "2027-01-15")
        self.assertEqual(scanner.parse_date("15/01/2027"), "2027-01-15")
        self.assertEqual(scanner.parse_date("15 Jan 2027"), "2027-01-15")
        self.assertEqual(scanner.parse_date("Jan 15, 2027"), "2027-01-15")
        self.assertEqual(scanner.parse_date("15 Oca 2027"), "2027-01-15")
        self.assertEqual(scanner.parse_date("22 Şub 2027"), "2027-02-22")
        self.assertEqual(scanner.parse_date("15 NİSAN 2027"), "2027-04-15")
        self.assertEqual(scanner.parse_date("14 EKİM 2027"), "2027-10-14")
        self.assertEqual(scanner.parse_date("1 Ağu 2027"), "2027-08-01")
        self.assertEqual(scanner.parse_date("31 Ara 2027"), "2027-12-31")
        self.assertIsNone(scanner.parse_date("2027-02-30"))
        self.assertIsNone(scanner.parse_date("not a date"))
        self.assertIsNone(scanner.parse_date(""))
        self.assertIsNone(scanner.parse_date(None))

    def test_normalise_strips_html_placeholders_tracking_and_keys_by_url(self):
        src = {"id": 7, "name": "Board", "url": "https://ex.com/list"}
        self.assertIsNone(scanner.normalise({"url": "https://ex.com/1"}, src))
        self.assertIsNone(scanner.normalise({"title": "Job", "url": "javascript:alert(1)"}, src))

        it = scanner.normalise({
            "title": "  <b>ML</b> <script>bad()</script> Intern  ",
            "url": "https://ex.com/jobs?id=7&utm_source=x&fbclid=y",
            "org": "n/a",
            "category": "-",
            "location": "unknown",
            "summary": "tbd",
            "deadline": "15 NİSAN 2027",
        }, src)
        self.assertEqual(it["title"], "ML Intern")
        self.assertEqual(it["url"], "https://ex.com/jobs?id=7")
        self.assertIsNone(it["org"])
        self.assertIsNone(it["category"])
        self.assertIsNone(it["location"])
        self.assertIsNone(it["summary"])
        self.assertEqual(it["deadline"], "2027-04-15")

        # Title edit on the same source+URL preserves item id rather than creating a duplicate
        it_edited = scanner.normalise({"title": "ML Intern (Updated)", "url": "https://ex.com/jobs?id=7"}, src)
        self.assertEqual(it["id"], it_edited["id"])

    def test_passes_exclude_and_deadlines(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        in_40d = (date.today() + timedelta(days=40)).isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        fs = [{"kind": "exclude", "value": "senior, c++"}, {"kind": "deadline_within", "value": "14"}]

        self.assertTrue(scanner.passes({"title": "International Intern", "deadline": tomorrow}, fs))
        self.assertFalse(scanner.passes({"title": "Senior Engineer", "deadline": tomorrow}, fs))
        self.assertFalse(scanner.passes({"title": "C++ Intern", "deadline": tomorrow}, fs))
        self.assertFalse(scanner.passes({"title": "ML Intern", "deadline": in_40d}, fs))
        self.assertFalse(scanner.passes({"title": "ML Intern", "deadline": yesterday}, []))
        self.assertTrue(scanner.passes({"title": "Rolling Intern", "deadline": None}, fs))

    def test_load_plugins_good_legacy_broken_and_underscore(self):
        (self.plugin_dir / "good.py").write_text(
            "SOURCES = [{'name': 'Good', 'url': 'https://good.example.com', 'fetch': lambda: []}]\n",
            encoding="utf-8",
        )
        (self.plugin_dir / "legacy.py").write_text(
            "KAYNAKLAR = [{'ad': 'Legacy', 'fn': lambda: []}]\n",
            encoding="utf-8",
        )
        (self.plugin_dir / "broken.py").write_text("raise RuntimeError('broken plugin')\n", encoding="utf-8")
        (self.plugin_dir / "_ignored.py").write_text(
            "SOURCES = [{'name': 'Ignored', 'url': 'https://ignored.example.com', 'fetch': lambda: []}]\n",
            encoding="utf-8",
        )
        found = scanner.load_plugins()
        self.assertEqual(set(found.keys()), {"Good", "Legacy"})
        names = {s["name"] for s in db.sources()}
        self.assertEqual(names, {"Good", "Legacy"})

    def test_transient_vs_structural_failures_and_backoff(self):
        db.set_settings({"agent": "claude", "verify_weekly": "0"})
        good_code = "def fetch():\n    return [{'title': 'A', 'url': 'https://ex.com/a'}]\n"
        sid = db.add_source("Site", "https://ex.com")
        db.update_source(sid, extractor=good_code, last_good_extractor=good_code, extractor_ok=1, extractor_updated=db.now())

        # Transient failure (e.g. 503 or timeout) keeps reader intact, skips learn_source, sets retry_after backoff
        for exc in (TimeoutError("timed out"), RuntimeError("HTTP 503 for https://ex.com: Service Unavailable")):
            with self.subTest(exc=str(exc)), \
                 patch.object(scanner, "_run_fast_reader", side_effect=exc), \
                 patch.object(tasks, "learn_source") as m_learn:
                with self.assertRaises(scanner.TransientError):
                    scanner.read_agent_source(db.source_by_id(sid), [])
                m_learn.assert_not_called()
            s = db.source_by_id(sid)
            self.assertEqual(s["extractor_ok"], 1)
            self.assertEqual(s["last_good_extractor"], good_code)
            self.assertEqual(s["last_error_kind"], "transient")
            self.assertGreater(s["fail_count"], 0)
            self.assertTrue(s["retry_after"] > db.now())

        # Subsequent call within backoff window fails immediately without calling reader or agent
        with patch.object(scanner, "_run_fast_reader") as m_fast, \
             patch.object(tasks, "learn_source") as m_learn:
            with self.assertRaises(scanner.TransientError):
                scanner.read_agent_source(db.source_by_id(sid), [])
            m_fast.assert_not_called()
            m_learn.assert_not_called()

        # Structural error (e.g. 404 or JSON parse error) triggers relearn
        db.update_source(sid, retry_after=None, last_error_kind=None, fail_count=0)
        repaired_code = "def fetch():\n    return [{'title': 'B', 'url': 'https://ex.com/b'}]\n"
        learned_ret = {"items": [{"title": "B", "url": "https://ex.com/b"}], "code": repaired_code, "log": ""}
        with patch.object(scanner, "_run_fast_reader", side_effect=RuntimeError("HTTP 404 for https://ex.com")), \
             patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            got = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)
        self.assertEqual(got, [{"title": "B", "url": "https://ex.com/b"}])
        self.assertEqual(db.source_by_id(sid)["last_good_extractor"], repaired_code)

    def test_self_healing_triggers_last_good_preservation_and_cheap_weekly_check(self):
        db.set_settings({"agent": "claude", "verify_weekly": "1"})
        ok_code = "def fetch():\n    return [{'title': 'A', 'url': 'https://ex.com/jobs/a', 'org': 'Org', 'deadline': '2027-06-01'}]\n"
        learned_ret = {"items": [{"title": "A", "url": "https://ex.com/jobs/a", "org": "Org", "deadline": "2027-06-01"}],
                       "code": ok_code, "log": ""}

        # 1. Runtime structural error triggers relearn
        sid = db.add_source("Site", "https://ex.com")
        db.update_source(sid, extractor="def fetch(): raise ZeroDivisionError()", extractor_ok=1,
                         extractor_updated=db.now())
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            res = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)
        self.assertEqual(len(res), 1)
        self.assertEqual(db.source_by_id(sid)["last_good_extractor"], ok_code)

        # 2. Failed heal (agent error or disagreeing reader) NEVER overwrites last_good_extractor
        db.update_source(sid, extractor="def fetch(): raise KeyError('k')", extractor_ok=1, extractor_updated=db.now())
        bad_heal = {
            "items": [{"title": "X", "url": "https://ex.com/x"}, {"title": "Y", "url": "https://ex.com/y"}, {"title": "Z", "url": "https://ex.com/z"}],
            "code": "def fetch():\n    return [{'title': 'Wrong', 'url': 'https://ex.com/wrong'}]\n",
            "log": "",
        }
        with patch.object(tasks, "learn_source", return_value=bad_heal):
            got = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(len(got), 3)
        self.assertEqual(db.source_by_id(sid)["last_good_extractor"], ok_code)
        self.assertEqual(db.source_by_id(sid)["extractor"], ok_code)

        # Restore working extractor
        db.update_source(sid, extractor=ok_code, last_good_extractor=ok_code, extractor_ok=1, extractor_updated=db.now())

        # 3. Empty result when site previously had open items triggers relearn
        scan1 = db.start_scan("manual", "claude", "", [])
        for k in range(5):
            db.save_item(scan1, db.source_by_id(sid), {"id": f"old{k}", "title": f"Old {k}", "url": f"https://ex.com/{k}"})
        db.update_source(sid, extractor="def fetch(): return []", extractor_ok=1, extractor_updated=db.now())
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)

        # 4. Sharp drop (<30% of >=5 open items) triggers relearn
        db.update_source(sid, extractor="def fetch(): return [{'title': 'Only1', 'url': 'https://ex.com/1'}]",
                         extractor_ok=1, extractor_updated=db.now())
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)

        # 5. Rows without title/url (<80% usable) triggers relearn
        bad_rows = "def fetch(): return [{'title': '', 'url': 'bad'}, {'title': 'Ok', 'url': 'https://ex.com/ok'}]"
        db.update_source(sid, extractor=bad_rows, extractor_ok=1, extractor_updated=db.now())
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)

        # 6. Cheap weekly check: stale reader runs first; healthy output does NOT call agent
        db._exec("UPDATE items SET status='gone' WHERE source_id=?", (sid,))
        rich_items = [
            {"title": f"Job {k}", "url": f"https://ex.com/jobs/{k}", "org": f"Org {k}", "deadline": "2027-06-01"}
            for k in range(4)
        ]
        rich_code = f"def fetch():\n    return {rich_items!r}\n"
        rich_stats = json.dumps(scanner.compute_item_stats(rich_items))
        old_ts = (date.today() - timedelta(days=10)).isoformat() + " 00:00:00"
        db.update_source(sid, extractor=rich_code, last_good_extractor=rich_code, extractor_ok=1,
                         extractor_updated=old_ts, last_good_stats=rich_stats)
        with patch.object(tasks, "learn_source") as m_learn:
            res_healthy = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 0)
        self.assertEqual(len(res_healthy), 4)
        self.assertGreater(db.source_by_id(sid)["extractor_updated"], old_ts)

        # 7. Weekly check DOES relearn when output is anomalous vs history (URL pattern / fill rate collapse)
        drifted_items = [{"title": f"Post {k}", "url": f"https://ex.com/blog/{k}"} for k in range(4)]
        drifted_code = f"def fetch():\n    return {drifted_items!r}\n"
        db.update_source(sid, extractor=drifted_code, last_good_extractor=rich_code, extractor_ok=1,
                         extractor_updated=old_ts, last_good_stats=rich_stats)
        with patch.object(tasks, "learn_source", return_value={"items": rich_items, "code": rich_code, "log": ""}) as m_learn:
            res_healed = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)
        self.assertEqual(len(res_healed), 4)

        # 8. If weekly anomalous relearn fails, weekly fallback returns reader items and keeps extractor_ok=1
        db.update_source(sid, extractor=drifted_code, last_good_extractor=rich_code, extractor_ok=1,
                         extractor_updated=old_ts, last_good_stats=rich_stats)
        with patch.object(tasks, "learn_source", side_effect=RuntimeError("quota used")):
            res_fb = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(len(res_fb), 4)
        self.assertEqual(db.source_by_id(sid)["extractor_ok"], 1)

        # 9. verify_weekly=0 skips weekly check entirely even when stale & drifted
        db.set_settings({"verify_weekly": "0"})
        db.update_source(sid, extractor=drifted_code, extractor_ok=1, extractor_updated=old_ts, last_good_stats=rich_stats)
        with patch.object(tasks, "learn_source") as m_learn:
            scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 0)

    def test_verify_reader_output_checks_fill_rates_duplicates_and_counts(self):
        agent_items = [
            {"title": f"Role {i}", "url": f"https://ex.com/{i}", "org": f"Org {i}", "deadline": "2027-07-01"}
            for i in range(4)
        ]
        self.assertTrue(scanner.verify_reader_output(agent_items, agent_items)[0])
        # Missing deadline fill rate rejected
        no_dl = [{"title": i["title"], "url": i["url"], "org": i["org"]} for i in agent_items]
        ok_dl, reason_dl = scanner.verify_reader_output(agent_items, no_dl)
        self.assertFalse(ok_dl)
        self.assertIn("deadline", reason_dl)
        # Duplicate items (>50% identical title+url) rejected even when agrees() passes
        small_agent = agent_items[:2]
        dups = [agent_items[0], agent_items[1], agent_items[0], agent_items[0], agent_items[0], agent_items[0]]
        ok_dup, reason_dup = scanner.verify_reader_output(small_agent, dups)
        self.assertFalse(ok_dup)
        self.assertIn("duplicate", reason_dup)

    def test_shared_readers_concurrent_learning_progressive_saves_and_lock(self):
        db.set_settings({"agent": "claude", "parallel": "2"})
        w1 = db.add_watch("W1", "req1")
        w2 = db.add_watch("W2", "req2")
        # Same canonical URL in two different watches should learn only once
        db.add_source("Board 0", "https://other.example.com/jobs", watch_id=w1)
        db.add_source("Board A", "https://shared.example.com/jobs/", watch_id=w1)
        db.add_source("Board B", "https://shared.example.com/jobs?utm_source=test", watch_id=w2)

        learn_urls = []
        active_lock = threading.Lock()
        conc = {"cur": 0, "peak": 0}
        shared_code = (
            "def fetch():\n"
            "    return [{'title': 'Shared Job', 'url': 'https://shared.example.com/jobs/1'}]\n"
        )
        both_started = threading.Barrier(2, timeout=2.0)
        other_gate = threading.Event()

        def fake_learn(src, fs, cancel_event=None):
            with active_lock:
                learn_urls.append(src["url"])
                conc["cur"] += 1
                conc["peak"] = max(conc["peak"], conc["cur"])
            try:
                both_started.wait()
                if "other.example.com" in src["url"]:
                    other_gate.wait(timeout=3.0)
                    return {
                        "items": [{"title": "Other Job", "url": "https://other.example.com/jobs/1"}],
                        "code": "def fetch(): return [{'title': 'Other Job', 'url': 'https://other.example.com/jobs/1'}]\n",
                        "log": "",
                    }
                return {"items": [{"title": "Shared Job", "url": "https://shared.example.com/jobs/1"}], "code": shared_code, "log": ""}
            finally:
                with active_lock:
                    conc["cur"] -= 1

        observed_progressive = {"found_mid_scan": 0, "learning_total": 0}

        def watcher():
            for _ in range(80):
                prog = scanner.get_progress()
                if prog["learning_total"] > 0:
                    observed_progressive["learning_total"] = max(observed_progressive["learning_total"], prog["learning_total"])
                if len(db.items()) >= 1 and prog["scan_id"]:
                    det = db.scan_detail(prog["scan_id"])
                    if det and det["found"] >= 1:
                        observed_progressive["found_mid_scan"] = det["found"]
                        other_gate.set()
                        return
                time.sleep(0.05)
            other_gate.set()

        t_watch = threading.Thread(target=watcher, daemon=True)
        with patch.object(tasks, "learn_source", side_effect=fake_learn), \
             patch.object(tasks, "classify", return_value={}):
            t_watch.start()
            scan_id = scanner.run_scan("manual")
            t_watch.join(timeout=5)

        self.assertIsNotNone(scan_id)
        # Only 2 learns happened (shared URL learned once, other URL learned once) concurrently
        self.assertEqual(len(learn_urls), 2)
        self.assertEqual(conc["peak"], 2)
        self.assertGreaterEqual(observed_progressive["found_mid_scan"], 1)
        self.assertGreaterEqual(observed_progressive["learning_total"], 2)

        # Cross-process DB scan lock prevents concurrent run_scan / start_scan_async
        self.assertTrue(db.acquire_scan_lock("external-proc"))
        try:
            self.assertIsNone(scanner.run_scan("manual"))
            self.assertFalse(scanner.start_scan_async("manual"))
        finally:
            db.release_scan_lock("external-proc")

    def test_test_source_preview_and_webhook_wiring(self):
        db.set_settings({"agent": "claude", "notify": "1", "webhook_url": "https://hooks.example.com/lookout", "webhook_allow_local": "1"})
        sid = db.add_source("PreviewBoard", "https://preview.example.com/jobs")
        db.update_source(sid, extractor="def fetch(): return [{'title': 'Good Intern', 'url': 'https://preview.example.com/1'}, {'title': 'Senior Lead', 'url': 'https://preview.example.com/2'}]",
                         extractor_ok=1, extractor_updated=db.now())
        db.add_filter("exclude", "senior")

        res = scanner.test_source(db.source_by_id(sid))
        self.assertTrue(res["ok"])
        self.assertEqual(res["method"], "reader")
        self.assertEqual(res["count"], 2)
        self.assertEqual(res["matching"], 1)
        # test_source does not persist items to DB
        self.assertEqual(db.items(), [])

        # Unlearned source falls back to tasks.quick_read_source
        sid2 = db.add_source("FreshBoard", "https://fresh.example.com/jobs")
        with patch.object(tasks, "quick_read_source", return_value={"items": [{"title": "Fresh Role", "url": "https://fresh.example.com/1"}], "log": ""}):
            res2 = scanner.test_source(db.source_by_id(sid2))
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["method"], "agent_preview")
        self.assertEqual(res2["count"], 1)

        # Scan finished triggers notify.webhook when new/updated items exist
        db.update_source(sid2, active=0)
        hook_calls = []

        def fake_webhook(url, payload, allow_local=False):
            hook_calls.append((url, payload, allow_local))
            return True, "ok"

        with patch.object(notify, "webhook", new=fake_webhook):
            scan_id = scanner.run_scan("manual", only=[sid])
        self.assertIsNotNone(scan_id)
        self.assertEqual(len(hook_calls), 1)
        hook_url, payload, allow_local = hook_calls[0]
        self.assertEqual(hook_url, "https://hooks.example.com/lookout")
        self.assertEqual(payload["event"], "scan_finished")
        self.assertEqual(payload["new_count"], 1)
        self.assertTrue(allow_local)

    def test_mark_gone(self):
        flag = self.tmp / "second_scan.flag"
        (self.plugin_dir / "p.py").write_text(
            "from pathlib import Path\n"
            f"FLAG = Path({str(flag)!r})\n"
            "def fetch():\n"
            "    if not FLAG.exists():\n"
            "        FLAG.write_text('1', encoding='utf-8')\n"
            "        return [{'title': 'A', 'url': 'https://ex.com/a'}, {'title': 'B', 'url': 'https://ex.com/b'}]\n"
            "    return [{'title': 'A', 'url': 'https://ex.com/a'}]\n"
            "SOURCES = [{'name': 'P', 'url': 'https://ex.com', 'fetch': fetch}]\n",
            encoding="utf-8",
        )
        scanner.run_scan()
        scanner.run_scan()
        by_title = {i["title"]: i for i in db.items()}
        self.assertEqual(by_title["A"]["status"], "open")
        self.assertEqual(by_title["B"]["status"], "gone")

    def test_cancel_mid_scan_kills_subprocess_skips_filter_and_preserves_state(self):
        db.set_settings({"agent": "claude"})
        sid = db.add_source("SlowSite", "https://slow.example.com")
        # Healthy extractor that sleeps 30s
        slow_code = "import time\ndef fetch():\n    time.sleep(30)\n    return []\n"
        db.update_source(sid, extractor=slow_code, extractor_ok=1, extractor_updated=db.now())
        old_scan = db.start_scan("manual", "claude", "", [])
        db.save_item(old_scan, db.source_by_id(sid), {"id": "keep1", "title": "Keep", "url": "https://slow.example.com/1"})

        scan_ids = []

        def run():
            scan_ids.append(scanner.run_scan("manual"))

        with patch.object(tasks, "classify") as m_cls, \
             patch.object(agents, "close_browser_session"):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with agents._proc_lock:
                    if agents._running:
                        break
                time.sleep(0.05)
            t0 = time.monotonic()
            scanner.cancel_scan()
            t.join(timeout=5.0)
            elapsed = time.monotonic() - t0

        self.assertFalse(t.is_alive())
        self.assertLess(elapsed, 4.0)
        m_cls.assert_not_called()
        detail = db.scan_detail(scan_ids[0])
        self.assertEqual(detail["status"], "cancelled")
        # Healthy extractor and previously open item must not be broken/marked gone on cancel
        self.assertEqual(db.source_by_id(sid)["extractor_ok"], 1)
        self.assertEqual(db.items()[0]["status"], "open")

    def test_watch_and_global_semantic_filter_scoping(self):
        db.set_settings({"agent": "claude"})
        w1 = db.add_watch("Internships", "internships")
        w2 = db.add_watch("Hackathons", "hackathons")
        db.add_filter("include", "python")
        wf1 = db.add_filter("type", "internship", watch_id=w1)
        db.add_filter("type", "hackathon", watch_id=w2)

        s1 = db.add_source("Board1", "https://b1.example.com", watch_id=w1)
        s2 = db.add_source("Board2", "https://b2.example.com", watch_id=w2)
        db.update_source(s1, extractor="def fetch(): return [{'title': 'Python Intern', 'url': 'https://b1.example.com/1'}]",
                         extractor_ok=1, extractor_updated=db.now())
        db.update_source(s2, extractor="def fetch(): return [{'title': 'Python Panel', 'url': 'https://b2.example.com/1'}]",
                         extractor_ok=1, extractor_updated=db.now())

        calls = []

        def fake_classify(items, fs, cancel_event=None):
            calls.append((items, fs))
            kinds = {f["value"] for f in fs}
            out = {}
            for it in items:
                if "internship" in kinds:
                    out[it["id"]] = "Intern" in it["title"]
                else:
                    out[it["id"]] = "Hackathon" in it["title"]
            return out

        with patch.object(tasks, "classify", side_effect=fake_classify):
            scan_id = scanner.run_scan("manual")

        self.assertEqual(len(calls), 2)
        by_title = {i["title"]: i for i in db.items()}
        self.assertEqual(by_title["Python Intern"]["match"], 1)
        self.assertEqual(by_title["Python Panel"]["match"], 0)
        self.assertEqual(db.scan_detail(scan_id)["found"], 1)

        # Updating w1 filter changes w1's match_key without touching w2's match_key
        key_w2_before = by_title["Python Panel"]["match_key"]
        db.update_filter(wf1, False)
        key_w1_after = scanner.filter_match_key(db.filters(True, watch_id=None), db.filters(True, watch_id=w1))
        key_w2_after = scanner.filter_match_key(db.filters(True, watch_id=None), db.filters(True, watch_id=w2))
        self.assertNotEqual(key_w1_after, by_title["Python Intern"]["match_key"])
        self.assertEqual(key_w2_after, key_w2_before)


if __name__ == "__main__":
    unittest.main()
