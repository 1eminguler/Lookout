# Local plugins

Most sources need no code: add a URL in **Sources** and your AI agent browses it.
A plugin is for a site you would rather read with a few lines of Python — for example
one with a clean JSON API. Plugins are fast and use no agent quota.

Put a `.py` file in this folder that defines `SOURCES`:

```python
SOURCES = [
    {"name": "My site", "url": "https://example.com/jobs", "fetch": fetch_my_site},
]
```

`fetch()` takes no arguments and returns a list of dicts. Raise an exception when the site
cannot be read; Lookout shows the source as failed instead of silently returning nothing.

| key | required | meaning |
|---|---|---|
| `title`, `url` | yes | item title and a link to it |
| `org`, `category`, `location`, `summary` | no | free text |
| `deadline`, `start_date` | no | a date (`2027-01-15`, `15.01.2027`, `Jan 15, 2027` all work) |
| `id` | no | stable id; defaults to source + url + title |
| `extra` | no | dict with anything else (price, salary…) |

Plugins appear in **Sources** automatically and can be switched on and off there.
Files starting with `_` are not loaded — `_example_hn_jobs.py` is a template: copy it to a new name to use it.
Everything in this folder except this README and the example is ignored by git.
