"""Unit tests for lookout.scanner."""
import hashlib
import json
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from lookout import agents, db, extractors, notify, scanner, tasks
from tests.helpers import TempLookoutTestCase, make_fake_agent


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

    def test_normalise_strips_html_placeholders_tracking_and_keys_by_title_and_url(self):
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

        it_edited = scanner.normalise({"title": "ML Intern (Updated)", "url": "https://ex.com/jobs?id=7"}, src)
        self.assertNotEqual(it["id"], it_edited["id"])
        raw = {"id": "7", "title": "ML Intern", "url": "https://ex.com/jobs?id=7"}
        self.assertEqual(scanner.normalise(raw, src)["id"],
                         scanner.normalise(dict(raw, title=it_edited["title"]), src)["id"])

    def test_scan_keeps_distinct_titles_with_same_url_and_preserves_legacy_item(self):
        db.set_settings({"agent": "claude"})
        sid = db.add_source("Careers", "https://example.com/jobs")
        source = db.source_by_id(sid)
        url = "https://example.com/careers"
        raw = [{"title": title, "url": url} for title in ("Summer Internship", "Young Talent")]
        legacy_id = hashlib.sha1(f"src:{sid}|{url}".encode()).hexdigest()[:20]
        scan_id = db.start_scan("manual", "claude", "", [])
        db.save_item(scan_id, source, dict(raw[0], id=legacy_id))
        db.set_item(legacy_id, action="saved")
        db.finish_scan(scan_id, "done")
        with patch.object(scanner, "read_agent_source", return_value=raw):
            for _ in range(2):
                scan_id = scanner.run_scan("manual")
                items = {item["title"]: item for item in db.scan_detail(scan_id)["items"]}
                self.assertEqual(set(items), {"Summer Internship", "Young Talent"})
                self.assertEqual(items["Summer Internship"]["id"], legacy_id)
                self.assertEqual(items["Summer Internship"]["action"], "saved")
                self.assertNotEqual(items["Young Talent"]["id"], legacy_id)

    def test_passes_exclude_deadlines_and_turkish_dotted_i(self):
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

        # Turkish uppercase İ / I\u0307 normalization in both item text and filter value
        tr_lower_fs = [{"kind": "exclude", "value": "istanbul, ingilizce"}]
        self.assertFalse(scanner.passes({"title": "İSTANBUL Staj Programı", "deadline": tomorrow}, tr_lower_fs))
        self.assertFalse(scanner.passes({"title": "Yaz Stajı", "location": "I\u0307STANBUL", "deadline": tomorrow}, tr_lower_fs))
        self.assertFalse(scanner.passes({"title": "Backend Intern", "summary": "İngilizce gerekli", "deadline": tomorrow}, tr_lower_fs))

        tr_upper_fs = [{"kind": "exclude", "value": "İSTANBUL, İNGİLİZCE"}]
        self.assertFalse(scanner.passes({"title": "istanbul staj", "deadline": tomorrow}, tr_upper_fs))
        self.assertFalse(scanner.passes({"title": "Backend Intern", "summary": "ingilizce bilen", "deadline": tomorrow}, tr_upper_fs))
        self.assertTrue(scanner.passes({"title": "Ankara Yaz Stajı", "deadline": tomorrow}, tr_upper_fs))

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

    def test_transient_vs_structural_failures_blocked_and_manual_bypass(self):
        # classify_error matrix: blocked, transient (including unknown), structural
        for exc in (
            scanner.BlockedError("HTTP 403"),
            RuntimeError("HTTP 401 Unauthorized"),
            RuntimeError("HTTP 403 Forbidden"),
            RuntimeError("HTTP 451 Unavailable For Legal Reasons"),
            RuntimeError("Cloudflare Attention Required / Just a moment"),
            RuntimeError("CAPTCHA challenge triggered"),
            RuntimeError("Access Denied"),
        ):
            with self.subTest(blocked=str(exc)):
                self.assertEqual(scanner.classify_error(exc), "blocked")

        for exc in (
            scanner.TransientError("cooldown"),
            TimeoutError("timed out"),
            ConnectionError("reset"),
            RuntimeError("extractor took longer than 180s"),
            RuntimeError("SSL: CERTIFICATE_VERIFY_FAILED"),
            RuntimeError("[WinError 10054] An existing connection was forcibly closed"),
            RuntimeError("HTTP 503 for https://ex.com: Service Unavailable"),
            RuntimeError("HTTP 429 Too Many Requests"),
            RuntimeError("completely unrecognized mysterious hiccup"),
        ):
            with self.subTest(transient=str(exc)):
                self.assertEqual(scanner.classify_error(exc), "transient")

        for exc in (
            RuntimeError("HTTP 404 for https://ex.com"),
            RuntimeError("HTTP 410 Gone"),
            RuntimeError("extractor failed: ZeroDivisionError"),
            RuntimeError("extractor failed: KeyError: 'jobs'"),
            RuntimeError("invalid json from https://ex.com/api"),
            RuntimeError("most items have no title or link"),
            RuntimeError("returned nothing; page/API shape changed (container missing)"),
            ValueError("no fetch() function"),
            TypeError("fetch() must return a list"),
        ):
            with self.subTest(structural=str(exc)):
                self.assertEqual(scanner.classify_error(exc), "structural")

        db.set_settings({"agent": "claude", "verify_weekly": "0"})
        good_code = "def fetch():\n    return [{'title': 'A', 'url': 'https://ex.com/a'}]\n"
        sid = db.add_source("Site", "https://ex.com")
        db.update_source(sid, extractor=good_code, last_good_extractor=good_code, extractor_ok=1, extractor_updated=db.now())

        # Blocked error raises BlockedError, keeps reader intact, skips learn_source, sets blocked cooldown
        with patch.object(scanner, "_run_fast_reader", side_effect=RuntimeError("HTTP 403 Forbidden (Cloudflare)")), \
             patch.object(tasks, "learn_source") as m_learn:
            with self.assertRaises(scanner.BlockedError):
                scanner.read_agent_source(db.source_by_id(sid), [])
            m_learn.assert_not_called()
        s_blk = db.source_by_id(sid)
        self.assertEqual(s_blk["extractor_ok"], 1)
        self.assertEqual(s_blk["last_error_kind"], "blocked")
        self.assertTrue(s_blk["retry_after"] > db.now())

        # Within blocked cooldown, non-manual read fails fast without invoking reader or agent
        with patch.object(scanner, "_run_fast_reader") as m_fast, \
             patch.object(tasks, "learn_source") as m_learn:
            with self.assertRaises(scanner.BlockedError) as ctx_blk:
                scanner.read_agent_source(db.source_by_id(sid), [])
            self.assertIn("Blocked cooldown", str(ctx_blk.exception))
            m_fast.assert_not_called()
            m_learn.assert_not_called()

        # Transient failure (503, timeout, unknown) keeps reader intact and sets retry_after backoff
        db.update_source(sid, retry_after=None, last_error_kind=None, fail_count=0)
        for exc in (
            TimeoutError("timed out"),
            RuntimeError("HTTP 503 for https://ex.com: Service Unavailable"),
            RuntimeError("unexpected transient glitch"),
        ):
            db.update_source(sid, retry_after=None, last_error_kind=None, fail_count=0)
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

        # Scheduled scan respects transient backoff, whereas manual scan bypasses backoff
        with patch.object(scanner, "_run_fast_reader") as m_fast, \
             patch.object(tasks, "learn_source") as m_learn:
            sc_sched = scanner.run_scan("scheduled")
            self.assertIsNotNone(sc_sched)
            m_fast.assert_not_called()
            m_learn.assert_not_called()
            det_sched = db.scan_detail(sc_sched)
            self.assertEqual(det_sched["sources"][0]["ok"], 0)
            self.assertIn("Waiting for retry backoff", det_sched["sources"][0]["error"])

        with patch.object(scanner, "_run_fast_reader", return_value=[{"title": "A", "url": "https://ex.com/a"}]) as m_fast, \
             patch.object(tasks, "learn_source") as m_learn:
            sc_man = scanner.run_scan("manual")
            self.assertIsNotNone(sc_man)
            self.assertEqual(m_fast.call_count, 1)
            m_learn.assert_not_called()
        s_after_man = db.source_by_id(sid)
        self.assertEqual(s_after_man["fail_count"], 0)
        self.assertIsNone(s_after_man["retry_after"])
        self.assertIsNone(s_after_man["last_error_kind"])

        # Structural error requires STRUCTURAL_FAIL_THRESHOLD=2 consecutive failures before relearning
        repaired_code = "def fetch():\n    return [{'title': 'B', 'url': 'https://ex.com/b'}]\n"
        learned_ret = {"items": [{"title": "B", "url": "https://ex.com/b"}], "code": repaired_code, "log": ""}
        with patch.object(scanner, "_run_fast_reader", side_effect=RuntimeError("HTTP 404 for https://ex.com")), \
             patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            with self.assertRaises(RuntimeError) as ctx_s1:
                scanner.read_agent_source(db.source_by_id(sid), [])
            self.assertIn("1/2", str(ctx_s1.exception))
            m_learn.assert_not_called()
            s_mid = db.source_by_id(sid)
            self.assertEqual(s_mid["struct_fail_count"], 1)
            self.assertEqual(s_mid["extractor_ok"], 1)

            # 2nd consecutive structural failure triggers relearn and resets struct_fail_count
            got = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)
        self.assertEqual(got, [{"title": "B", "url": "https://ex.com/b"}])
        s_repaired = db.source_by_id(sid)
        self.assertEqual(s_repaired["last_good_extractor"], repaired_code)
        self.assertEqual(s_repaired["struct_fail_count"], 0)
        self.assertEqual(s_repaired["extractor_ok"], 1)

    def test_self_healing_triggers_last_good_preservation_and_cheap_weekly_check(self):
        db.set_settings({"agent": "claude", "verify_weekly": "1"})
        ok_code = "def fetch():\n    return [{'title': 'A', 'url': 'https://ex.com/jobs/a', 'org': 'Org', 'deadline': '2027-06-01'}]\n"
        learned_ret = {"items": [{"title": "A", "url": "https://ex.com/jobs/a", "org": "Org", "deadline": "2027-06-01"}],
                       "code": ok_code, "log": ""}

        # 1. Runtime structural error triggers relearn after 2 consecutive structural failures
        sid = db.add_source("Site", "https://ex.com")
        db.update_source(sid, extractor="def fetch(): raise ZeroDivisionError()", extractor_ok=1,
                         extractor_updated=db.now())
        with patch.object(tasks, "learn_source", return_value=learned_ret) as m_learn:
            with self.assertRaises(RuntimeError) as ctx1:
                scanner.read_agent_source(db.source_by_id(sid), [])
            self.assertIn("1/2", str(ctx1.exception))
            m_learn.assert_not_called()
            res = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)
        self.assertEqual(len(res), 1)
        self.assertEqual(db.source_by_id(sid)["last_good_extractor"], ok_code)

        # 2. Failed heal (agent error or disagreeing reader) NEVER overwrites last_good_extractor
        db.update_source(sid, extractor="def fetch(): raise KeyError('k')", extractor_ok=1,
                         extractor_updated=db.now(), struct_fail_count=1)
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

        # Restore working extractor cleanly
        db.update_source(sid, extractor=ok_code, last_good_extractor=ok_code, extractor_ok=1,
                         extractor_updated=db.now(), struct_fail_count=0, learn_fail_count=0, learn_retry_after=None)

        # 3. A nonempty read that drops from five listings to one closes the missing listings.
        scan1 = db.start_scan("manual", "claude", "", [])
        src_row = db.source_by_id(sid)
        for k in range(5):
            db.save_item(scan1, src_row, {"id": f"old{k}", "title": f"Old {k}", "url": f"https://ex.com/{k}"})
        db.finish_scan(scan1, "done")

        db.update_source(sid, extractor="def fetch(): return [{'title': 'Only1', 'url': 'https://ex.com/0'}]",
                         extractor_ok=1, extractor_updated=db.now(), baseline_done=1)
        with patch.object(tasks, "learn_source") as m_learn:
            scan_drop = scanner.run_scan("manual", only=[sid])
        self.assertIsNotNone(scan_drop)
        m_learn.assert_not_called()
        self.assertEqual(db.source_by_id(sid)["extractor_ok"], 1)
        self.assertEqual(db.source_open_item_count(sid), 1)
        self.assertEqual(db.item_by_id("old1")["status"], "gone")

        # Rows without title/url (<80% usable) count as structural failures.
        bad_rows = "def fetch(): return [{'title': '', 'url': 'bad'}, {'title': 'Ok', 'url': 'https://ex.com/ok'}]"
        db.update_source(sid, extractor=bad_rows, extractor_ok=1, extractor_updated=db.now(), struct_fail_count=1)
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
                         extractor_updated=old_ts, last_good_stats=rich_stats, struct_fail_count=0,
                         learn_fail_count=0, learn_retry_after=None)
        with patch.object(tasks, "learn_source") as m_learn:
            res_healthy = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 0)
        self.assertEqual(len(res_healthy), 4)
        self.assertGreater(db.source_by_id(sid)["extractor_updated"], old_ts)

        # 7. Weekly check DOES relearn when output is anomalous vs history (URL pattern / fill rate collapse)
        drifted_items = [{"title": f"Post {k}", "url": f"https://ex.com/blog/{k}"} for k in range(4)]
        drifted_code = f"def fetch():\n    return {drifted_items!r}\n"
        db.update_source(sid, extractor=drifted_code, last_good_extractor=rich_code, extractor_ok=1,
                         extractor_updated=old_ts, last_good_stats=rich_stats, learn_fail_count=0, learn_retry_after=None)
        with patch.object(tasks, "learn_source", return_value={"items": rich_items, "code": rich_code, "log": ""}) as m_learn:
            res_healed = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 1)
        self.assertEqual(len(res_healed), 4)

        # 8. If weekly anomalous relearn fails, weekly fallback returns reader items, keeps extractor_ok=1,
        #    AND bumps extractor_updated so it does not stay stale on the next scan
        db.update_source(sid, extractor=drifted_code, last_good_extractor=rich_code, extractor_ok=1,
                         extractor_updated=old_ts, last_good_stats=rich_stats, learn_fail_count=0, learn_retry_after=None)
        with patch.object(tasks, "learn_source", side_effect=RuntimeError("quota used")):
            res_fb = scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(len(res_fb), 4)
        s_fb = db.source_by_id(sid)
        self.assertEqual(s_fb["extractor_ok"], 1)
        self.assertGreater(s_fb["extractor_updated"], old_ts)

        # 9. verify_weekly=0 skips weekly check entirely even when stale & drifted
        db.set_settings({"verify_weekly": "0"})
        db.update_source(sid, extractor=drifted_code, extractor_ok=1, extractor_updated=old_ts, last_good_stats=rich_stats)
        with patch.object(tasks, "learn_source") as m_learn:
            scanner.read_agent_source(db.source_by_id(sid), [])
        self.assertEqual(m_learn.call_count, 0)

    def test_suspect_empty_reads_require_two_consecutive_scans(self):
        db.set_settings({"agent": "claude", "verify_weekly": "0"})
        sid = db.add_source("Careers", "https://example.com/jobs")
        other = db.add_source("Other", "https://other.example.com/jobs")
        listing = {"title": "Summer Internship", "url": "https://example.com/jobs/summer"}
        nonempty = f"def fetch(): return [{listing!r}]"
        empty = "def fetch(): return []"
        with patch.object(tasks, "learn_source") as learn:
            for code, count, expected_open in (
                (nonempty, 0, 1),
                (empty, 1, 1),
                ("def fetch(): raise RuntimeError('connection timed out')", 0, 1),
                (empty, 1, 1),
                (nonempty, 0, 1),
                (empty, 1, 1),
                (empty, 2, 0),
            ):
                with self.subTest(code=code, count=count):
                    db.update_source(sid, extractor=code, extractor_ok=1, extractor_updated=db.now())
                    with patch.object(db, "mark_gone", wraps=db.mark_gone) as mark_gone:
                        scan_id = scanner.run_scan("manual", only=[sid])
                    self.assertEqual(db.source_by_id(sid)["empty_read_count"], count)
                    self.assertEqual(db.source_by_id(other)["empty_read_count"], 0)
                    self.assertEqual(db.source_open_item_count(sid), expected_open)
                    if count == 1:
                        mark_gone.assert_not_called()
                        result = db.scan_detail(scan_id)["sources"][0]
                        self.assertIn("Suspect empty read", result["error"])
            learn.assert_not_called()

    def test_explicit_empty_reader_closes_items_on_first_scan(self):
        db.set_settings({"agent": "claude", "verify_weekly": "1"})
        sid = db.add_source("Careers", "https://example.com/jobs")
        code = "def fetch(): return [{'empty': True}]"
        old_date = (date.today() - timedelta(days=10)).isoformat()
        db.update_source(sid, extractor=code, extractor_ok=1, extractor_updated=old_date)
        scan_id = db.start_scan("manual", "claude", "", [])
        source = db.source_by_id(sid)
        db.save_item(scan_id, source, {"id": "old", "title": "Intern", "url": source["url"]})
        db.finish_scan(scan_id, "done")
        with patch.object(tasks, "learn_source") as learn:
            scan_id = scanner.run_scan("manual", only=[sid])
        learn.assert_not_called()
        self.assertEqual(db.item_by_id("old")["status"], "gone")
        self.assertEqual(db.source_by_id(sid)["empty_read_count"], 0)
        self.assertEqual(db.scan_detail(scan_id)["sources"][0]["ok"], 1)

    def test_missing_decision_stays_pending_and_retries_changed_content(self):
        db.set_settings({"agent": "claude"})
        sid = db.add_source("Careers", "https://example.com/jobs")
        source = db.source_by_id(sid)
        filters = [{"kind": "location", "value": "remote"}]
        key = scanner.filter_match_key(filters)
        intern = {"id": "intern", "title": "Intern", "url": source["url"], "location": "Ankara"}
        scan_id = db.start_scan("manual", "claude", "", [])
        db.save_item(scan_id, source, intern, match=0, match_key=key)
        db.finish_scan(scan_id, "done")

        scan_id = db.start_scan("manual", "claude", "", [])
        db.save_item(scan_id, source, dict(intern, location="Remote"))
        db.save_item(scan_id, source, dict(intern, id="other", title="Other"))
        with patch.object(tasks, "classify", return_value={"other": False}) as classify:
            scanner.apply_semantic_filters(scan_id, filters)
        sent = {item["id"]: item for item in classify.call_args.args[0]}
        self.assertEqual(sent["intern"]["location"], "Remote")
        self.assertIsNone(db.item_by_id("intern")["match"])
        self.assertIsNone(db.item_by_id("intern")["match_key"])
        self.assertEqual(db.item_by_id("other")["match"], 0)

        with patch.object(tasks, "classify", return_value={"intern": True}) as classify:
            scanner.apply_semantic_filters(scan_id, filters)
        self.assertEqual([item["id"] for item in classify.call_args.args[0]], ["intern"])
        self.assertEqual(db.item_by_id("intern")["match"], 1)

    def test_empty_results_do_not_verify_reader(self):
        self.assertIs(extractors.agrees([], []), extractors.UNVERIFIED)
        ok_empty, reason_empty = scanner.verify_reader_output([], [])
        self.assertFalse(ok_empty)
        self.assertIn("0 items", reason_empty)

        ok_empty_vs_code, reason_empty_vs_code = scanner.verify_reader_output(
            [], [{"title": "Job", "url": "https://ex.com/1"}]
        )
        self.assertFalse(ok_empty_vs_code)
        self.assertIn("unverified", reason_empty_vs_code)

        db.set_settings({"agent": "claude"})
        sid = db.add_source("EmptyLearn", "https://emptylearn.example.com")
        empty_ret = {"items": [], "code": "def fetch():\n    return []\n", "log": "nothing found"}
        with patch.object(tasks, "learn_source", return_value=empty_ret):
            with self.assertRaises(RuntimeError):
                scanner.read_agent_source(db.source_by_id(sid), [])
        s = db.source_by_id(sid)
        self.assertEqual(s["extractor_ok"], 0)
        self.assertEqual(s["learn_fail_count"], 1)

    def test_learn_fail_cooldown_and_pause_after_three_failures(self):
        db.set_settings({"agent": "claude"})
        sid = db.add_source("FlakyLearn", "https://flaky.example.com")

        # 1st failed learn -> learn_fail_count=1, ~30m learn_retry_after, not paused
        t_before = datetime.now()
        with patch.object(tasks, "learn_source", side_effect=RuntimeError("agent error 1")):
            with self.assertRaises(RuntimeError):
                scanner.read_agent_source(db.source_by_id(sid), [])
        s1 = db.source_by_id(sid)
        self.assertEqual(s1["learn_fail_count"], 1)
        self.assertEqual(s1["paused"], 0)
        self.assertIsNotNone(s1["learn_retry_after"])
        dt1 = datetime.strptime(s1["learn_retry_after"], db.TS)
        self.assertGreaterEqual(dt1, t_before + timedelta(minutes=25))

        # Immediate non-forced retry hits relearn cooldown without calling learn_source
        with patch.object(tasks, "learn_source") as m_learn:
            with self.assertRaises(scanner.TransientError) as ctx_cd:
                scanner.read_agent_source(db.source_by_id(sid), [])
            self.assertIn("relearn cooldown", str(ctx_cd.exception))
            m_learn.assert_not_called()

        # 2nd failed learn after cooldown expires -> learn_fail_count=2, ~60m cooldown
        past = (datetime.now() - timedelta(minutes=5)).strftime(db.TS)
        db.update_source(sid, learn_retry_after=past)
        t_before2 = datetime.now()
        with patch.object(tasks, "learn_source", side_effect=RuntimeError("agent error 2")):
            with self.assertRaises(RuntimeError):
                scanner.read_agent_source(db.source_by_id(sid), [])
        s2 = db.source_by_id(sid)
        self.assertEqual(s2["learn_fail_count"], 2)
        self.assertEqual(s2["paused"], 0)
        dt2 = datetime.strptime(s2["learn_retry_after"], db.TS)
        self.assertGreaterEqual(dt2, t_before2 + timedelta(minutes=55))

        # 3rd failed learn -> learn_fail_count=3, paused=1, learn_retry_after=None
        db.update_source(sid, learn_retry_after=past)
        with patch.object(tasks, "learn_source", side_effect=RuntimeError("agent error 3")):
            with self.assertRaises(RuntimeError):
                scanner.read_agent_source(db.source_by_id(sid), [])
        s3 = db.source_by_id(sid)
        self.assertEqual(s3["learn_fail_count"], 3)
        self.assertEqual(s3["paused"], 1)
        self.assertIsNone(s3["learn_retry_after"])
        self.assertIn("paused", (s3["extractor_error"] or "").lower())

        # Direct read raises PausedSourceError and run_scan skips paused sources
        with patch.object(tasks, "learn_source") as m_learn:
            with self.assertRaises(scanner.PausedSourceError):
                scanner.read_agent_source(db.source_by_id(sid), [])
            sc_id = scanner.run_scan("manual")
            m_learn.assert_not_called()
        self.assertEqual(db.scan_detail(sc_id)["sources"], [])

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
        # Same canonical URL + same notes + same filters in two different watches should learn only once
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

    def test_shared_readers_scoped_by_notes_and_watch_filters(self):
        db.set_settings({"agent": "claude", "parallel": "1"})
        w_int = db.add_watch("Internships", "internships")
        w_grant = db.add_watch("Grants", "grants")
        w_int_dup = db.add_watch("Internships Copy", "internships")
        db.add_filter("type", "internship", watch_id=w_int)
        db.add_filter("type", "grant", watch_id=w_grant)
        db.add_filter("type", "internship", watch_id=w_int_dup)

        url = "https://sharedctx.example.com/opportunities"
        s_int = db.add_source("Int Board", url, notes="Internships tab", watch_id=w_int)
        s_grant = db.add_source("Grant Board", url, notes="Grants tab", watch_id=w_grant)
        s_int_dup = db.add_source("Int Mirror", url + "?utm_source=x", notes="Internships tab", watch_id=w_int_dup)

        learned = []

        def fake_learn(src, fs, cancel_event=None):
            learned.append((src["id"], src["notes"]))
            if "Grants" in (src.get("notes") or ""):
                return {
                    "items": [{"title": "Grant A", "url": "https://sharedctx.example.com/grant-a"}],
                    "code": "def fetch():\n    return [{'title': 'Grant A', 'url': 'https://sharedctx.example.com/grant-a'}]\n",
                    "log": "",
                }
            return {
                "items": [{"title": "Intern A", "url": "https://sharedctx.example.com/intern-a"}],
                "code": "def fetch():\n    return [{'title': 'Intern A', 'url': 'https://sharedctx.example.com/intern-a'}]\n",
                "log": "",
            }

        with patch.object(tasks, "learn_source", side_effect=fake_learn), \
             patch.object(tasks, "classify", side_effect=lambda items, fs, **kw: {i["id"]: True for i in items}):
            scanner.run_scan("manual")

        # s_int and s_int_dup share 1 learn; s_grant learns separately -> 2 learns total, no clobbering
        self.assertEqual(len(learned), 2)
        self.assertIn("Intern A", db.source_by_id(s_int)["extractor"])
        self.assertEqual(db.source_by_id(s_int_dup)["extractor"], db.source_by_id(s_int)["extractor"])
        self.assertIn("Grant A", db.source_by_id(s_grant)["extractor"])

    def test_excluded_items_not_marked_gone_and_reevaluate_open_items(self):
        db.set_settings({"agent": "claude"})
        tomorrow = (date.today() + timedelta(days=2)).isoformat()
        in_40d = (date.today() + timedelta(days=40)).isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        db.add_filter("exclude", "senior")
        db.add_filter("deadline_within", "14")

        (self.plugin_dir / "exc_p.py").write_text(
            "def fetch():\n"
            "    return [\n"
            f"        {{'title': 'Python ML Intern', 'url': 'https://ex.example/ok', 'deadline': {tomorrow!r}}},\n"
            f"        {{'title': 'Senior Engineer', 'url': 'https://ex.example/sen', 'deadline': {tomorrow!r}}},\n"
            f"        {{'title': 'Later Intern', 'url': 'https://ex.example/lat', 'deadline': {in_40d!r}}},\n"
            f"        {{'title': 'Expired Intern', 'url': 'https://ex.example/exp', 'deadline': {yesterday!r}}},\n"
            "    ]\n"
            "SOURCES = [{'name': 'ExcPlug', 'url': 'https://ex.example', 'fetch': fetch}]\n",
            encoding="utf-8",
        )

        sc1 = scanner.run_scan("manual")
        sc2 = scanner.run_scan("manual")
        self.assertIsNotNone(sc1)
        self.assertIsNotNone(sc2)

        by_title = {i["title"]: i for i in db.items()}
        self.assertNotIn("Expired Intern", by_title)
        for title in ("Senior Engineer", "Later Intern"):
            row = by_title[title]
            self.assertEqual(row["status"], "open")
            self.assertEqual(row["match"], 0)
            self.assertEqual(row["match_key"], "det_excluded")
            self.assertEqual(row["last_scan"], sc2)
            self.assertEqual(db.item_history(row["id"]), [])

        self.assertEqual(by_title["Python ML Intern"]["match"], 1)
        self.assertEqual(by_title["Python ML Intern"]["match_key"], "none")

        # Adding an exclude filter immediately flips matching open item to det_excluded via reevaluate_open_items()
        f_ml = db.add_filter("exclude", "ml")
        res_add = scanner.reevaluate_open_items()
        self.assertEqual(res_add["excluded"], 1)
        self.assertEqual(db.item_by_id(by_title["Python ML Intern"]["id"])["match"], 0)
        self.assertEqual(db.item_by_id(by_title["Python ML Intern"]["id"])["match_key"], "det_excluded")

        # Deleting that exclude filter restores the item to match=1 when no semantic filters are active
        db.delete_filter(f_ml)
        res_del = scanner.reevaluate_open_items()
        self.assertEqual(res_del["updated"], 1)
        self.assertEqual(db.item_by_id(by_title["Python ML Intern"]["id"])["match"], 1)
        self.assertEqual(db.item_by_id(by_title["Python ML Intern"]["id"])["match_key"], "none")

        # Adding a semantic filter queues open items for re-classification on the next scan
        db.add_filter("include", "python")
        res_sem = scanner.reevaluate_open_items()
        self.assertEqual(res_sem["queued_semantic"], 1)
        self.assertIsNone(db.item_by_id(by_title["Python ML Intern"]["id"])["match_key"])

        with patch.object(tasks, "classify", return_value={by_title["Python ML Intern"]["id"]: True}) as m_cls:
            scanner.run_scan("manual")
        self.assertEqual(m_cls.call_count, 1)
        self.assertNotIn(db.item_by_id(by_title["Python ML Intern"]["id"])["match_key"], (None, "none", "det_excluded"))

    def test_test_source_preview_and_webhook_wiring(self):
        db.set_settings({
            "agent": "claude",
            "notify": "1",
            "webhook_url": "https://hooks.example.com/lookout",
            "webhook_format": "json",
            "webhook_allow_local": "1",
        })
        sid = db.add_source("PreviewBoard", "https://preview.example.com/jobs")
        db.update_source(sid, extractor="def fetch(): return [{'title': 'Good Intern', 'url': 'https://preview.example.com/1'}, {'title': 'Senior Lead', 'url': 'https://preview.example.com/2'}]",
                         extractor_ok=1, extractor_updated=db.now(), baseline_done=1)
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

        # Agent quota error during test_source or scan strips raw RuntimeError: prefix for user-facing error text
        with patch.object(tasks, "quick_read_source", side_effect=RuntimeError("agy quota is used up")):
            res_err = scanner.test_source(db.source_by_id(sid2))
        self.assertFalse(res_err["ok"])
        self.assertEqual(res_err["error"], "agy quota is used up")
        self.assertNotIn("RuntimeError", res_err["error"])

        with patch.object(scanner, "read_agent_source", side_effect=RuntimeError("agy quota is used up")):
            sc_err = scanner.run_scan("manual", only=[sid2])
        det_err = db.scan_detail(sc_err)
        self.assertEqual(det_err["sources"][0]["error"], "agy quota is used up")
        self.assertNotIn("RuntimeError", det_err["sources"][0]["error"])

        # Scan finished triggers notify.webhook when new/updated items exist after baseline
        db.update_source(sid2, active=0)
        hook_calls = []

        def fake_webhook(url, payload, fmt=None, allow_local=False):
            hook_calls.append((url, payload, fmt, allow_local))
            return True, "ok"

        with patch.object(notify, "desktop", return_value=(True, "ok")), \
             patch.object(notify, "webhook", new=fake_webhook):
            scan_id = scanner.run_scan("manual", only=[sid])
        self.assertIsNotNone(scan_id)
        self.assertEqual(len(hook_calls), 1)
        hook_url, payload, fmt, allow_local = hook_calls[0]
        self.assertEqual(hook_url, "https://hooks.example.com/lookout")
        self.assertEqual(payload["event"], "scan_finished")
        self.assertEqual(payload["new_count"], 1)
        self.assertEqual(fmt, "json")
        self.assertTrue(allow_local)

    def test_unified_notifier_baseline_changed_items_and_delivery_failures(self):
        db.set_settings({
            "agent": "claude",
            "notify": "1",
            "smtp_user": "sender@example.com",
            "smtp_password": "pw",
            "mail_to": "a@example.com, b@example.com",
            "webhook_url": "https://hooks.slack.com/services/T/B/X",
            "webhook_format": "slack",
            "webhook_allow_local": "0",
            "telegram_chat_id": "12345",
        })
        state = {"step": 1}

        def fetch_items():
            if state["step"] == 1:
                return [{"title": "Grant One", "url": "https://uni.example/1", "deadline": "2027-06-01"}]
            if state["step"] == 2:
                return [
                    {"title": "Grant One", "url": "https://uni.example/1", "deadline": "2027-06-15"},
                    {"title": "Grant Two", "url": "https://uni.example/2", "deadline": "2027-07-01"},
                ]
            return [
                {"title": "Grant One", "url": "https://uni.example/1", "deadline": "2027-06-15"},
                {"title": "Grant Two", "url": "https://uni.example/2", "deadline": "2027-07-01"},
                {"title": "Grant Three", "url": "https://uni.example/3", "deadline": "2027-08-01"},
            ]

        plugins = {"UniPlug": fetch_items}
        sid = db.add_source("UniPlug", "plugin:uni.py", method="plugin", plugin="UniPlug")
        self.assertEqual(db.source_by_id(sid)["baseline_done"], 0)

        # 1st scan sets silent baseline without sending any notifications
        with patch.object(scanner, "load_plugins", return_value=plugins), \
             patch.object(notify, "desktop") as m_desk, \
             patch.object(notify, "mail_digest") as m_mail, \
             patch.object(notify, "webhook") as m_hook:
            sc1 = scanner.run_scan("manual")
        self.assertIsNotNone(sc1)
        self.assertEqual(db.source_by_id(sid)["baseline_done"], 1)
        m_desk.assert_not_called()
        m_mail.assert_not_called()
        m_hook.assert_not_called()
        self.assertEqual(db.unnotified(), [])

        # 2nd scan finds 1 new item and 1 deadline change -> all channels fire & mark notified
        state["step"] = 2
        with patch.object(scanner, "load_plugins", return_value=plugins), \
             patch.object(notify, "desktop", return_value=(True, "Toast sent")) as m_desk, \
             patch.object(notify, "mail_digest", return_value=(True, "Sent")) as m_mail, \
             patch.object(notify, "webhook", return_value=(True, "HTTP 200")) as m_hook:
            sc2 = scanner.run_scan("manual")
        self.assertIsNotNone(sc2)
        m_desk.assert_called_once()
        self.assertIn("1 new, 1 updated", m_desk.call_args[0][0])
        m_mail.assert_called_once()
        self.assertEqual(len(m_mail.call_args[0][0]), 1)
        self.assertEqual(len(m_mail.call_args.kwargs["changed_items"]), 1)
        m_hook.assert_called_once()
        wh_payload = m_hook.call_args[0][1]
        self.assertEqual(wh_payload["new_count"], 1)
        self.assertEqual(wh_payload["updated_count"], 1)
        self.assertEqual(wh_payload["chat_id"], "12345")
        self.assertEqual(m_hook.call_args.kwargs["fmt"], "slack")
        self.assertFalse(m_hook.call_args.kwargs["allow_local"])
        self.assertEqual(db.recent_notify_errors(), [])
        self.assertEqual(db.unnotified(), [])

        # 3rd scan: remote channel failure logs error and does NOT mark new item notified
        state["step"] = 3
        with patch.object(scanner, "load_plugins", return_value=plugins), \
             patch.object(notify, "desktop", return_value=(True, "Toast sent")), \
             patch.object(notify, "mail_digest", return_value=(True, "Sent")), \
             patch.object(notify, "webhook", return_value=(False, "HTTP 502 Bad Gateway")):
            sc3 = scanner.run_scan("manual")
        self.assertIsNotNone(sc3)
        errs = db.recent_notify_errors()
        self.assertEqual([(e["channel"], e["message"]) for e in errs], [("webhook", "HTTP 502 Bad Gateway")])
        self.assertEqual([i["title"] for i in db.unnotified()], ["Grant Three"])

        # Desktop-only failure (no remote channels configured) also logs error and leaves item unnotified
        db.dismiss_notify_errors()
        db.set_settings({"smtp_user": "", "smtp_password": "", "mail_to": "", "webhook_url": ""})
        with patch.object(notify, "desktop", return_value=(False, "notify-send missing")):
            res_d = scanner.notify_after_scan(sc3)
        self.assertFalse(res_d["sent"])
        self.assertEqual([(e["channel"], e["message"]) for e in db.recent_notify_errors()], [("desktop", "notify-send missing")])
        self.assertEqual([i["title"] for i in db.unnotified()], ["Grant Three"])

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

    def test_honestly_empty_listing_with_repair_round_stores_reason_and_closes_stale_items(self):
        db.set_settings({"agent": "claude", "verify_weekly": "0"})
        sid = db.add_source("ClosedCalls", "https://calls.example.com/list")
        src = db.source_by_id(sid)

        # Seed 12 stale open items on this source
        seed_scan = db.start_scan("manual", "claude", "", [])
        for k in range(12):
            db.save_item(seed_scan, src, {
                "id": f"stale-{k}",
                "title": f"Call #{k}",
                "url": f"https://calls.example.com/detail/{k}",
            }, match=1, match_key="none")
        db.finish_scan(seed_scan, "done")
        self.assertEqual(db.source_open_item_count(sid), 12)

        bad_reader = (
            "def fetch():\n"
            "    fmt = 'https://calls.example.com/page/{0}'\n"
            "    _ = fmt.format(1)\n"
            "    return [{'empty': True}]\n"
        )
        good_empty_reader = "def fetch():\n    return [{'empty': True}]\n"
        reason_text = "all 181 cards show 'Başvurular Tamamlandı'"

        def fake_agent(aid, model, wd, timeout, task_md):
            if len(calls) == 1:
                return {
                    "items.json": [],
                    "result.json": {"empty_reason": reason_text},
                    "extractor.py": bad_reader,
                }
            # Repair round in the same workdir receives the exact check error
            return {"extractor.py": good_empty_reader}

        fake_run, calls = make_fake_agent(fake_agent)
        with patch.object(agents, "run_task", side_effect=fake_run):
            scan_id = scanner.run_scan("manual", only=[sid])

        self.assertIsNotNone(scan_id)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["workdir"], calls[1]["workdir"])
        self.assertIn("dynamic '.format()' calls are not allowed", calls[1]["task_md"])

        updated_src = db.source_by_id(sid)
        self.assertEqual(updated_src["extractor_ok"], 1)
        self.assertEqual(updated_src["learn_fail_count"], 0)
        self.assertEqual(updated_src["empty_reason"], reason_text)
        self.assertEqual(updated_src["empty_read_count"], 0)
        # Shown in db.sources() for the Sources view
        listed_src = next(s for s in db.sources() if s["id"] == sid)
        self.assertEqual(listed_src["empty_reason"], reason_text)

        # All 12 open items are closed (status='gone') and never deleted
        self.assertEqual(db.source_open_item_count(sid), 0)
        stored_items = db.items(source_id=sid)
        self.assertEqual(len(stored_items), 12)
        self.assertTrue(all(it["status"] == "gone" for it in stored_items))

        # Subsequent scan that finds an open item clears empty_reason
        reopened_code = "def fetch():\n    return [{'title': 'New 2027 Call', 'url': 'https://calls.example.com/2027'}]\n"
        db.update_source(sid, extractor=reopened_code, last_good_extractor=reopened_code, extractor_ok=1)
        scanner.run_scan("manual", only=[sid])
        self.assertIsNone(db.source_by_id(sid)["empty_reason"])
        self.assertEqual(db.source_open_item_count(sid), 1)

    def test_repair_round_that_still_fails_check_fails_as_today(self):
        db.set_settings({"agent": "claude", "verify_weekly": "0"})
        sid = db.add_source("BrokenReaderSite", "https://broken.example.com/list")
        bad_round_1 = "class Bad:\n    pass\ndef fetch():\n    return []\n"
        bad_round_2 = "def fetch():\n    tpl = '{0}'\n    return [{'title': tpl.format(1), 'url': 'https://broken.example.com/1'}]\n"

        fake_run, calls = make_fake_agent([
            {
                "results.json": [{"title": "Role 1", "url": "https://broken.example.com/1"}],
                "extractor.py": bad_round_1,
            },
            {
                "extractor.py": bad_round_2,
            },
        ])
        with patch.object(agents, "run_task", side_effect=fake_run):
            got = scanner.read_agent_source(db.source_by_id(sid), [])

        # Exactly one repair round in the same workdir, then fails as today
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["workdir"], calls[1]["workdir"])
        self.assertIn("class definitions are not allowed", calls[1]["task_md"])
        self.assertEqual(len(got), 1)
        s = db.source_by_id(sid)
        self.assertEqual(s["extractor_ok"], 0)
        self.assertEqual(s["learn_fail_count"], 1)
        self.assertIn("dynamic '.format()' calls are not allowed", s["extractor_error"])

    def test_empty_ok_module_marker_also_verifies_honestly_empty_listing(self):
        db.set_settings({"agent": "claude", "verify_weekly": "0"})
        sid = db.add_source("EmptyOkSite", "https://emptyok.example.com/list")
        src = db.source_by_id(sid)
        sc0 = db.start_scan("manual", "claude", "", [])
        db.save_item(sc0, src, {"id": "old-open", "title": "Old", "url": "https://emptyok.example.com/1"})
        db.finish_scan(sc0, "done")

        fake_run, calls = make_fake_agent({
            "results.json": {"items": [], "empty_reason": "No open positions right now"},
            "extractor.py": "EMPTY_OK = True\ndef fetch():\n    return []\n",
        })
        with patch.object(agents, "run_task", side_effect=fake_run):
            scanner.read_agent_source(db.source_by_id(sid), [])

        self.assertEqual(len(calls), 1)
        s = db.source_by_id(sid)
        self.assertEqual(s["extractor_ok"], 1)
        self.assertEqual(s["empty_reason"], "No open positions right now")
        self.assertEqual(db.item_by_id("old-open")["status"], "gone")


if __name__ == "__main__":
    unittest.main()
