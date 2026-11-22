"""Unit tests for lookout.tasks."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from lookout import agents, db, tasks
from tests.helpers import TempLookoutTestCase


class TaskTests(TempLookoutTestCase):
    def setUp(self):
        super().setUp()
        db.set_settings({"agent": "claude", "model": "sonnet"})

    def test_learn_source_happy_and_partial(self):
        seen_paths = []

        def fake_run(aid, model, workdir: Path, timeout, **kw):
            task_txt = (workdir / "TASK.md").read_text(encoding="utf-8")
            seen_paths.append(str(workdir) in task_txt)
            (workdir / "results.json").write_text(
                json.dumps([{"title": "Job 1", "url": "https://ex.com/1"}]), encoding="utf-8")
            (workdir / "extractor.py").write_text("def fetch():\n    return []\n", encoding="utf-8")
            return "done"

        src = {"name": "Ex", "url": "https://ex.com", "notes": "tab 2"}
        with patch.object(agents, "run_task", side_effect=fake_run):
            out = tasks.learn_source(src, [])
        self.assertTrue(seen_paths[0])
        self.assertEqual(len(out["items"]), 1)
        self.assertIn("def fetch", out["code"])

        # Partial result: results.json present, extractor.py missing
        def fake_partial(aid, model, workdir: Path, timeout, **kw):
            (workdir / "results.json").write_text(json.dumps([{"title": "Only", "url": "https://ex.com/o"}]), encoding="utf-8")
            return "partial"

        with patch.object(agents, "run_task", side_effect=fake_partial):
            out2 = tasks.learn_source(src, [])
        self.assertEqual(len(out2["items"]), 1)
        self.assertIsNone(out2["code"])

    def test_classify_happy_missing_and_invalid(self):
        items = [{"id": "a1", "title": "ML Intern"}, {"id": "a2", "title": "Sales"}]

        def fake_ok(aid, model, workdir: Path, timeout, **kw):
            self.assertIn(str(workdir), (workdir / "TASK.md").read_text(encoding="utf-8"))
            (workdir / "decisions.json").write_text('{"a1": true, "a2": false}', encoding="utf-8")
            return ""

        with patch.object(agents, "run_task", side_effect=fake_ok):
            self.assertEqual(tasks.classify(items, []), {"a1": True, "a2": False})

        with patch.object(agents, "run_task", return_value=""):
            with self.assertRaises(RuntimeError):
                tasks.classify(items, [])

        def fake_bad_json(aid, model, workdir: Path, timeout, **kw):
            (workdir / "decisions.json").write_text("{bad json", encoding="utf-8")
            return ""

        with patch.object(agents, "run_task", side_effect=fake_bad_json):
            with self.assertRaises(RuntimeError):
                tasks.classify(items, [])

    def test_discover_sources_full_partial_missing_and_invalid(self):
        def fake_full(aid, model, workdir: Path, timeout, **kw):
            self.assertIn(str(workdir), (workdir / "TASK.md").read_text(encoding="utf-8"))
            (workdir / "title.txt").write_text('"CS internships Türkiye."\nsecond line', encoding="utf-8")
            (workdir / "sources.json").write_text(json.dumps([
                {"name": "Board", "url": "https://board.example.com/jobs", "why": "Lists CS internships", "notes": "tab"},
                {"name": "BadURL", "url": "ftp://nope"},
            ]), encoding="utf-8")
            (workdir / "filters.json").write_text(json.dumps([
                {"kind": "type", "value": "internship"},
                {"kind": "exclude", "value": "panel, senior"},
                {"kind": "deadline_within", "value": "not-a-number"},
                {"kind": "bogus", "value": "x"},
            ]), encoding="utf-8")
            return ""

        with patch.object(agents, "run_task", side_effect=fake_full):
            out = tasks.discover_sources("cs internships in turkiye")
        self.assertEqual(out["title"], "CS internships Türkiye")
        self.assertEqual(len(out["sources"]), 1)
        self.assertEqual(out["sources"][0]["name"], "Board")
        self.assertEqual(out["filters"], [
            {"kind": "type", "value": "internship"},
            {"kind": "exclude", "value": "panel, senior"},
        ])

        # Partial: sources ok, filters invalid JSON, title.txt missing
        def fake_partial(aid, model, workdir: Path, timeout, **kw):
            (workdir / "sources.json").write_text('[{"url": "https://onlyurl.example.com"}]', encoding="utf-8")
            (workdir / "filters.json").write_text("not json", encoding="utf-8")
            return ""

        with patch.object(agents, "run_task", side_effect=fake_partial):
            out2 = tasks.discover_sources("anything")
        self.assertEqual(out2["sources"][0]["name"], "onlyurl.example.com")
        self.assertEqual(out2["filters"], [])
        self.assertIsNone(out2["title"])

        # Missing sources.json
        with patch.object(agents, "run_task", return_value="no output"):
            with self.assertRaises(RuntimeError):
                tasks.discover_sources("anything")

        # Invalid JSON in sources.json
        def fake_bad_sources(aid, model, workdir: Path, timeout, **kw):
            (workdir / "sources.json").write_text("{corrupt", encoding="utf-8")
            return ""

        with patch.object(agents, "run_task", side_effect=fake_bad_sources):
            with self.assertRaises(RuntimeError):
                tasks.discover_sources("anything")

    def test_quick_title(self):
        self.assertEqual(tasks.quick_title("türkiye geneli bilgisayar mühendisliği için staj ilanları"),
                         "Türkiye geneli bilgisayar mühendisliği staj")
        self.assertEqual(tasks.quick_title("looking for new machine learning internships"),
                         "Machine learning internships")
        self.assertEqual(tasks.quick_title("   "), "Web search")
        self.assertLessEqual(len(tasks.quick_title("word " * 20)), 48)


if __name__ == "__main__":
    unittest.main()
