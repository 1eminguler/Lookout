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


if __name__ == "__main__":
    unittest.main()
