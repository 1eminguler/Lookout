"""Unit tests for lookout.scanner."""
import sys
import threading
import time
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from lookout import agents, db, scanner, tasks
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

    def test_normalise_strips_html_placeholders_and_tracking(self):
        src = {"name": "Board"}
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

    def test_self_healing_triggers_and_weekly_recheck(self):
        db.set_settings({"agent": "claude", "verify_weekly": "1"})
        ok_code = "def fetch():\n    return [{'title': 'A', 'url': 'https://ex.com/a'}]\n"
        learned_ret = {"items": [{"title": "A", "url": "https://ex.com/a"}], "code": ok_code, "log": ""}

        # 1. Runtime error from broken extractor triggers relearn
        sid = db.add_source("Site", "https://ex.com")
        db.update_source(sid, extractor="def fetch(): raise ZeroDivisionError()", extractor_ok=1,
                         extractor_updated=db.now())
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            res = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)
        self.assertEqual(len(res), 1)
        self.assertEqual(db.source_by_id(sid)["extractor_ok"], 1)

        # 2. Empty result when site previously had open items triggers relearn
        scan1 = db.start_scan("manual", "claude", "", [])
        for k in range(5):
            db.save_item(scan1, db.source_by_id(sid), {"id": f"old{k}", "title": f"Old {k}", "url": f"https://ex.com/{k}"})
        db.update_source(sid, extractor="def fetch(): return []", extractor_ok=1, extractor_updated=db.now())
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)

        # 3. Sharp drop (<30% of >=5 open items) triggers relearn
        db.update_source(sid, extractor="def fetch(): return [{'title': 'Only1', 'url': 'https://ex.com/1'}]",
                         extractor_ok=1, extractor_updated=db.now())
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)

        # 4. Rows without title/url (<80% usable) triggers relearn
        bad_rows = "def fetch(): return [{'title': '', 'url': 'bad'}, {'title': 'Ok', 'url': 'https://ex.com/ok'}]"
        db.update_source(sid, extractor=bad_rows, extractor_ok=1, extractor_updated=db.now())
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)

        # 5. Weekly re-check: >7 days old relearns when verify_weekly=1, skips when verify_weekly=0
        db._exec("UPDATE items SET status='gone' WHERE source_id=?", (sid,))
        old_ts = (date.today() - timedelta(days=10)).isoformat() + " 00:00:00"
        db.update_source(sid, extractor=ok_code, extractor_ok=1, extractor_updated=old_ts)
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)

        db.set_settings({"verify_weekly": "0"})
        db.update_source(sid, extractor=ok_code, extractor_ok=1, extractor_updated=old_ts)
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 0)

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
