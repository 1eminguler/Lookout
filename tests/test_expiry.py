"""Tests for expiry rules: yearless dates, closed markers, stale years, the stored expired column and cleanup."""
import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from lookout import db, scanner, tasks
from tests.helpers import TempLookoutTestCase

TODAY = date(2026, 11, 22)


class YearlessDateTests(TempLookoutTestCase):
    def test_yearless_day_month_turkish_and_english(self):
        cases = {
            "13 Kasım": "2026-11-13",
            "13 Kas": "2026-11-13",
            "son gün: 13 kasım": "2026-11-13",
            "Nov 13": "2026-11-13",
            "13 November": "2026-11-13",
            "13.11": "2026-11-13",
            "13/11": "2026-11-13",
            "31 Ağustos": "2026-08-31",
            "28 EKİM": "2026-10-28",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(scanner.parse_date(raw, today=TODAY), want)

    def test_yearless_prefers_recent_past_unless_next_occurrence_is_soon(self):
        # Past-biased rule: the most recent occurrence on or before today wins…
        self.assertEqual(scanner.parse_date("13 Kasım", today=TODAY), "2026-11-13")
        self.assertEqual(scanner.parse_date("08 Mart", today=TODAY), "2026-03-08")
        self.assertEqual(scanner.parse_date("07 Mayıs", today=TODAY), "2026-05-07")
        self.assertEqual(scanner.parse_date("31 Ağustos", today=TODAY), "2026-08-31")
        # …unless the next occurrence is within YEARLESS_FUTURE_DAYS days
        self.assertEqual(scanner.parse_date("05 Ocak", today=TODAY), "2027-01-05")
        self.assertEqual(scanner.parse_date("15 Aralık", today=TODAY), "2026-12-15")
        # Boundary: exactly YEARLESS_FUTURE_DAYS days ahead wins, one day more falls back to the past
        self.assertEqual(scanner.parse_date("21 Ocak", today=TODAY), "2027-01-21")
        self.assertEqual(scanner.parse_date("22 Ocak", today=TODAY), "2026-01-22")
        # Mid-year: the next January 1st is far away, so the one that just passed wins
        self.assertEqual(scanner.parse_date("01 Ocak", today=date(2026, 7, 1)), "2026-01-01")

    def test_full_dates_still_win_and_garbage_stays_none(self):
        self.assertEqual(scanner.parse_date("13 Kasım 2027", today=TODAY), "2027-11-13")
        self.assertEqual(scanner.parse_date("13.11.2027", today=TODAY), "2027-11-13")
        self.assertEqual(scanner.parse_date("Nov 13, 2027", today=TODAY), "2027-11-13")
        self.assertIsNone(scanner.parse_date("30 Şubat", today=TODAY))
        self.assertIsNone(scanner.parse_date("13 Foo", today=TODAY))
        self.assertIsNone(scanner.parse_date("may 2027", today=TODAY))
        self.assertIsNone(scanner.parse_date("2026.11", today=TODAY))
        self.assertIsNone(scanner.parse_date("", today=TODAY))
        self.assertIsNone(scanner.parse_date(None, today=TODAY))

    def test_normalise_parses_yearless_deadline_and_reader_closed_flag(self):
        src = {"id": 3, "name": "Src", "url": "https://ex.com/list"}
        it = scanner.normalise(
            {"title": "Staj", "url": "https://ex.com/1", "deadline": "13 Kasım", "closed": True}, src, today=TODAY)
        self.assertEqual(it["deadline"], "2026-11-13")
        self.assertTrue(it["closed"])
        it2 = scanner.normalise({"title": "Staj", "url": "https://ex.com/2", "status": "closed"}, src, today=TODAY)
        self.assertTrue(it2["closed"])
        it3 = scanner.normalise({"title": "Staj", "url": "https://ex.com/3", "status": "open"}, src, today=TODAY)
        self.assertFalse(it3["closed"])
        it4 = scanner.normalise({"title": "Staj", "url": "https://ex.com/4"}, src, today=TODAY)
        self.assertFalse(it4["closed"])

    def test_normalise_marks_inferred_deadline_year_in_extra(self):
        src = {"id": 3, "name": "Src", "url": "https://ex.com/list"}
        it = scanner.normalise(
            {"title": "Staj", "url": "https://ex.com/1", "deadline": "13 Kasım"}, src, today=TODAY)
        self.assertEqual(json.loads(it["extra"]), {"deadline_inferred": True})
        # Existing extra content is kept
        it2 = scanner.normalise(
            {"title": "Staj", "url": "https://ex.com/2", "deadline": "05 Ocak", "extra": {"button": "Başvur"}},
            src, today=TODAY)
        self.assertEqual(json.loads(it2["extra"]), {"button": "Başvur", "deadline_inferred": True})
        # A deadline whose year is written in the text is not inferred
        it3 = scanner.normalise(
            {"title": "Staj", "url": "https://ex.com/3", "deadline": "13 Kasım 2027"}, src, today=TODAY)
        self.assertIsNone(it3["extra"])
        it4 = scanner.normalise(
            {"title": "Staj", "url": "https://ex.com/4", "deadline": "2027-01-05", "extra": {"a": 1}},
            src, today=TODAY)
        self.assertEqual(json.loads(it4["extra"]), {"a": 1})
        # Only the deadline sets the flag, never the start_date
        it5 = scanner.normalise(
            {"title": "Staj", "url": "https://ex.com/5", "start_date": "13 Kasım"}, src, today=TODAY)
        self.assertIsNone(it5["extra"])


class ClosedMarkerTests(TempLookoutTestCase):
    def test_turkish_and_english_markers_mark_expired(self):
        for field, text in (
            ("title", "ELEMENTS Uzun Dönem Staj Programı Tamamlandı"),
            ("title", "TAMAMLANDI"),
            ("category", "Kapandı"),
            ("title", "KAPANDI"),
            ("summary", "Başvurular sona erdi."),
            ("summary", "Başvuru süresi doldu"),
            ("summary", "Süresi doldu"),
            ("title", "This call has expired"),
            ("title", "No longer accepting applications"),
            ("summary", "Applications closed"),
            ("extra", '{"button": "Closed"}'),
        ):
            with self.subTest(text=text):
                item = {"title": "Job", "deadline": None}
                item[field] = text
                self.assertTrue(scanner._is_expired(item, today=TODAY))

    def test_similar_words_without_closed_marker_stay_open(self):
        for text in ("Kapanış tarihi yakında", "Kapanış: 13 Kasım", "closing soon", "enclosed herewith"):
            with self.subTest(text=text):
                self.assertFalse(scanner._is_expired({"title": text, "deadline": None}, today=TODAY))

    def test_reader_closed_flag_and_status(self):
        self.assertTrue(scanner._is_expired({"title": "Job", "closed": True}, today=TODAY))
        self.assertTrue(scanner._is_expired({"title": "Job", "status": "closed"}, today=TODAY))
        self.assertTrue(scanner._is_expired({"title": "Job", "status": "Closed"}, today=TODAY))
        self.assertFalse(scanner._is_expired({"title": "Job", "closed": False}, today=TODAY))
        self.assertFalse(scanner._is_expired({"title": "Job", "status": "open"}, today=TODAY))


class StaleYearTests(TempLookoutTestCase):
    def test_past_years_in_title_or_url_mark_expired(self):
        self.assertTrue(scanner._is_expired({"title": "ELEMENTS Uzun Dönem Staj Programı 2021"}, today=TODAY))
        self.assertTrue(scanner._is_expired({"title": "Program", "url": "https://ex.com/we-power-2023/"}, today=TODAY))
        self.assertTrue(scanner._is_expired({"title": "Nextremers 2022"}, today=TODAY))
        self.assertTrue(scanner._is_expired({"title": "Gönüllü 2001-2023 programı"}, today=TODAY))

    def test_current_future_and_non_year_numbers_are_ignored(self):
        self.assertFalse(scanner._is_expired({"title": "Program 2026"}, today=TODAY))
        self.assertFalse(scanner._is_expired({"title": "2027 Yılı Bursları"}, today=TODAY))
        self.assertFalse(scanner._is_expired({"title": "İlan", "url": "https://ex.com/ilan/140"}, today=TODAY))
        self.assertFalse(scanner._is_expired({"title": "Toplantı 140 kişilik"}, today=TODAY))
        self.assertFalse(scanner._is_expired({"title": "No year here"}, today=TODAY))

    def test_stale_year_ignored_when_item_has_a_date(self):
        self.assertFalse(scanner._is_expired({"title": "Program 2021", "deadline": "2027-01-01"}, today=TODAY))
        self.assertFalse(scanner._is_expired({"title": "Program 2021", "start_date": "2026-12-01"}, today=TODAY))


class InferredDeadlineTests(TempLookoutTestCase):
    SRC = {"id": 3, "name": "Src", "url": "https://ex.com/list"}

    def test_old_years_in_text_beat_a_guessed_deadline(self):
        # "Talent Nest Summer Internship 2023" with a yearless "05 Ocak" deadline: the guessed
        # 2027-01-05 is still in the future, but the title only mentions 2023, so it is expired.
        it = scanner.normalise(
            {"title": "Talent Nest Summer Internship 2023", "url": "https://ex.com/1", "deadline": "05 Ocak"},
            self.SRC, today=TODAY)
        self.assertEqual(it["deadline"], "2027-01-05")
        self.assertTrue(scanner._is_expired(it, today=TODAY))

    def test_old_years_in_url_beat_a_guessed_deadline(self):
        it = scanner.normalise(
            {"title": "We Power", "url": "https://ex.com/we-power-2023/", "deadline": "05 Ocak"},
            self.SRC, today=TODAY)
        self.assertTrue(scanner._is_expired(it, today=TODAY))

    def test_stored_row_with_inferred_flag_is_expired(self):
        row = {"title": "Talent Nest Summer Internship 2023", "url": "https://ex.com/1",
               "deadline": "2027-01-05", "extra": json.dumps({"deadline_inferred": True})}
        self.assertTrue(scanner._is_expired(row, today=TODAY))
        # Same deadline, but the year was written in the text (no flag) -> stays open
        row_real = {"title": "Talent Nest Summer Internship 2023", "url": "https://ex.com/2",
                    "deadline": "2027-01-05"}
        self.assertFalse(scanner._is_expired(row_real, today=TODAY))

    def test_guessed_deadline_without_old_years_stays_open(self):
        for title in ("Talent Nest Summer Internship", "Summer Internship 2026", "Burslar 2027"):
            it = scanner.normalise(
                {"title": title, "url": "https://ex.com/1", "deadline": "05 Ocak"}, self.SRC, today=TODAY)
            with self.subTest(title=title):
                self.assertFalse(scanner._is_expired(it, today=TODAY))

    def test_mixed_years_with_a_current_or_future_year_stay_open(self):
        it = scanner.normalise(
            {"title": "Gönüllü 2023-2027 programı", "url": "https://ex.com/1", "deadline": "05 Ocak"},
            self.SRC, today=TODAY)
        self.assertFalse(scanner._is_expired(it, today=TODAY))

    def test_bad_extra_json_is_not_treated_as_inferred(self):
        row = {"title": "Talent Nest 2023", "url": "https://ex.com/1", "deadline": "2027-01-05",
               "extra": "not json"}
        self.assertFalse(scanner._is_expired(row, today=TODAY))


class ExpiredDecisionTests(TempLookoutTestCase):
    def test_passes_hides_expired_items(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.assertFalse(scanner.passes({"title": "Tamamlandı Program", "deadline": None}, []))
        self.assertFalse(scanner.passes({"title": "Program 2021", "deadline": None}, []))
        self.assertFalse(scanner.passes({"title": "Old", "deadline": yesterday}, []))
        self.assertTrue(scanner.passes({"title": "Fresh Program", "deadline": None}, []))


class StoredExpiryTests(TempLookoutTestCase):
    SRC = {"id": None, "name": "S"}

    def test_save_item_stores_the_expired_decision(self):
        scan = db.start_scan("manual", "", "", [])
        db.save_item(scan, self.SRC, {"id": "a1", "title": "A", "url": "https://ex.com/a"}, expired=1)
        db.save_item(scan, self.SRC, {"id": "b1", "title": "B", "url": "https://ex.com/b"})
        self.assertEqual(db.item_by_id("a1")["expired"], 1)
        self.assertEqual(db.item_by_id("b1")["expired"], 0)
        # Re-saving with an explicit 0 clears the flag (e.g. the deadline moved to the future)
        db.save_item(scan, self.SRC, {"id": "a1", "title": "A", "url": "https://ex.com/a",
                                      "deadline": "2027-01-01"}, expired=0)
        self.assertEqual(db.item_by_id("a1")["expired"], 0)

    def test_items_and_counts_exclude_expired_rows(self):
        scan = db.start_scan("manual", "", "", [])
        db.save_item(scan, self.SRC, {"id": "ok1", "title": "Fresh", "url": "https://ex.com/1"}, match=1, match_key="k")
        db.save_item(scan, self.SRC, {"id": "ex1", "title": "Old 2021", "url": "https://ex.com/2"}, match=1, match_key="k")
        db.mark_items_expired(["ex1"])
        ids = [i["id"] for i in db.items(status="open", match_only=True, exclude_expired=True)]
        self.assertEqual(ids, ["ok1"])
        self.assertEqual(db.items_count(status="open", match_only=True, exclude_expired=True), 1)
        cnt = db.counts()
        self.assertEqual(cnt["feed_open"], 1)

    def test_feed_watch_agenda_and_notifications_exclude_expired(self):
        wid = db.add_watch("W")
        src = {"id": None, "name": "S", "watch_id": wid}
        scan = db.start_scan("manual", "", "", [])
        db.save_item(scan, src, {"id": "ok1", "title": "Fresh", "url": "https://ex.com/1"}, match=1, match_key="k")
        db.save_item(scan, src, {"id": "ex1", "title": "Old 2021", "url": "https://ex.com/2"}, match=1, match_key="k")
        db.save_item(scan, src, {"id": "ag1", "title": "Saved", "url": "https://ex.com/3",
                                 "deadline": "2027-01-01"}, match=1, match_key="k")
        db.save_item(scan, src, {"id": "ag2", "title": "Saved But Closed", "url": "https://ex.com/4",
                                 "deadline": "2027-01-01"}, match=1, match_key="k")
        db.set_item("ag1", action="saved")
        db.set_item("ag2", action="saved")
        db.mark_items_expired(["ex1", "ag2"])

        self.assertEqual(db.counts()["feed_open"], 2)  # ok1 + ag1
        self.assertEqual({i["id"] for i in db.watch_feed_items(wid)}, {"ok1", "ag1"})
        self.assertEqual([i["id"] for i in db.agenda_items()], ["ag1"])
        self.assertEqual({i["id"] for i in db.unnotified()}, {"ok1", "ag1"})
        self.assertEqual({i["id"] for i in db.scan_changes(scan)}, {"ok1", "ag1"})


class CleanupTests(TempLookoutTestCase):
    def test_expire_stored_items_marks_but_keeps_rows(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        scan = db.start_scan("manual", "", "", [])
        db.save_item(scan, {"id": None, "name": "S"}, {"id": "dl", "title": "Past deadline",
                                                       "url": "https://ex.com/1", "deadline": yesterday})
        db.save_item(scan, {"id": None, "name": "S"}, {"id": "mk", "title": "Etkinlik Tamamlandı",
                                                       "url": "https://ex.com/2"})
        db.save_item(scan, {"id": None, "name": "S"}, {"id": "yr", "title": "Program 2021",
                                                       "url": "https://ex.com/3"})
        db.save_item(scan, {"id": None, "name": "S"}, {"id": "ok", "title": "Gençlik Programı",
                                                       "url": "https://ex.com/4"})
        self.assertEqual(scanner.expire_stored_items(), 3)
        for iid in ("dl", "mk", "yr"):
            row = db.item_by_id(iid)
            self.assertEqual(row["expired"], 1)
            self.assertEqual(row["status"], "open")  # marked, not deleted
        self.assertEqual(db.item_by_id("ok")["expired"], 0)

    def test_expire_stored_items_once_runs_a_single_time(self):
        scan = db.start_scan("manual", "", "", [])
        db.save_item(scan, {"id": None, "name": "S"}, {"id": "mk", "title": "Program Tamamlandı",
                                                       "url": "https://ex.com/1"})
        self.assertEqual(scanner.expire_stored_items_once(), 1)
        self.assertEqual(db.get_settings()["expired_cleanup_v2"], "1")
        # The settings flag guards the second pass
        db.save_item(scan, {"id": None, "name": "S"}, {"id": "mk2", "title": "Kapandı",
                                                       "url": "https://ex.com/2"})
        self.assertEqual(scanner.expire_stored_items_once(), 0)
        self.assertEqual(db.item_by_id("mk2")["expired"], 0)

    def test_expire_stored_items_once_runs_even_when_v1_already_ran(self):
        # The user's machine already ran the v1 pass with the old year rule; v2 must still run once.
        db.set_settings({"expired_cleanup_v1": "1"})
        scan = db.start_scan("manual", "", "", [])
        db.save_item(scan, {"id": None, "name": "S"}, {"id": "mk", "title": "Program Tamamlandı",
                                                       "url": "https://ex.com/1"})
        self.assertEqual(scanner.expire_stored_items_once(), 1)
        self.assertEqual(db.item_by_id("mk")["expired"], 1)
        self.assertEqual(db.get_settings()["expired_cleanup_v2"], "1")

    def test_scan_marks_stored_items_expired_afterwards(self):
        (self.plugin_dir / "p.py").write_text(
            "def fetch():\n"
            "    raise RuntimeError('site down')\n"
            "SOURCES = [{'name': 'P', 'url': 'https://ex.com', 'fetch': fetch}]\n",
            encoding="utf-8",
        )
        scan = db.start_scan("manual", "", "", [])
        db.save_item(scan, {"id": None, "name": "P"}, {"id": "old", "title": "Eski Program Tamamlandı",
                                                       "url": "https://ex.com/old"})
        db.finish_scan(scan, "done")
        scan_id = scanner.run_scan("manual")
        self.assertIsNotNone(scan_id)
        row = db.item_by_id("old")
        self.assertEqual(row["expired"], 1)
        self.assertEqual(row["status"], "open")


class ReaderVerificationTests(TempLookoutTestCase):
    def test_verify_reader_output_rejects_mostly_expired_readers(self):
        items = [
            {"title": "Tamamlandı Programı", "url": "https://ex.com/1"},
            {"title": "Kapandı 2021", "url": "https://ex.com/2"},
            {"title": "Program 2023", "url": "https://ex.com/3"},
        ]
        ok, reason = scanner.verify_reader_output(items, items, source={"id": 1, "name": "S"}, today=TODAY)
        self.assertFalse(ok)
        self.assertEqual(reason, "returns closed listings")

    def test_verify_reader_output_accepts_open_readers(self):
        items = [{"title": f"Fresh {k}", "url": f"https://ex.com/{k}", "org": "O", "deadline": "2027-06-01"}
                 for k in range(4)]
        ok, reason = scanner.verify_reader_output(items, items, source={"id": 1, "name": "S"}, today=TODAY)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_learn_that_returns_closed_listings_is_marked_for_relearn(self):
        db.set_settings({"agent": "claude"})
        sid = db.add_source("ClosedBoard", "https://closed.example.com")
        closed = {
            "items": [{"title": "Tamamlandı", "url": "https://closed.example.com/1"}],
            "code": "def fetch():\n    return [{'title': 'Tamamlandı', 'url': 'https://closed.example.com/1'}]\n",
            "log": "",
        }
        with patch.object(tasks, "learn_source", return_value=closed):
            scanner.read_agent_source(db.source_by_id(sid), [])
        s = db.source_by_id(sid)
        self.assertEqual(s["extractor_ok"], 0)
        self.assertEqual(s["learn_fail_count"], 1)
        self.assertIsNotNone(s["learn_retry_after"])
        self.assertIn("returns closed listings", s["extractor_error"])


class ReaderPromptTests(unittest.TestCase):
    def test_reader_prompts_require_full_deadline_lookup(self):
        learn_md = tasks.LEARN.format(browser="", name="N", url="https://ex.com", notes="none", previous="",
                                      filters="- No filters", minutes=5, session="s", safety="")
        quick_md = tasks.QUICK_READ.format(browser="", name="N", url="https://ex.com", notes="none",
                                           filters="- No filters", minutes=2, session="s", safety="")
        for name, prompt in (("learn", learn_md), ("quick read", quick_md)):
            with self.subTest(prompt=name):
                wording = " ".join(prompt.split())
                self.assertIn("currently open", wording)
                self.assertIn("Tamamlandı", wording)
                self.assertIn("deadline without a year", wording)
                self.assertIn("open that listing's own detail page", wording)
                self.assertIn("take the full deadline date with the year from there", wording)
                self.assertIn("at most 40 detail requests per run", wording)
                self.assertIn("yearless deadline text only if no full deadline date is available", wording)
        self.assertIn("Reuse the throttled `get` helper for detail requests", learn_md)


if __name__ == "__main__":
    unittest.main()
