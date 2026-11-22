# Contributing to Lookout

Thanks for helping improve Lookout. The codebase is intentionally small: Python standard library on the back end, plain HTML/CSS/JavaScript on the front end (no build step, strict CSP with zero inline styles/scripts), and an offline `unittest` suite.

Please also read [SECURITY.md](SECURITY.md) before modifying agent permissions, the reader sandbox, SSRF checks, or local API authentication.

## Run from source

```bash
# Editable install via pyproject.toml (or pip install -r requirements.txt)
pip install -e ".[desktop,dev]"
python run.py            # desktop window (falls back to browser if pywebview cannot start)
python run.py --browser  # open directly in your web browser, handy with dev tools
```

Use a separate data folder and port while developing so your real `data/lookout.db` stays untouched:

```bash
LOOKOUT_DATA_DIR=/tmp/lookout-dev LOOKOUT_PORT=8790 python run.py --browser
```

Supported environment variables: `LOOKOUT_DATA_DIR`, `LOOKOUT_PLUGIN_DIR`, `LOOKOUT_PORT`, `LOOKOUT_BIND`, `LOOKOUT_USER_AGENT`, `LOOKOUT_MIN_INTERVAL`, `LOOKOUT_RESPECT_ROBOTS`, and `LOOKOUT_REQUIRE_UI_TEST`.

## Run the checks

Run the same checks as CI ([`.github/workflows/check.yml`](.github/workflows/check.yml)) before opening a pull request:

```bash
python -m compileall -q run.py lookout plugins tests
node --check web/app.js
ruff check .
python -m unittest discover -s tests -v
```

- CI runs on `ubuntu-latest`, `windows-latest`, and `macos-latest` across Python 3.10 and 3.13, plus a `ruff` lint job and a Playwright + Chromium UI smoke test job (`tests/test_ui.py` with `LOOKOUT_REQUIRE_UI_TEST=1`).
- All unit, integration, and UI tests must remain fast and completely offline (no live internet requests, no real agent CLIs, and no writes to `data/` or port `8780`).

## Architecture & security invariants

### Single source of truth for reader sandbox constants

The reader allow/deny constants live in **one** place in [`lookout/lookout_extract.py`](lookout/lookout_extract.py):

- `SANDBOX_HELPERS` (`get`, `get_json`, `post_json`, `browser_eval`, `absolute`, `allowed`)
- `SANDBOX_MODULES`
- `SANDBOX_BANNED_CALLS`
- `SANDBOX_BANNED_ATTRS`

Both the AST validator (`extractors.check`) and the runtime `RUNNER` (`_safe_import`) in [`lookout/extractors.py`](lookout/extractors.py) import those exact constants. Never duplicate allow-lists or deny-lists as raw strings in `RUNNER` or tests — update [`lookout/lookout_extract.py`](lookout/lookout_extract.py) and add regression tests proving parse-time and runtime checks stay identical.

### Local auth, CSP, and `custom_command` invariants

- **One-time launch secret -> session cookie:** `server.make_server()` issues `srv.launch_secret` (and writes `data/.launch_secret`), which `GET /?k=<secret>` consumes once in exchange for an `HttpOnly; SameSite=Strict` `lookout_session` cookie before redirecting (`302`) to `/`. Never embed `API_TOKEN` or session tokens in `index.html`.
- **Strict CSP:** Every HTTP response carries `Content-Security-Policy: default-src 'self'; img-src 'self' data:; frame-ancestors 'none'`. Do not add inline `style="..."` attributes, inline `<script>` blocks, or `eval()` calls in `web/index.html` or `web/app.js`.
- **No `custom_command` over HTTP:** `POST /api/settings` must reject `"custom_command"` with `403`, and `/api/state` and `/api/agents` must never return the raw command string. Custom commands are read only from `--custom-command` or `data/agent_command.txt`.

### Test patterns (`tests/helpers.py`)

Shared fixtures live in [`tests/helpers.py`](tests/helpers.py):

