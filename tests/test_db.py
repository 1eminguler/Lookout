"""Tests for SQLite initialization, schema migrations, watches, interrupted cleanup, CRUD and agent usage."""
import contextlib
import sqlite3
import unittest
from datetime import date, datetime, timedelta

from lookout import db
from tests.helpers import LookoutTestCase, seed_db


class TestDatabase(LookoutTestCase):
    def test_init_empty_dir_creates_tables_and_defaults(self):
        self.assertTrue(db.DB_PATH.exists())
        s = db.get_settings()
        self.assertEqual(s["smtp_host"], "smtp.gmail.com")
        self.assertEqual(s["parallel"], "2")
        self.assertEqual(db.watches(), [])
        self.assertEqual(db.sources(), [])
        self.assertEqual(db.filters(), [])
        self.assertEqual(db.items(), [])
        self.assertEqual(db.scans(), [])
        self.assertEqual(db.discoveries(), [])

    def test_migration_from_original_schema_preserves_data(self):
        db.DB_PATH.unlink()
        old_sql = """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, url TEXT NOT NULL, notes TEXT DEFAULT '',
            method TEXT DEFAULT 'agent', plugin TEXT, recipe TEXT,
            active INTEGER DEFAULT 1, created TEXT NOT NULL
        );
        CREATE TABLE filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL, value TEXT NOT NULL,
            active INTEGER DEFAULT 1, created TEXT NOT NULL
        );
        CREATE TABLE scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started TEXT NOT NULL, finished TEXT, trigger TEXT DEFAULT 'manual',
            agent TEXT, model TEXT, status TEXT DEFAULT 'running',
            found INTEGER DEFAULT 0, new INTEGER DEFAULT 0, filters TEXT, note TEXT
        );
        CREATE TABLE scan_sources (
            scan_id INTEGER NOT NULL, source_id INTEGER, source_name TEXT NOT NULL,
            ok INTEGER, count INTEGER DEFAULT 0, new INTEGER DEFAULT 0,
            error TEXT, ms INTEGER DEFAULT 0, PRIMARY KEY (scan_id, source_name)
        );
        CREATE TABLE items (
            id TEXT PRIMARY KEY, source_id INTEGER, source_name TEXT NOT NULL,
            title TEXT NOT NULL, org TEXT, category TEXT, url TEXT NOT NULL,
            deadline TEXT, start_date TEXT, location TEXT, summary TEXT, extra TEXT,
            status TEXT DEFAULT 'open', first_scan INTEGER, last_scan INTEGER,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            notified INTEGER DEFAULT 0, action TEXT, note TEXT, remind_on TEXT
        );
        CREATE TABLE scan_items (
            scan_id INTEGER NOT NULL, item_id TEXT NOT NULL,
            is_new INTEGER DEFAULT 0, PRIMARY KEY (scan_id, item_id)
        );
        CREATE TABLE agent_usage (
            agent TEXT NOT NULL, model TEXT NOT NULL, day TEXT NOT NULL,
            calls INTEGER DEFAULT 0, ok INTEGER DEFAULT 0, last_call TEXT,
            last_error TEXT, resets_at TEXT, PRIMARY KEY (agent, model, day)
        );
        """
        with contextlib.closing(sqlite3.connect(str(db.DB_PATH))) as c:
            c.executescript(old_sql)
            c.execute("INSERT INTO sources(name, url, created) VALUES('Legacy Site', 'https://legacy.example', '2026-01-01 00:00:00')")
            c.execute("INSERT INTO scans(started, status) VALUES('2026-01-01 00:00:00', 'done')")
            c.execute("INSERT INTO items(id, source_id, source_name, title, url, first_seen, last_seen) "
                      "VALUES('old-1', 1, 'Legacy Site', 'Old Role', 'https://legacy.example/1', '2026-01-01 00:00:00', '2026-01-01 00:00:00')")
            c.commit()

        db.init()
        srcs = db.sources()
        self.assertEqual(len(srcs), 1)
        self.assertEqual(srcs[0]["name"], "Legacy Site")
        self.assertEqual(srcs[0]["extractor_ok"], 0)
        self.assertEqual(srcs[0]["extractor_runs"], 0)
        self.assertEqual(srcs[0]["empty_read_count"], 0)
        self.assertIsNone(srcs[0]["watch_id"])
        self.assertEqual(db.scans()[0]["label"], None)
        items = db.items()
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["match"])
        self.assertIsNone(items[0]["match_key"])
        self.assertIsNone(items[0]["snoozed_until"])

    def test_settings_crud_ignores_unknown_keys(self):
        db.set_settings({"agent": "claude", "unknown_key": "ignored", "parallel": 4})
        s = db.get_settings()
        self.assertEqual(s["agent"], "claude")
        self.assertEqual(s["parallel"], "4")
        self.assertNotIn("unknown_key", s)

    def test_watches_crud_and_scoped_filters_sources(self):
        with self.assertRaises(ValueError):
            db.add_watch("   ")
        wid = db.add_watch("TR CS Internships", "türkiye staj ilanları")
        gsid = db.add_source("Global Board", "https://global.example")
        wsid = db.add_source("Watch Board", "https://watch.example", watch_id=wid)
        gfid = db.add_filter("include", "python")
        wfid = db.add_filter("type", "internship", watch_id=wid)

        self.assertEqual(len(db.watches()), 1)
        self.assertEqual(db.watches()[0]["source_count"], 1)
        self.assertEqual(db.watches()[0]["filter_count"], 1)

        det = db.watch_detail(wid)
        self.assertEqual(det["title"], "TR CS Internships")
        self.assertEqual([s["id"] for s in det["sources"]], [wsid])
        self.assertEqual([f["id"] for f in det["filters"]], [wfid])
        self.assertEqual([s["id"] for s in db.sources(watch_id=None)], [gsid])
        self.assertEqual([f["id"] for f in db.filters_for_watch(None)], [gfid])
        self.assertEqual([f["id"] for f in db.filters_for_watch(wid)], [gfid, wfid])

        self.assertTrue(db.update_watch(wid, title="Updated Watch", active=0))
        self.assertEqual(db.watches(active_only=True), [])
        with self.assertRaises(ValueError):
            db.update_watch(wid, title="")

        # Scoped items_needing_match
        scan_id = db.start_scan("manual", "", "", [])
        db.save_item(scan_id, {"id": gsid, "name": "Global Board", "watch_id": None}, {"id": "g1", "title": "G", "url": "https://global.example/1"})
        db.save_item(scan_id, {"id": wsid, "name": "Watch Board", "watch_id": wid}, {"id": "w1", "title": "W", "url": "https://watch.example/1"})
        self.assertEqual([i["id"] for i in db.items_needing_match(scan_id, "k", watch_id=None)], ["g1"])
        self.assertEqual([i["id"] for i in db.items_needing_match(scan_id, "k", watch_id=wid)], ["w1"])

        self.assertTrue(db.delete_watch(wid))
        self.assertIsNone(db.watch_detail(wid))
        self.assertIsNone(db.source_by_id(wsid))
        self.assertEqual([f["id"] for f in db.filters()], [gfid])

    def test_mark_interrupted_clears_stale_running_rows(self):
        s_run = db.start_scan("manual", "claude", "haiku", [])
        s_done = db.start_scan("manual", "claude", "haiku", [])
        db.finish_scan(s_done, "done")
        d_run = db.start_discovery("req", "Title", "claude", "haiku")
        d_done = db.start_discovery("req2", "Title2", "claude", "haiku")
        db.finish_discovery(d_done, "done", [])

        s_cnt, d_cnt = db.mark_interrupted()
        self.assertEqual((s_cnt, d_cnt), (1, 1))
        self.assertEqual(db.scan_detail(s_run)["status"], "interrupted")
        self.assertIsNotNone(db.scan_detail(s_run)["finished"])
        self.assertEqual(db.scan_detail(s_done)["status"], "done")
        self.assertEqual(db.discovery_detail(d_run)["status"], "interrupted")
        self.assertEqual(db.discovery_detail(d_done)["status"], "done")
        self.assertIsNone(db.running_scan())

    def test_sources_crud_and_open_item_count(self):
        sid = db.add_source("  My Board  ", "https://board.example.com", "Notes", method="agent")
        db.add_source("Plugin Source", "plugin:x.py", "", method="plugin", plugin="plug_x")
        self.assertIsNotNone(db.source_by_plugin("plug_x"))
        self.assertIsNone(db.source_by_plugin("missing"))

        db.update_source(sid, active=0, extractor="def fetch(): return []", extractor_ok=1, extractor_runs=3)
        self.assertEqual(len(db.sources(active_only=True)), 1)
        all_s = {s["id"]: s for s in db.sources()}
        self.assertEqual(all_s[sid]["extractor_runs"], 3)
        self.assertEqual(all_s[sid]["open_items"], 0)

        self.assertTrue(db.delete_source(sid))
        self.assertFalse(db.delete_source(sid))
        self.assertEqual(len(db.sources()), 1)

    def test_filters_crud_and_validation(self):
        fid = db.add_filter("include", "  robotics  ")
        self.assertEqual(db.filters()[0]["value"], "robotics")
        db.update_filter(fid, False)
        self.assertEqual(db.filters(active_only=True), [])
        db.update_filter(fid, True)
        self.assertEqual(len(db.filters(active_only=True)), 1)
        self.assertTrue(db.delete_filter(fid))
        self.assertEqual(db.filters(), [])

        for kind, val in [("bogus", "value"), ("include", "   "), ("deadline_within", "0"), ("deadline_within", "-5"), ("deadline_within", "abc")]:
            with self.assertRaises(ValueError):
                db.add_filter(kind, val)

    def test_scans_items_mark_gone_and_delete(self):
        sid = db.add_source("Alpha", "https://alpha.example")
        src = {"id": sid, "name": "Alpha"}
        scan1 = db.start_scan("manual", "claude", "haiku", [], label="Alpha")
        self.assertIsNotNone(db.running_scan())

        it1 = {"id": "a1", "title": "Role 1", "url": "https://alpha.example/1"}
        it2 = {"id": "a2", "title": "Role 2", "url": "https://alpha.example/2"}
        self.assertTrue(db.save_item(scan1, src, it1))
        self.assertTrue(db.save_item(scan1, src, it2))
        db.set_matches({"a1": True, "a2": False}, "k1")
        db.record_source_result(scan1, src, True, 2, 2, "", 150)
        db.finish_scan(scan1, "done")

        detail = db.scan_detail(scan1)
        self.assertEqual(detail["found"], 1)
        self.assertEqual(detail["new"], 1)
        self.assertIsNone(db.running_scan())

        scan2 = db.start_scan("manual", "claude", "haiku", [])
        self.assertFalse(db.save_item(scan2, src, it1))
        db.mark_gone(src, scan2)
        db.finish_scan(scan2, "done")
        by_id = {i["id"]: i for i in db.items()}
        self.assertEqual(by_id["a1"]["status"], "open")
        self.assertEqual(by_id["a2"]["status"], "gone")

        scan3 = db.start_scan("manual", "", "", [])
        self.assertFalse(db.save_item(scan3, src, it2))
        db.mark_gone({"id": None, "name": "Alpha"}, scan3)
        db.finish_scan(scan3, "done")
        by_id = {i["id"]: i for i in db.items()}
        self.assertEqual(by_id["a2"]["status"], "open")
        self.assertEqual(by_id["a1"]["status"], "gone")

        db.delete_scan(scan1)
        self.assertIsNone(db.scan_detail(scan1))
        self.assertEqual(len(db.items()), 2)

    def test_item_actions_notifications_and_match_cache(self):
        seeded = seed_db(4)
        iid = seeded["items"][0]
        self.assertFalse(db.set_item(iid, bogus="1"))
        self.assertTrue(db.set_item(iid, note="Updated note"))
        self.assertEqual({i["id"]: i for i in db.items()}[iid]["note"], "Updated note")

        un = db.unnotified()
        un_ids = {i["id"] for i in un}
        self.assertNotIn(seeded["items"][2], un_ids)  # hidden
        self.assertNotIn(seeded["items"][3], un_ids)  # match=0
        db.mark_notified(un_ids)
        self.assertEqual(db.unnotified(), [])

        scan_id = seeded["scan_id"]
        self.assertEqual(db.items_needing_match(scan_id, "seedkey"), [])
        self.assertEqual(len(db.items_needing_match(scan_id, "newkey")), 4)

    def test_unnotified_excludes_non_matching_and_snoozed_items(self):
        seeded = seed_db(4)
        future_day = (date.today() + timedelta(days=5)).isoformat()
        db.set_item(seeded["items"][1], snoozed_until=future_day)
        un_ids = {i["id"] for i in db.unnotified()}
        self.assertNotIn(seeded["items"][3], un_ids)  # match=0
        self.assertNotIn(seeded["items"][1], un_ids)  # snoozed into future
        self.assertIn(seeded["items"][0], un_ids)

    def test_discoveries_crud_and_corrupt_json(self):
        did = db.start_discovery("rust jobs", "Rust jobs", "claude", "haiku")
        db.finish_discovery(did, "done", [{"name": "Rust Jobs", "url": "https://rust.example"}], title="Rust Jobs")
        lst = db.discoveries()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["found"], 1)
        det = db.discovery_detail(did)
        self.assertEqual(det["results"][0]["name"], "Rust Jobs")

        db._exec("UPDATE discoveries SET results='{bad' WHERE id=?", (did,))
        self.assertEqual(db.discoveries()[0]["found"], 0)
        self.assertEqual(db.discovery_detail(did)["results"], [])

        db.delete_discovery(did)
        self.assertIsNone(db.discovery_detail(did))

    def test_wal_mode_user_version_and_v1_to_v2_migration(self):
        with contextlib.closing(db.connect()) as c:
            mode = c.execute("PRAGMA journal_mode").fetchone()[0]
            ver = c.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")
        self.assertEqual(ver, db.SCHEMA_VERSION)

        # Simulate a v1 database with an existing working extractor
        db.DB_PATH.unlink()
        with contextlib.closing(sqlite3.connect(str(db.DB_PATH))) as c:
            c.executescript(db.SCHEMA)
            db._add_columns(c, db.MIGRATIONS)
            c.execute(
                "INSERT INTO sources(name, url, method, extractor, extractor_ok, extractor_updated, created) "
                "VALUES('V1', 'https://v1.example', 'agent', 'def fetch(): return []', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            c.execute("PRAGMA user_version = 1")
            c.commit()

        db.init()
        with contextlib.closing(db.connect()) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        src = db.sources()[0]
        self.assertEqual(src["last_good_extractor"], "def fetch(): return []")
        self.assertEqual(src["last_good_updated"], "2026-01-01 00:00:00")

    def test_cross_process_scan_lock_and_safe_mark_interrupted(self):
        # Live lock prevents second owner from acquiring, while heartbeat keeps it alive
        self.assertTrue(db.acquire_scan_lock("tok-1"))
        self.assertFalse(db.acquire_scan_lock("tok-2"))
        s_live = db.start_scan("manual", "claude", "haiku", [])
        s_stale = db.start_scan("manual", "claude", "haiku", [])
        db._exec("UPDATE scans SET started='2020-01-01 00:00:00' WHERE id=?", (s_stale,))
        self.assertTrue(db.update_scan_lock("tok-1", scan_id=s_live))

        d_live = db.start_discovery("live req", "Live", "claude", "haiku")
        d_stale = db.start_discovery("stale req", "Stale", "claude", "haiku")
        self.assertTrue(db.acquire_scan_lock("disc-1", name="discover"))
        self.assertTrue(db.update_scan_lock("disc-1", scan_id=d_live, name="discover"))

        s_cnt, d_cnt = db.mark_interrupted()
        self.assertEqual((s_cnt, d_cnt), (1, 1))
        self.assertEqual(db.scan_detail(s_live)["status"], "running")
        self.assertEqual(db.scan_detail(s_stale)["status"], "interrupted")
        self.assertEqual(db.discovery_detail(d_live)["status"], "running")
        self.assertEqual(db.discovery_detail(d_stale)["status"], "interrupted")

        # Wrong token cannot release; right token releases
        db.release_scan_lock("wrong-tok")
        self.assertIsNotNone(db.active_scan_lock("scan"))
        db.release_scan_lock("tok-1")
        db.release_scan_lock("disc-1", name="discover")
        self.assertIsNone(db.active_scan_lock("scan"))

        # Stale lock (dead PID) can be taken over by a new owner
        self.assertTrue(db.acquire_scan_lock("dead-tok", pid=999999))
        self.assertTrue(db.acquire_scan_lock("tok-3"))
        db.release_scan_lock("tok-3")

    def test_shared_url_readers_and_cascade_deletes(self):
        w1 = db.add_watch("W1", "req1")
        w2 = db.add_watch("W2", "req2")
        s1 = db.add_source("Board W1", "https://jobs.example.com/list/?utm_source=abc&b=2&a=1", watch_id=w1)
        code = "def fetch(): return [{'title': 'X', 'url': 'https://jobs.example.com/1'}]"
        db.save_shared_reader("https://jobs.example.com/list?a=1&b=2", code, ok=True, propagate=True)

        # New source with equivalent canonical URL across another watch gets the shared reader immediately
        s2 = db.add_source("Board W2", "https://jobs.example.com/list/?a=1&b=2&fbclid=xyz", watch_id=w2)
        src2 = db.source_by_id(s2)
        self.assertEqual(src2["extractor_ok"], 1)
        self.assertEqual(src2["extractor"], code)
        self.assertEqual(src2["last_good_extractor"], code)

        # Cascade delete on watch and source cleans up items, history, scans, scan_items, scan_sources, discoveries
        g_src = db.add_source("Global", "https://global.example.com")
        sc1 = db.start_scan("manual", "claude", "", [], watch_id=w1)
        sc_g = db.start_scan("manual", "claude", "", [])
        db.save_item(sc1, db.source_by_id(s1), {"id": "w1-it", "title": "W1 Item", "url": "https://jobs.example.com/1"})
        db.save_item(sc1, db.source_by_id(s1), {"id": "w1-it", "title": "W1 Item", "url": "https://jobs.example.com/1", "deadline": "2027-05-01"})
        db.save_item(sc_g, db.source_by_id(g_src), {"id": "g-it", "title": "G Item", "url": "https://global.example.com/1"})
        db.record_source_result(sc1, db.source_by_id(s1), True, 1, 1, "", 10)
        db.start_discovery("w1 req", "W1", "claude", "", watch_id=w1)

        self.assertTrue(db.delete_watch(w1))
        self.assertIsNone(db.scan_detail(sc1))
        self.assertEqual(db.item_history("w1-it"), [])
        self.assertIsNone(db.item_by_id("w1-it"))
        self.assertIsNotNone(db.item_by_id("g-it"))

        # Cascade delete on a standalone source cleans up its items and history
        sc_g2 = db.start_scan("manual", "claude", "", [])
        db.save_item(sc_g2, db.source_by_id(g_src), {"id": "g-it", "title": "G Item v2", "url": "https://global.example.com/1"})
        self.assertTrue(db.item_history("g-it"))
        self.assertTrue(db.delete_source(g_src))
        self.assertIsNone(db.item_by_id("g-it"))
        self.assertEqual(db.item_history("g-it"), [])

    def test_item_change_history_no_resurrection_and_paging(self):
        sid = db.add_source("Board", "https://board.example.com/jobs")
        src = db.source_by_id(sid)
        sc1 = db.start_scan("manual", "claude", "", [])
        is_new = db.save_item(sc1, src, {
            "id": "it-1",
            "title": "Intern",
            "url": "https://board.example.com/jobs/101",
            "deadline": "2027-03-01",
        })
        self.assertTrue(is_new)

        # Deadline moved updates existing item (matched by source_id+url even if raw id changed)
        sc2 = db.start_scan("manual", "claude", "", [])
        is_new2 = db.save_item(sc2, src, {
            "id": "it-1-different-hash",
            "title": "Intern",
            "url": "https://board.example.com/jobs/101",
            "deadline": "2027-03-15",
        })
        self.assertFalse(is_new2)
        it = db.item_by_id("it-1")
        self.assertIn("2027-03-01", it["change_summary"])
        self.assertIn("2027-03-15", it["change_summary"])
        self.assertEqual(it["last_change_kind"], "deadline_moved")
        hist = db.item_history("it-1")
        self.assertEqual(hist[0]["kind"], "deadline_moved")

        # Mark gone records closed; re-appearing later records reopened WITHOUT returning is_new=True
        sc3 = db.start_scan("manual", "claude", "", [])
        self.assertEqual(db.mark_gone(src, sc3), 1)
        self.assertEqual(db.item_by_id("it-1")["status"], "gone")
        sc4 = db.start_scan("manual", "claude", "", [])
        is_new4 = db.save_item(sc4, src, {
            "id": "it-1",
            "title": "Intern",
            "url": "https://board.example.com/jobs/101",
            "deadline": "2027-03-15",
        })
        self.assertFalse(is_new4)
        self.assertEqual(db.item_by_id("it-1")["status"], "open")
        self.assertTrue(any(h["kind"] == "reopened" for h in db.item_history("it-1")))

        # Pagination
        for k in range(5):
            db.save_item(sc4, src, {"id": f"p-{k}", "title": f"Item {k}", "url": f"https://board.example.com/jobs/p/{k}"})
        self.assertEqual(db.items_count(), 6)
        self.assertEqual(len(db.items(limit=2, offset=0)), 2)
        self.assertEqual(len(db.items(limit=2, offset=4)), 2)
        self.assertEqual(len(db.items(limit=2, offset=6)), 0)

    def test_agent_usage_and_quota_block(self):
        future = (datetime.now() + timedelta(hours=2)).strftime(db.TS)
        past = (datetime.now() - timedelta(hours=2)).strftime(db.TS)
        db.log_agent_call("gemini", "gemini-2.5-flash", ok=False, error="429 quota", resets_at=future)
        self.assertEqual(db.agent_blocked_until("gemini", "gemini-2.5-flash"), future)
        usage = db.agent_usage("gemini")
        self.assertEqual(usage[0]["calls"], 1)
        self.assertEqual(usage[0]["ok"], 0)
        self.assertEqual(usage[0]["blocked_until"], future)

        db.log_agent_call("gemini", "gemini-2.5-flash", ok=True, resets_at=past)
        self.assertIsNone(db.agent_blocked_until("gemini", "gemini-2.5-flash"))
        self.assertEqual(db.agent_usage("gemini")[0]["calls"], 2)

    def test_shared_readers_scoped_by_notes_filters_and_mark_filters_changed(self):
        w1 = db.add_watch("Watch One", "internships")
        w2 = db.add_watch("Watch Two", "research grants")
        w3 = db.add_watch("Watch Three", "internships copy")
        url = "https://shared.example.com/opportunities"

        # Case A: differing notes on the same URL must NOT share or overwrite extractors
        s1 = db.add_source("Board A", url, notes="Internships", watch_id=w1)
        s2 = db.add_source("Board B", f"{url}/?utm_source=test", notes="Research grants", watch_id=w2)
        code_grants = "def fetch(): return [{'title': 'Grant', 'url': 'https://shared.example.com/g1'}]"
        code_intern = "def fetch(): return [{'title': 'Intern', 'url': 'https://shared.example.com/i1'}]"
        self.assertNotEqual(
            db.reader_context_hash("Internships", []),
            db.reader_context_hash("Research grants", []),
        )
        self.assertEqual(
            db.reader_context_hash("Internships", []),
            db.reader_context_hash("  internships  ", []),
        )

        db.save_shared_reader(url, code_grants, ok=True, propagate=True, notes="Research grants", fs=[])
        self.assertEqual(db.source_by_id(s2)["extractor"], code_grants)
        self.assertIsNone(db.source_by_id(s1)["extractor"])

        db.save_shared_reader(url, code_intern, ok=True, propagate=True, notes="Internships", fs=[])
        self.assertEqual(db.source_by_id(s1)["extractor"], code_intern)
        self.assertEqual(db.source_by_id(s2)["extractor"], code_grants)
        self.assertEqual(db.get_shared_reader(url, notes="Internships", fs=[])["extractor"], code_intern)
        self.assertEqual(db.get_shared_reader(url, notes="Research grants", fs=[])["extractor"], code_grants)
        self.assertIsNone(db.get_shared_reader(url, notes="Other", fs=[]))

        s_match_notes = db.add_source("Board C", url, notes="  internships ", watch_id=w3)
        s_diff_notes = db.add_source("Board D", url, notes="Fellowships", watch_id=w3)
        self.assertEqual(db.source_by_id(s_match_notes)["extractor"], code_intern)
        self.assertIsNone(db.source_by_id(s_diff_notes)["extractor"])

        # Case B: differing watch filters must NOT share or overwrite; identical (url, notes, filters) DOES share
        scoped_url = "https://scoped.example.com/jobs"
        wf1 = db.add_watch("Py Watch", "python")
        wf2 = db.add_watch("Rs Watch", "rust")
        wf3 = db.add_watch("Py Mirror", "python")
        db.add_filter("include", "python", watch_id=wf1)
        db.add_filter("include", "rust", watch_id=wf2)
        db.add_filter("include", "  PYTHON ", watch_id=wf3)

        fs1 = db.filters_for_watch(wf1, active_only=True)
        fs2 = db.filters_for_watch(wf2, active_only=True)
        fs3 = db.filters_for_watch(wf3, active_only=True)
        self.assertEqual(db.filters_hash(fs1), db.filters_hash(fs3))
        self.assertNotEqual(db.filters_hash(fs1), db.filters_hash(fs2))
        self.assertNotEqual(db.reader_context_hash("Same", fs1), db.reader_context_hash("Same", fs2))

        sp1 = db.add_source("Py1", scoped_url, notes="Same", watch_id=wf1)
        sr1 = db.add_source("Rs1", scoped_url, notes="Same", watch_id=wf2)
        sp3_pre = db.add_source("Py3 Pre", scoped_url, notes="Same", watch_id=wf3)

        code_rs = "def fetch(): return [{'title': 'Rust', 'url': 'https://scoped.example.com/r1'}]"
        code_py = "def fetch(): return [{'title': 'Python', 'url': 'https://scoped.example.com/p1'}]"
        db.save_shared_reader(scoped_url, code_rs, ok=True, propagate=True, notes="Same", fs=fs2)
        self.assertEqual(db.source_by_id(sr1)["extractor"], code_rs)
        self.assertIsNone(db.source_by_id(sp1)["extractor"])
        self.assertIsNone(db.source_by_id(sp3_pre)["extractor"])

        db.save_shared_reader(scoped_url, code_py, ok=True, propagate=True, notes="Same", fs=fs1)
        self.assertEqual(db.source_by_id(sp1)["extractor"], code_py)
        self.assertEqual(db.source_by_id(sp3_pre)["extractor"], code_py)
        self.assertEqual(db.source_by_id(sr1)["extractor"], code_rs)

        sp3_new = db.add_source("Py3 New", scoped_url, notes="Same", watch_id=wf3)
        sr_new = db.add_source("Rs New", scoped_url, notes="Same", watch_id=wf2)
        sg_none = db.add_source("Global", scoped_url, notes="Same", watch_id=None)
        self.assertEqual(db.source_by_id(sp3_new)["extractor"], code_py)
        self.assertEqual(db.source_by_id(sr_new)["extractor"], code_rs)
        self.assertIsNone(db.source_by_id(sg_none)["extractor"])

        # mark_filters_changed lifecycle: add, toggle, update, delete, revert, and save_shared_reader clear
        wm = db.add_watch("Marked Watch", "req")
        wo = db.add_watch("Unchanged Watch", "req")
        f_base = db.add_filter("type", "internship", watch_id=wm)
        sm = db.add_source("MarkedSrc", "https://marked.example.com/list", notes="N", watch_id=wm)
        so = db.add_source("OtherSrc", "https://other.example.com/list", notes="N", watch_id=wo)
        db.save_shared_reader("https://marked.example.com/list", "def fetch(): return []", ok=True,
                              propagate=True, notes="N", fs=db.filters_for_watch(wm))
        db.save_shared_reader("https://other.example.com/list", "def fetch(): return []", ok=True,
                              propagate=True, notes="N", fs=db.filters_for_watch(wo))
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 0)
        self.assertEqual(db.source_by_id(so)["filters_changed"], 0)
        self.assertEqual(db.mark_filters_changed(wm), 0)

        f_extra = db.add_filter("exclude", "senior", watch_id=wm)
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 1)
        self.assertEqual(db.source_by_id(so)["filters_changed"], 0)
        self.assertEqual(db.mark_filters_changed(wm), 1)

        # Toggling the added filter off reverts active filters back to filter_hash
        db.update_filter(f_extra, active=False)
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 0)

        # Toggling back on and updating flags it again; deleting reverts it
        db.update_filter(f_extra, active=True)
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 1)
        db.update_filter(f_extra, value="principal")
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 1)
        db.delete_filter(f_extra)
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 0)

        # Updating base filter sets filters_changed=1; reverting value clears it back to 0
        db.update_filter(f_base, value="fellowship")
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 1)
        db.update_filter(f_base, value="internship")
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 0)

        # Changing filter again and re-saving shared reader clears filters_changed=0 and updates filter_hash
        db.update_filter(f_base, value="fellowship")
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 1)
        new_fs = db.filters_for_watch(wm, active_only=True)
        db.save_shared_reader("https://marked.example.com/list", "def fetch(): return [{'title': 'F', 'url': 'https://marked.example.com/1'}]",
                              ok=True, propagate=True, notes="N", fs=new_fs)
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 0)
        self.assertEqual(db.source_by_id(sm)["filter_hash"], db.filters_hash(new_fs))

        # Global filter affects all learned sources and clearing it resets them
        f_glob = db.add_filter("location", "Remote")
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 1)
        self.assertEqual(db.source_by_id(so)["filters_changed"], 1)
        db.delete_filter(f_glob)
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 0)
        self.assertEqual(db.source_by_id(so)["filters_changed"], 0)

        # Deleting a baseline filter sets filters_changed=1
        db.delete_filter(f_base)
        self.assertEqual(db.source_by_id(sm)["filters_changed"], 1)

    def test_items_filtering_sorting_turkish_search_facets_and_counts(self):
        today_d = date.today()
        today = today_d.isoformat()
        yesterday = (today_d - timedelta(days=1)).isoformat()
        in_3d = (today_d + timedelta(days=3)).isoformat()
        in_4d = (today_d + timedelta(days=4)).isoformat()
        in_5d = (today_d + timedelta(days=5)).isoformat()
        in_10d = (today_d + timedelta(days=10)).isoformat()
        in_12d = (today_d + timedelta(days=12)).isoformat()
        in_20d = (today_d + timedelta(days=20)).isoformat()
        old_ts = (datetime.now() - timedelta(days=5)).strftime(db.TS)
        recent_ts_a = (datetime.now() - timedelta(minutes=30)).strftime(db.TS)
        recent_ts_b = (datetime.now() - timedelta(minutes=10)).strftime(db.TS)
        recent_ts_c = (datetime.now() - timedelta(minutes=5)).strftime(db.TS)

        w1 = db.add_watch("W1", "internships")
        w2 = db.add_watch("W2", "hackathons")
        w_empty = db.add_watch("W Empty", "none")

        f_glob_on = db.add_filter("include", "python")
        f_glob_off = db.add_filter("exclude", "unpaid")
        db.update_filter(f_glob_off, active=False)
        db.add_filter("type", "internship", watch_id=w1)
        self.assertTrue(f_glob_on > 0)

        s_w1_a = db.add_source("Alpha W1", "https://alpha.example.com", watch_id=w1)
        s_w1_b = db.add_source("Beta W1", "https://beta.example.com", watch_id=w1)
        s_w2 = db.add_source("Gamma W2", "https://gamma.example.com", watch_id=w2)
        s_paused = db.add_source("Paused Src", "https://paused.example.com", watch_id=w2)
        db.update_source(s_paused, active=0)
        s_glob = db.add_source("Global Src", "https://glob.example.com", watch_id=None)

        src_w1_a = db.source_by_id(s_w1_a)
        src_w1_b = db.source_by_id(s_w1_b)
        src_w2 = db.source_by_id(s_w2)
        src_glob = db.source_by_id(s_glob)

        sc1 = db.start_scan("manual", "claude", "", [])
        # 1) Open, closing this week, Turkish uppercase İ in title & location
        db.save_item(sc1, src_w1_a, {
            "id": "it-tr-upper", "title": "İSTANBUL Staj Programı", "org": "Tekno",
            "category": "internship", "url": "https://alpha.example.com/1",
            "deadline": in_3d, "location": "İngilizce / İstanbul", "summary": "Yaz dönemi",
        }, match=1, match_key="k")
        # 2) Saved item on Beta W1 (counts in feed_open AND agenda_saved, NOT closing_this_week)
        db.save_item(sc1, src_w1_b, {
            "id": "it-saved", "title": "Ankara Grant", "org": "Lab",
            "category": "grant", "url": "https://beta.example.com/1",
            "deadline": in_12d, "location": "Ankara", "summary": "Research",
        }, match=1, match_key="k")
        db.set_item("it-saved", action="saved")
        # 3) Item that will be updated in sc2 (with empty category -> 'Other', lowercase istanbul/ingilizce)
        db.save_item(sc1, src_w1_a, {
            "id": "it-upd", "title": "ingilizce Bootcamp", "org": "Dev",
            "category": "", "url": "https://alpha.example.com/2",
            "deadline": in_20d, "location": "istanbul", "summary": "100% online",
        }, match=1, match_key="k")
        # 4) Snoozed item on w1 (snoozed into future, closing this week -> excluded from feed_open & closing_this_week)
        db.save_item(sc1, src_w1_a, {
            "id": "it-snoozed", "title": "Snoozed Opp", "org": "Org",
            "category": "internship", "url": "https://alpha.example.com/3",
            "deadline": in_3d, "location": "Izmir",
        }, match=1, match_key="k")
        db.set_item("it-snoozed", snoozed_until=in_4d)
        # 5) Hidden item on w1
        db.save_item(sc1, src_w1_a, {
            "id": "it-hidden", "title": "Hidden Opp", "org": "Org",
            "category": "internship", "url": "https://alpha.example.com/4",
            "deadline": in_10d,
        }, match=1, match_key="k")
        db.set_item("it-hidden", action="hidden")
        # 6) Expired via deadline
        db.save_item(sc1, src_w1_a, {
            "id": "it-exp-dl", "title": "Expired DL", "category": "internship",
            "url": "https://alpha.example.com/5", "deadline": yesterday,
        }, match=1, match_key="k")
        # 7) Expired via start_date fallback when deadline is None
        db.save_item(sc1, src_w1_a, {
            "id": "it-exp-sd", "title": "Expired SD", "category": "internship",
            "url": "https://alpha.example.com/6", "deadline": None, "start_date": yesterday,
        }, match=1, match_key="k")
        # 8) Excluded (match=0)
        db.save_item(sc1, src_w1_a, {
            "id": "it-exc", "title": "Excluded Role", "category": "internship",
            "url": "https://alpha.example.com/7", "deadline": in_5d,
        }, match=0, match_key="det_excluded")
        # 9) Item on w1 that will become gone in sc2
        db.save_item(sc1, src_w1_b, {
            "id": "it-gone", "title": "Closing Soon Gone", "category": "internship",
            "url": "https://beta.example.com/2", "deadline": in_5d,
        }, match=1, match_key="k")
        # 10) Item on w2 with snoozed_until == today (snooze expired -> treated as active/unsnoozed)
        db.save_item(sc1, src_w2, {
            "id": "it-w2-open", "title": "Unsnoozed Hack", "category": "hackathon",
            "url": "https://gamma.example.com/1", "deadline": in_5d,
        }, match=1, match_key="k")
        db.set_item("it-w2-open", snoozed_until=today)
        # 11) Applied item on w2
        db.save_item(sc1, src_w2, {
            "id": "it-applied", "title": "Applied Hack", "category": "hackathon",
            "url": "https://gamma.example.com/2", "deadline": in_10d,
        }, match=1, match_key="k")
        db.set_item("it-applied", action="applied")
        # 12) Global item with no deadline (NULL category -> 'Other')
        db.save_item(sc1, src_glob, {
            "id": "it-glob", "title": "Global Open_Item", "category": None,
            "url": "https://glob.example.com/1", "deadline": None,
        }, match=1, match_key="k")
        db.finish_scan(sc1, "done")

        # Scan 2: update it-upd (making it recently updated) and mark it-gone as gone
        sc2 = db.start_scan("manual", "claude", "", [])
        db.save_item(sc2, src_w1_a, {
            "id": "it-upd", "title": "ingilizce Bootcamp v2", "org": "Dev",
            "category": "", "url": "https://alpha.example.com/2",
            "deadline": in_20d, "location": "istanbul", "summary": "100% online",
        })
        # Refresh it-saved so only it-gone is missing on src_w1_b
        db.save_item(sc2, src_w1_b, {
            "id": "it-saved", "title": "Ankara Grant", "org": "Lab",
            "category": "grant", "url": "https://beta.example.com/1",
            "deadline": in_12d, "location": "Ankara", "summary": "Research",
        })
        self.assertEqual(db.mark_gone(src_w1_b, sc2), 1)
        db.finish_scan(sc2, "done")

        # Deterministic timestamps for sorting and new/old checks
        db._exec("UPDATE items SET first_seen=? WHERE id=?", (old_ts, "it-upd"))
        db._exec("UPDATE items SET first_seen=? WHERE id=?", (recent_ts_a, "it-saved"))
        db._exec("UPDATE items SET first_seen=? WHERE id=?", (recent_ts_b, "it-tr-upper"))
        db._exec("UPDATE items SET first_seen=? WHERE id=?", (recent_ts_c, "it-glob"))
        db._exec("UPDATE items SET updated_at=? WHERE id=?", (recent_ts_c, "it-upd"))

        # Filtering checks with items() and items_count()
        def ids(**kw):
            rows = db.items(**kw)
            count_kw = {k: v for k, v in kw.items() if k != "sort"}
            self.assertEqual(db.items_count(**count_kw), len(rows))
            return [r["id"] for r in rows]

        self.assertEqual(set(ids(watch_id=w2)), {"it-w2-open", "it-applied"})
        self.assertEqual(ids(watch_id=None), ["it-glob"])
        self.assertEqual(ids(status="gone"), ["it-gone"])
        self.assertEqual(ids(match="0"), ["it-exc"])
        self.assertNotIn("it-exc", ids(match="1"))
        self.assertEqual(set(ids(status="all", match="1", exclude_hidden=False)),
                         {"it-tr-upper", "it-saved", "it-upd", "it-snoozed", "it-hidden",
                          "it-exp-dl", "it-exp-sd", "it-gone", "it-w2-open", "it-applied", "it-glob"})

        # Default feed-like filter combination
        feed_ids = set(ids(
            status="open", match="1", exclude_hidden=True, exclude_applied=True,
            exclude_expired=True, snoozed="exclude",
        ))
        self.assertEqual(feed_ids, {"it-tr-upper", "it-saved", "it-upd", "it-w2-open", "it-glob"})
        self.assertIn("it-applied", ids(status="open", match="1", exclude_hidden=True, exclude_applied=False, exclude_expired=True, snoozed="exclude"))
        self.assertEqual(
            {"it-exp-dl", "it-exp-sd"} & set(ids(status="open", match="1", exclude_hidden=True, exclude_applied=True, exclude_expired=False, snoozed="exclude")),
            {"it-exp-dl", "it-exp-sd"},
        )
        self.assertIn("it-snoozed", ids(status="open", match="1", snoozed="all"))
        self.assertEqual(ids(status="open", snoozed="only"), ["it-snoozed"])
        self.assertEqual(ids(only_updated=True), ["it-upd"])  # excludes 'it-gone' whose last_change_kind=='closed'

        # Category ('Other' covers '' and NULL) and source filters
        self.assertEqual(set(ids(categories=["Other"])), {"it-upd", "it-glob"})
        self.assertEqual(set(ids(categories=["grant"])), {"it-saved"})
        self.assertEqual(set(ids(source_name="Gamma W2")), {"it-w2-open", "it-applied"})
        self.assertEqual(set(ids(source_id=s_w1_b)), {"it-saved", "it-gone"})

        # Turkish 'İ' / 'i' case-insensitive search + wildcard escaping
        self.assertEqual(set(ids(search="istanbul")), {"it-tr-upper", "it-upd"})
        self.assertEqual(set(ids(search="İSTANBUL")), {"it-tr-upper", "it-upd"})
        self.assertEqual(set(ids(search="ingilizce")), {"it-tr-upper", "it-upd"})
        self.assertEqual(set(ids(search="İngilizce")), {"it-tr-upper", "it-upd"})
        self.assertEqual(ids(search="İSTANBUL staj"), ["it-tr-upper"])
        self.assertEqual(ids(search="100%"), ["it-upd"])
        self.assertEqual(ids(search="Open_Item"), ["it-glob"])

        # Sorting: first_seen, deadline (NULLs last), updated
        w1_feed_kw = dict(watch_id=w1, status="open", match="1", exclude_hidden=True,
                          exclude_applied=True, exclude_expired=True, snoozed="exclude")
        self.assertEqual(ids(sort="first_seen", **w1_feed_kw), ["it-tr-upper", "it-saved", "it-upd"])
        self.assertEqual(ids(sort="updated", **w1_feed_kw), ["it-upd", "it-tr-upper", "it-saved"])
        all_feed_kw = dict(status="open", match="1", exclude_hidden=True,
                           exclude_applied=True, exclude_expired=True, snoozed="exclude")
        self.assertEqual(ids(sort="deadline", **all_feed_kw),
                         ["it-tr-upper", "it-w2-open", "it-saved", "it-upd", "it-glob"])

        # Facets (global and per-watch)
        facets = db.items_facets()
        self.assertEqual(facets["categories"], {"Other": 2, "grant": 1, "hackathon": 1, "internship": 1})
        self.assertEqual(facets["sources"], ["Alpha W1", "Beta W1", "Gamma W2", "Global Src"])
        self.assertEqual(facets["updated_count"], 1)
        self.assertEqual(facets["snoozed_count"], 1)

        facets_w1 = db.items_facets(watch_id=w1)
        self.assertEqual(facets_w1["categories"], {"Other": 1, "grant": 1, "internship": 1})
        self.assertEqual(facets_w1["sources"], ["Alpha W1", "Beta W1"])
        self.assertEqual(facets_w1["updated_count"], 1)
        self.assertEqual(facets_w1["snoozed_count"], 1)

        # Counts()
        c = db.counts()
        self.assertEqual(c["feed_open"], 5)  # it-tr-upper, it-saved, it-upd, it-w2-open, it-glob
        self.assertEqual(c["feed_new"], 4)  # it-upd was aged 5 days ago
        self.assertEqual(c["feed_updated"], 1)  # it-upd
        self.assertEqual(c["feed_snoozed"], 1)  # it-snoozed
        self.assertEqual(c["closing_this_week"], 2)  # it-tr-upper (3d) + it-w2-open (5d)
        self.assertEqual(c["agenda_saved"], 1)
        self.assertEqual(c["agenda_applied"], 1)
        self.assertEqual(c["sources_active"], 4)  # 5 total - 1 paused
        self.assertEqual(c["filters_global_active"], 1)
        self.assertEqual(c["items_total"], 12)
        self.assertEqual(c["watches"][str(w1)], {"open": 3, "new": 2, "snoozed": 1, "closing_this_week": 1})
        self.assertEqual(c["watches"][str(w2)], {"open": 1, "new": 1, "snoozed": 0, "closing_this_week": 1})
        self.assertEqual(c["watches"][str(w_empty)], {"open": 0, "new": 0, "snoozed": 0, "closing_this_week": 0})

        # Updates older than 72h drop out of only_updated, facets.updated_count, and counts.feed_updated
        db._exec("UPDATE items SET updated_at=? WHERE id=?", (old_ts, "it-upd"))
        self.assertEqual(ids(only_updated=True), [])
        self.assertEqual(db.items_facets()["updated_count"], 0)
        self.assertEqual(db.counts()["feed_updated"], 0)

    def test_items_pagination_over_500(self):
        sid = db.add_source("Bulk Board", "https://bulk.example.com")
        src = db.source_by_id(sid)
        sc = db.start_scan("manual", "claude", "", [])
        total = 520
        ts_now = db.now()
        with db._conn() as c:
            rows = [
                (
                    f"bulk-{idx:04d}", sid, src["name"], f"Bulk Item {idx}",
                    f"https://bulk.example.com/items/{idx}", sc, sc, ts_now, ts_now, 1, "bulkkey",
                )
                for idx in range(total)
            ]
            c.executemany(
                "INSERT INTO items(id, source_id, source_name, title, url, first_scan, last_scan, "
                "first_seen, last_seen, match, match_key) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            c.executemany(
                "INSERT INTO scan_items(scan_id, item_id, is_new, is_updated) VALUES(?, ?, 1, 0)",
                [(sc, f"bulk-{idx:04d}") for idx in range(total)],
            )
            c.commit()
        db.finish_scan(sc, "done")

        self.assertEqual(db.items_count(), total)
        first_page = db.items(limit=50, offset=0)
        tail_page = db.items(limit=50, offset=500)
        past_page = db.items(limit=50, offset=550)
        self.assertEqual(len(first_page), 50)
        self.assertEqual(len(tail_page), 20)
        self.assertEqual(past_page, [])

        seen_ids = []
        for off in range(0, total, 50):
            chunk = db.items(limit=50, offset=off)
            seen_ids.extend(r["id"] for r in chunk)
        self.assertEqual(len(seen_ids), total)
        self.assertEqual(set(seen_ids), {f"bulk-{idx:04d}" for idx in range(total)})

    def test_sources_include_code_flag_and_source_code(self):
        code = "def fetch(): return [{'title': 'A', 'url': 'https://a.example/1'}]"
        good_code = "def fetch(): return [{'title': 'B', 'url': 'https://b.example/1'}]"
        s_full = db.add_source("Full Src", "https://full.example.com")
        s_backup = db.add_source("Backup Src", "https://backup.example.com")
        s_empty = db.add_source("Empty Src", "https://empty.example.com")

        db.update_source(s_full, extractor=code, last_good_extractor=good_code, extractor_ok=1, extractor_runs=4)
        db.update_source(s_backup, extractor=None, last_good_extractor=good_code, extractor_ok=0, extractor_error="timeout")

        slim = {s["id"]: s for s in db.sources(include_code=False)}
        self.assertNotIn("extractor", slim[s_full])
        self.assertNotIn("last_good_extractor", slim[s_full])
        self.assertTrue(slim[s_full]["has_extractor"])
        self.assertTrue(slim[s_full]["has_last_good_extractor"])

        self.assertNotIn("extractor", slim[s_backup])
        self.assertNotIn("last_good_extractor", slim[s_backup])
        self.assertFalse(slim[s_backup]["has_extractor"])
        self.assertTrue(slim[s_backup]["has_last_good_extractor"])

        self.assertFalse(slim[s_empty]["has_extractor"])
        self.assertFalse(slim[s_empty]["has_last_good_extractor"])

        full = {s["id"]: s for s in db.sources(include_code=True)}
        self.assertEqual(full[s_full]["extractor"], code)
        self.assertEqual(full[s_full]["last_good_extractor"], good_code)
        self.assertTrue(full[s_full]["has_extractor"])
        self.assertTrue(full[s_full]["has_last_good_extractor"])

        sc_full = db.source_code(s_full)
        self.assertEqual(sc_full["id"], s_full)
        self.assertEqual(sc_full["extractor"], code)
        self.assertEqual(sc_full["last_good_extractor"], good_code)
        self.assertEqual(sc_full["extractor_ok"], 1)
        self.assertEqual(sc_full["extractor_runs"], 4)

        sc_backup = db.source_code(s_backup)
        self.assertIsNone(sc_backup["extractor"])
        self.assertEqual(sc_backup["last_good_extractor"], good_code)
        self.assertEqual(sc_backup["extractor_error"], "timeout")
        self.assertIsNone(db.source_code(999999))

    def test_volatile_fields_no_history_and_excluded_items_mark_gone(self):
        sid = db.add_source("Board", "https://board.example.com")
        src = db.source_by_id(sid)

        sc1 = db.start_scan("manual", "claude", "", [])
        base_item = {
            "id": "v-1",
            "title": "ML Intern",
            "org": "Acme",
            "category": "internship",
            "url": "https://board.example.com/1",
            "deadline": "2027-06-01",
            "location": "Istanbul",
            "summary": "Initial summary",
            "extra": '{"stipend": 1000}',
        }
        self.assertTrue(db.save_item(sc1, src, base_item, match=1, match_key="k1"))
        db.save_item(sc1, src, {
            "id": "exc-1",
            "title": "Senior Architect",
            "url": "https://board.example.com/exc",
            "summary": "Excluded",
        }, match=0, match_key="det_excluded")
        db.save_item(sc1, src, {
            "id": "vanish-1",
            "title": "Temporary Role",
            "url": "https://board.example.com/vanish",
        }, match=1, match_key="k1")
        db.finish_scan(sc1, "done")

        # Changing only volatile fields (summary, extra) must NOT add history or mark is_updated=1
        sc2 = db.start_scan("manual", "claude", "", [])
        volatile_item = dict(base_item, summary="Updated summary text", extra='{"stipend": 1500}')
        self.assertFalse(db.save_item(sc2, src, volatile_item))
        # Excluded item still present on source updates last_scan so mark_gone ignores it
        self.assertFalse(db.save_item(sc2, src, {
            "id": "exc-1",
            "title": "Senior Architect",
            "url": "https://board.example.com/exc",
            "summary": "Still excluded",
        }, match=0, match_key="det_excluded"))
        gone_cnt = db.mark_gone(src, sc2)
        db.finish_scan(sc2, "done")

        self.assertEqual(gone_cnt, 1)  # only vanish-1, NOT exc-1
        it_v = db.item_by_id("v-1")
        self.assertEqual(it_v["summary"], "Updated summary text")
        self.assertIsNone(it_v["match"])
        self.assertIsNone(it_v["match_key"])
        self.assertIn("v-1", {r["id"] for r in db.items_needing_match(sc2, "k1")})
        self.assertEqual(it_v["extra"], '{"stipend": 1500}')
        self.assertIsNone(it_v["updated_at"])
        self.assertIsNone(it_v["last_change_kind"])
        self.assertEqual(db.item_history("v-1"), [])
        sc2_items = {r["id"]: r for r in db.scan_detail(sc2)["items"]}
        self.assertEqual(sc2_items["v-1"]["is_updated"], 0)
        self.assertEqual(db.scan_changes(sc2), [])

        exc = db.item_by_id("exc-1")
        self.assertEqual(exc["status"], "open")
        self.assertEqual(exc["last_scan"], sc2)
        self.assertEqual(exc["match"], 0)
        self.assertEqual(exc["match_key"], "det_excluded")
        self.assertEqual(db.item_history("exc-1"), [])
        self.assertEqual(db.item_by_id("vanish-1")["status"], "gone")
        self.assertEqual(db.item_history("vanish-1")[0]["kind"], "closed")

        # Tracked field changes (title, location, org, category, deadline) DO insert history & is_updated=1
        tracked_mutations = [
            ("title", "Applied ML Intern", "updated"),
            ("location", "Remote", "updated"),
            ("org", "Acme Labs", "updated"),
            ("category", "fellowship", "updated"),
            ("deadline", "2027-06-15", "deadline_moved"),
        ]
        cur_item = dict(volatile_item)
        for field, new_val, expected_kind in tracked_mutations:
            sc_mut = db.start_scan("manual", "claude", "", [])
            cur_item[field] = new_val
            self.assertFalse(db.save_item(sc_mut, src, cur_item))
            db.finish_scan(sc_mut, "done")
            latest_hist = db.item_history("v-1")[0]
            self.assertEqual(latest_hist["kind"], expected_kind)
            self.assertEqual(latest_hist["field"], field)
            self.assertEqual(latest_hist["new_value"], new_val)
            mut_items = {r["id"]: r for r in db.scan_detail(sc_mut)["items"]}
            self.assertEqual(mut_items["v-1"]["is_updated"], 1)

        # Once exc-1 disappears on a later scan while v-1 stays present, mark_gone marks only exc-1 closed
        sc_gone = db.start_scan("manual", "claude", "", [])
        db.save_item(sc_gone, src, cur_item)
        self.assertEqual(db.mark_gone(src, sc_gone), 1)
        db.finish_scan(sc_gone, "done")
        self.assertEqual(db.item_by_id("v-1")["status"], "open")
        self.assertEqual(db.item_by_id("exc-1")["status"], "gone")
        self.assertEqual(db.item_history("exc-1")[0]["kind"], "closed")

    def test_agent_usage_preserves_resets_at_across_non_quota_failures(self):
        future = (datetime.now() + timedelta(hours=3)).strftime(db.TS)
        db.log_agent_call("gemini", "flash", ok=False, error="429", resets_at=future)
        self.assertEqual(db.agent_blocked_until("gemini", "flash"), future)

        # Subsequent failure without resets_at must preserve the existing future resets_at
        db.log_agent_call("gemini", "flash", ok=False, error="other", resets_at=None)
        self.assertEqual(db.agent_blocked_until("gemini", "flash"), future)
        row = db._one("SELECT * FROM agent_usage WHERE agent=? AND model=?", ("gemini", "flash"))
        self.assertEqual(row["calls"], 2)
        self.assertEqual(row["ok"], 0)
        self.assertEqual(row["last_error"], "other")
        self.assertEqual(row["resets_at"], future)

        # Successful call clears resets_at to NULL
        db.log_agent_call("gemini", "flash", ok=True)
        self.assertIsNone(db.agent_blocked_until("gemini", "flash"))
        row_ok = db._one("SELECT * FROM agent_usage WHERE agent=? AND model=?", ("gemini", "flash"))
        self.assertEqual(row_ok["calls"], 3)
        self.assertEqual(row_ok["ok"], 1)
        self.assertIsNone(row_ok["resets_at"])

    def test_notification_log_recent_errors_and_dismiss(self):
        self.assertEqual(db.recent_notify_errors(), [])
        db.log_notification(1, "email", False, "SMTP auth failed")
        tg_id = db.log_notification(1, "telegram", False, "Chat not found")
        db.log_notification(2, "email", True, "Sent ok")  # latest for email succeeded
        db.log_notification(2, "webhook", False, "502 Bad Gateway")
        wh_id = db.log_notification(3, "webhook", False, "503 Unavailable")

        errs = db.recent_notify_errors()
        self.assertEqual([(e["id"], e["channel"], e["message"]) for e in errs], [
            (wh_id, "webhook", "503 Unavailable"),
            (tg_id, "telegram", "Chat not found"),
        ])

        db.dismiss_notify_errors()
        self.assertEqual(db.recent_notify_errors(), [])
        self.assertEqual(db.get_settings()["notify_errors_dismissed_id"], str(wh_id))

        new_err_id = db.log_notification(4, "email", False, "SMTP timeout")
        after = db.recent_notify_errors()
        self.assertEqual([(e["id"], e["channel"], e["message"]) for e in after], [
            (new_err_id, "email", "SMTP timeout"),
        ])

    def test_v5_migration_and_deadline_text_refresh(self):
        db.DB_PATH.unlink()
        with contextlib.closing(sqlite3.connect(str(db.DB_PATH))) as c:
            c.executescript(db.SCHEMA)
            for migration in (db.MIGRATIONS, db.MIGRATIONS_V2, db.MIGRATIONS_V3, db.MIGRATIONS_V4, db.MIGRATIONS_V5):
                db._add_columns(c, migration)
            c.execute(
                """INSERT INTO items(id, source_name, title, url, deadline, first_seen, last_seen, note)
                   VALUES('old', 'Board', 'Role', 'https://example.com/old',
                          '2030-03-15', '2026-01-01', '2026-01-01', 'My note')"""
            )
            c.execute("PRAGMA user_version = 5")
            c.commit()

        db.init()
        with contextlib.closing(db.connect()) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        old = db.item_by_id("old")
        self.assertIsNone(old["deadline_raw"])
        self.assertEqual(old["deadline"], "2030-03-15")
        self.assertEqual(old["note"], "My note")

        source = {"name": "Board"}
        scan_id = db.start_scan("manual", "claude", "", [])
        item = {"id": "new", "title": "Role", "url": "https://example.com/new",
                "deadline": "2030-03-15", "deadline_raw": "15 Mart 2030 17.00"}
        self.assertTrue(db.save_item(scan_id, source, item, match=1, match_key="cached"))
        self.assertEqual(db.item_by_id("new")["deadline_raw"], item["deadline_raw"])
        db.finish_scan(scan_id, "done")

        next_scan = db.start_scan("manual", "claude", "", [])
        item["deadline_raw"] = "15 Mart 2030 18.00"
        self.assertFalse(db.save_item(next_scan, source, item))
        refreshed = db.item_by_id("new")
        self.assertEqual(refreshed["deadline_raw"], item["deadline_raw"])
        self.assertEqual(refreshed["match_key"], "cached")
        self.assertIsNone(refreshed["updated_at"])
        self.assertEqual(db.item_history("new"), [])
        self.assertEqual(db.scan_changes(next_scan), [])

    def test_schema_v3_and_v1_v2_to_v3_baseline_done_migration(self):
        # v2 -> v3 migration sets baseline_done=1 for sources with prior ok=1 scans or items
        db.DB_PATH.unlink()
        with contextlib.closing(sqlite3.connect(str(db.DB_PATH))) as c:
            c.executescript(db.SCHEMA)
            db._add_columns(c, db.MIGRATIONS)
            db._add_columns(c, db.MIGRATIONS_V2)
            c.executescript(db.INDEXES_V2)
            c.execute("INSERT INTO sources(name, url, created) VALUES('Scanned OK', 'https://s1.example', '2026-01-01 00:00:00')")
            c.execute("INSERT INTO sources(name, url, created) VALUES('Has Item', 'https://s2.example', '2026-01-01 00:00:00')")
            c.execute("INSERT INTO sources(name, url, created) VALUES('Failed Only', 'https://s3.example', '2026-01-01 00:00:00')")
            c.execute("INSERT INTO sources(name, url, created) VALUES('Brand New', 'https://s4.example', '2026-01-01 00:00:00')")
            c.execute("INSERT INTO scans(started, status) VALUES('2026-01-01 00:00:00', 'done')")
            c.execute("INSERT INTO scan_sources(scan_id, source_id, source_name, ok) VALUES(1, 1, 'Scanned OK', 1)")
            c.execute("INSERT INTO scan_sources(scan_id, source_id, source_name, ok) VALUES(1, 3, 'Failed Only', 0)")
            c.execute(
                "INSERT INTO items(id, source_id, source_name, title, url, first_seen, last_seen) "
                "VALUES('v2-it-1', 2, 'Has Item', 'Old Grant', 'https://s2.example/1', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            c.execute("PRAGMA user_version = 2")
            c.commit()

        db.init()
        with contextlib.closing(db.connect()) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
            ur_cols = {r[1] for r in c.execute("PRAGMA table_info(url_readers)")}
            self.assertIn("context_hash", ur_cols)
        by_name = {s["name"]: s for s in db.sources()}
        self.assertEqual(by_name["Scanned OK"]["baseline_done"], 1)
        self.assertEqual(by_name["Has Item"]["baseline_done"], 1)
        self.assertEqual(by_name["Failed Only"]["baseline_done"], 0)
        self.assertEqual(by_name["Brand New"]["baseline_done"], 0)

        # v1 -> v3 migration also sets baseline_done=1 for sources with prior scans/items
        db.DB_PATH.unlink()
        with contextlib.closing(sqlite3.connect(str(db.DB_PATH))) as c:
            c.executescript(db.SCHEMA)
            db._add_columns(c, db.MIGRATIONS)
            c.execute(
                "INSERT INTO sources(name, url, method, extractor, extractor_ok, extractor_updated, created) "
                "VALUES('V1 Scanned', 'https://v1s.example', 'agent', 'def fetch(): return []', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            c.execute("INSERT INTO sources(name, url, created) VALUES('V1 Item', 'https://v1i.example', '2026-01-01 00:00:00')")
            c.execute("INSERT INTO sources(name, url, created) VALUES('V1 Fresh', 'https://v1f.example', '2026-01-01 00:00:00')")
            c.execute("INSERT INTO scans(started, status) VALUES('2026-01-01 00:00:00', 'done')")
            c.execute("INSERT INTO scan_sources(scan_id, source_id, source_name, ok) VALUES(1, 1, 'V1 Scanned', 1)")
            c.execute(
                "INSERT INTO items(id, source_id, source_name, title, url, first_seen, last_seen) "
                "VALUES('v1-it-1', 2, 'V1 Item', 'Old Role', 'https://v1i.example/1', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            c.execute("PRAGMA user_version = 1")
            c.commit()

        db.init()
        with contextlib.closing(db.connect()) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        v1_by_name = {s["name"]: s for s in db.sources()}
        self.assertEqual(v1_by_name["V1 Scanned"]["baseline_done"], 1)
        self.assertEqual(v1_by_name["V1 Scanned"]["last_good_extractor"], "def fetch(): return []")
        self.assertEqual(v1_by_name["V1 Item"]["baseline_done"], 1)
        self.assertEqual(v1_by_name["V1 Fresh"]["baseline_done"], 0)


if __name__ == "__main__":
    unittest.main()
