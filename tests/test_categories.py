"""Category grouping for Feed filters and counts."""
import unittest

from lookout import categories, db
from tests.helpers import TempLookoutTestCase


class CategoryGroupTests(unittest.TestCase):
    def test_site_specific_names_fold_into_groups(self):
        cases = {
            "staj": categories.INTERNSHIP, "Staj İlanları": categories.INTERNSHIP, "Stajyer · AI / ML": categories.INTERNSHIP,
            "Internship": categories.INTERNSHIP, "Data/Analytics internship": categories.INTERNSHIP,
            "Genç Yetenek Programı": categories.TALENT, "burs": categories.TALENT, "Young Talent": categories.TALENT,
            "hackathon": categories.EVENTS, "etkinlik": categories.EVENTS, "Bootcamp": categories.EVENTS,
            "Full stack": categories.SOFTWARE, "Yazılım Geliştirici": categories.SOFTWARE, "Ar-Ge": categories.SOFTWARE,
            "Embedded systems": categories.SOFTWARE, "UI / UX": categories.SOFTWARE,
            "Sales & Business Development": categories.BUSINESS, "Operations": categories.BUSINESS,
            "İş Analisti": categories.BUSINESS, "Satış ve Müşteri İlişkileri Yönetimi": categories.BUSINESS,
            "İş İlanı": categories.OTHER, "Tam Zamanlı": categories.OTHER, "": categories.OTHER, None: categories.OTHER,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(categories.group(raw), expected)

    def test_words_that_only_contain_ar_ge_are_not_rnd(self):
        self.assertEqual(categories.group("Large accounts"), categories.OTHER)
        self.assertEqual(categories.group("Charge nurse"), categories.OTHER)


class CategoryFacetTests(TempLookoutTestCase):
    def test_facets_and_filter_use_groups(self):
        sid = db.add_source("Board", "https://board.example.com")
        src = db.source_by_id(sid)
        scan = db.start_scan("manual", "claude", "", [])
        for n, cat in enumerate(["staj", "Staj İlanları", "Internship", "hackathon", "Full stack"]):
            db.save_item(scan, src, {"id": f"c{n}", "title": f"Item {n}", "url": f"https://board.example.com/{n}",
                                     "category": cat})
        facets = db.items_facets(status="open", match_only=False, exclude_expired=False, snoozed="all")
        self.assertEqual(facets["categories"].get(categories.INTERNSHIP), 3)
        self.assertEqual(facets["categories"].get(categories.EVENTS), 1)
        self.assertEqual(facets["categories"].get(categories.SOFTWARE), 1)
        self.assertNotIn("staj", facets["categories"])
        rows = db.items(status="open", categories=[categories.INTERNSHIP])
        self.assertEqual(sorted(r["category"] for r in rows), ["Internship", "Staj İlanları", "staj"])


if __name__ == "__main__":
    unittest.main()