- `TempLookoutTestCase` (`LookoutTestCase`): redirects `LOOKOUT_DATA_DIR`, `LOOKOUT_PLUGIN_DIR`, `LOOKOUT_PORT`, `lookout.db.DB_PATH`, `lookout.scanner.PLUGIN_DIR`, and `lookout.tasks.WORK` to a temporary directory per test and resets module caches (including `scanner.progress`, `server.discovery`, `server.test_job`, `lookout_extract` throttle/robots caches, and CLI custom command state).
- `seed_db()`: populates realistic watches, sources, filters, scans, items, discoveries, settings, and agent usage rows.
- `LiveServer`: starts `lookout.server.Handler` on `127.0.0.1:0` in a background thread with `launch_secret`, `launch_secrets`, `session_tokens`, and `api_token` initialized. When testing `/api/*` endpoints, authenticate either by exchanging `GET /?k=<srv.launch_secret>` for a `lookout_session` cookie or via `X-Lookout-Token`, and include negative tests verifying that unauthenticated `/api/*` requests receive `401`, replayed launch secrets receive `401`, and bad `Host`/`Origin`/`Referer`/`Sec-Fetch-Site` headers receive `403`.
- `make_fake_agent(responses)`: replaces `lookout.agents.run_task` to read `TASK.md` and write mock `results.json`, `extractor.py`, `decisions.json`, `sources.json`, `filters.json`, and `title.txt` outputs offline.

Follow these offline testing patterns when adding coverage for security and OS features:

- **SSRF, throttle & `robots.txt` (`lookout/lookout_extract.py`):** Test blocked loopback/private/link-local/CGNAT/local-hostname targets against a local `http.server` (including HTTP `301`/`302`/`307`/`308` redirects onto loopback), and prove public-IP allowance, per-host throttling, User-Agent headers, and `allowed()` / `respect_robots` enforcement with mocked `socket.getaddrinfo` + local HTTP stubs rather than external network calls.
- **Webhooks & SMTP (`lookout/notify.py`):** Use local `http.server` stubs and `unittest.mock` to test JSON, Slack, Discord, Telegram, and `ntfy` payload formatting, `(ok, message)` return tuples, private-target blocking by default vs. explicit opt-in, configurable `mail_from`, multiple `mail_to` recipients, and `STARTTLS` (`auto`/`1`/`0`) + port 465 `SMTP_SSL`.
- **Per-OS scheduling (`lookout/notify.py`):** Patch `sys.platform`, `shutil.which`, `subprocess.run`, and filesystem paths (`XDG_CONFIG_HOME`, `Path.home()`) into temporary directories when testing Windows `schtasks`/PowerShell, Linux `systemd --user` unit files, macOS `launchd` plists, `crontab` install/remove paths, `PATH` + `LOOKOUT_DATA_DIR` environment capture, and `run_schedule_now()`.

## Adding an agent adapter

Agents live in `AGENTS` in [`lookout/agents.py`](lookout/agents.py). Each entry specifies the executable and a command builder that:

- passes the one-line prompt from `prompt_for(workdir)`,
- enables non-interactive execution of `agent-browser` and file reads/writes in `workdir` (if the CLI supports per-task tool restrictions, accept `task_kind` and restrict `"classify"` to file read/write only),
- passes the selected `--model` / `-m` flag when a model is set.

If the CLI supports listing models, add `"list_models": [...]`. Keep `"verified": False` until flags have been checked on a real installation (and state the tested CLI version in your pull request). Update the permission & safety tables in [README.md](README.md) and [SECURITY.md](SECURITY.md) whenever adding or changing agent flags.

Add a command-building test in `tests/test_agents.py` patching `shutil.which`, calling `agents.build_command("<your-id>", "my-model", workdir, 120)` (and `model=""`), and asserting the executable, model flag, and `agents.prompt_for(workdir)` appear in the returned command.

## Plugins

Example plugins are welcome as `_example_<name>.py` in `plugins/` (files starting with `_` are ignored by the loader). Keep examples focused on public, documented endpoints and include no credentials.

## Pull requests

- Keep each pull request focused, with a short description of what changed and why.
- Include a screenshot for UI changes.
- Verify `compileall`, `node --check web/app.js`, `ruff check .`, and `python -m unittest discover -s tests -v` all pass.
- Never commit files from `data/` or personal `plugins/`.
