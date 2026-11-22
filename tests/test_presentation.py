"""Offline checks for the disposable presentation data and icon."""
import os
from contextlib import closing
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PresentationTests(unittest.TestCase):
    def test_demo_is_fictional_and_refuses_existing_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, LOOKOUT_DATA_DIR=directory)
            script = str(ROOT / 'tools' / 'demo_data.py')
            subprocess.run([sys.executable, script], cwd=ROOT, env=env, check=True)
            with closing(sqlite3.connect(Path(directory) / 'lookout.db')) as db:
                self.assertEqual(db.execute('select count(*) from watches').fetchone()[0], 4)
                self.assertEqual(db.execute("select count(*) from sources where watch_id=(select id from watches where title='Summer internships')").fetchone()[0], 2)
                self.assertEqual(db.execute("select value from filters where kind='location'").fetchone()[0], 'Türkiye or remote')
                self.assertEqual(db.execute("select note from items where action='saved'").fetchone()[0], 'Prepare the project summary')
                self.assertGreater(db.execute('select count(*) from items where deadline is null').fetchone()[0], 0)
            result = subprocess.run([sys.executable, script], cwd=ROOT, env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_icon_geometry_and_sizes(self):
        self.assertEqual((ROOT / 'web/icon.svg').read_bytes(), (ROOT / 'docs/logo.svg').read_bytes())
        ico = (ROOT / 'web/icon.ico').read_bytes()
        self.assertEqual(struct.unpack_from('<HHH', ico), (0, 1, 6))
        self.assertEqual([ico[6 + 16*i] or 256 for i in range(6)], [16, 24, 32, 48, 64, 256])


if __name__ == '__main__':
    unittest.main()
