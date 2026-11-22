"""Example plugin: latest "Who is hiring" style job posts from the public Hacker News API."""
import json
import urllib.request

API = "https://hacker-news.firebaseio.com/v0"


def _get(path):
    with urllib.request.urlopen(f"{API}/{path}.json", timeout=15) as r:
        return json.load(r)


def fetch_hn_jobs():
    items = []
    for story_id in _get("jobstories")[:30]:
        s = _get(f"item/{story_id}")
        if not s or not s.get("title"):
            continue
        items.append({
            "id": f"hn-{story_id}",
            "title": s["title"],
            "url": s.get("url") or f"https://news.ycombinator.com/item?id={story_id}",
            "category": "job",
            "org": "Hacker News",
        })
    return items


SOURCES = [{"name": "Hacker News jobs (example)", "url": "https://news.ycombinator.com/jobs", "fetch": fetch_hn_jobs}]
