"""Seed a new, disposable LOOKOUT_DATA_DIR with fictional demonstration content."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DEMO_DATE = '2026-11-23'


def build_demo():
    directory = os.environ.get('LOOKOUT_DATA_DIR')
    if not directory:
        raise RuntimeError('Set LOOKOUT_DATA_DIR to a new, empty throwaway directory')
    path = Path(directory).resolve()
    if path == (ROOT / 'data').resolve() or (ROOT / 'data').resolve() in path.parents or (path.exists() and any(path.iterdir())):
        raise ValueError('Demo directory must be empty and separate from project data')
    path.mkdir(parents=True, exist_ok=True)
    from lookout import db
    if db.DB_PATH.resolve().parent != path:
        raise RuntimeError('LOOKOUT_DATA_DIR must be set before importing lookout.db')
    db.now = lambda: DEMO_DATE + ' 10:00:00'
    db.init()
    db.set_settings({'expired_cleanup_v2': '1'})
    watches = [
        ('Summer internships', 'Find embedded software internships in Türkiye or remote.', [('Kuzey Careers', 'https://example.com/careers'), ('Opportunity Board', 'https://example.org/internships')]),
        ('Mechanical keyboards back in stock', 'Watch for mechanical keyboards returning to stock.', [('Key Market', 'https://example.com/keyboards')]),
        ('Rust meetups and talks', 'Find new Rust talks and meetups.', [('Events Board', 'https://example.org/events')]),
        ('Hackathons', 'Find upcoming hackathons.', [('Hackathon Board', 'https://example.com/hackathons')]),
    ]
    sources = []
    for title, request, sites in watches:
        wid = db.add_watch(title, request)
        sources.append([db.source_by_id(db.add_source(name, url, watch_id=wid)) for name, url in sites])
        if title == 'Summer internships':
            db.add_filter('location', 'Türkiye or remote', watch_id=wid)
    scan = db.start_scan('manual', '', '', [])
    items = [
        (sources[0][0], 'internship', 'Embedded Software Summer Internship', 'Kuzey Devre', 'Internship', 'Ankara', '2026-11-28'),
        (sources[0][1], 'program', 'Engineering Summer Program', 'Mavi Teknoloji', 'Program', 'İstanbul', None),
        (sources[1][0], 'keyboard', 'Mechanical keyboard back in stock', 'Key Market', 'Product', 'Online', None),
        (sources[2][0], 'rust', 'Rust systems talk', 'Open Technology Forum', 'Event', 'Online', '2026-12-08'),
        (sources[3][0], 'hackathon', 'City Hackathon', 'Digital Commons', 'Hackathon', 'Ankara', '2026-12-12'),
    ]
    for src, ident, title, org, category, location, deadline in items:
        db.save_item(scan, src, dict(id=ident, title=title, org=org, category=category, location=location,
                                   deadline=deadline, url='https://example.com/' + ident,
                                   summary='A fictional example for the Lookout demo.'), match=1, match_key='demo')
    db.set_item('internship', action='saved', remind_on='2026-11-26', note='Prepare the project summary')
    db.finish_scan(scan, 'done')
    return {'watch_id': sources[0][0]['watch_id'], 'agenda_id': 'internship'}


if __name__ == '__main__':
    build_demo()
