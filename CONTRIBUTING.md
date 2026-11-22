# Contributing to Lookout

Thanks for helping improve Lookout. The codebase is intentionally small: Python standard library on the back end, plain HTML/CSS/JavaScript on the front end (no build step), and an offline `unittest` suite.

Please also read [SECURITY.md](SECURITY.md) before modifying agent permissions, the reader sandbox, SSRF checks, or local API authentication.

## Run from source

```bash
pip install -r requirements.txt
python run.py            # desktop window (falls back to browser if pywebview cannot start)
python run.py --browser  # open directly in your web browser, handy with dev tools
```

Use a separate data folder and port while developing so your real `data/lookout.db` stays untouched:

```bash
LOOKOUT_DATA_DIR=/tmp/lookout-dev LOOKOUT_PORT=8790 python run.py --browser
```

## Run the checks

Run the same checks as CI ([`.github/workflows/check.yml`](.github/workflows/check.yml)) before opening a pull request:

```bash
python -m compileall -q run.py lookout plugins tests
node --check web/app.js
python -m unittest discover -s tests -v
```

- All unit and integration tests must remain fast and completely offline (no live internet requests, no real agent CLIs, and no writes to `data/` or port `8780`).
- An optional Playwright UI smoke test lives in `tests/test_ui.py`; it skips automatically if `playwright` and Chromium are not installed, and runs against a temporary database on an ephemeral port when present.

## Architecture & security invariants

### Single source of truth for reader sandbox constants

The reader allow/deny constants live in **one** place in [`lookout/lookout_extract.py`](lookout/lookout_extract.py):

- `SANDBOX_HELPERS`
- `SANDBOX_MODULES`
- `SANDBOX_BANNED_CALLS`
- `SANDBOX_BANNED_ATTRS`

Both the AST validator (`extractors.check`) and the runtime `RUNNER` (`_safe_import`) in [`lookout/extractors.py`](lookout/extractors.py) import those exact constants. Never duplicate allow-lists or deny-lists as raw strings in `RUNNER` or tests — update [`lookout/lookout_extract.py`](lookout/lookout_extract.py) and add regression tests proving parse-time and runtime checks stay identical.

### Test patterns (`tests/helpers.py`)

Shared fixtures live in [`tests/helpers.py`](tests/helpers.py):

- `TempLookoutTestCase` (`LookoutTestCase`): redirects `LOOKOUT_DATA_DIR`, `LOOKOUT_PLUGIN_DIR`, `LOOKOUT_PORT`, `lookout.db.DB_PATH`, `lookout.scanner.PLUGIN_DIR`, and `lookout.tasks.WORK` to a temporary directory per test and resets module caches.
- `seed_db()`: populates realistic watches, sources, filters, scans, items, discoveries, settings, and agent usage rows.
- `LiveServer`: starts `lookout.server.Handler` on `127.0.0.1:0` in a background thread. When testing `/api/*` endpoints protected by the server's per-launch API token, obtain the token injected into `/` (`index.html`) (or via `LiveServer`'s token support) for authenticated calls, and include explicit negative tests verifying that unauthenticated `/api/*` requests receive `401` and bad `Host`/`Origin` headers receive `403`.
- `make_fake_agent(responses)`: replaces `lookout.agents.run_task` to read `TASK.md` and write mock `results.json`, `extractor.py`, `decisions.json`, `sources.json`, and `title.txt` outputs offline.

Follow these offline testing patterns when adding coverage for security and OS features:

- **SSRF & redirect checks (`lookout/lookout_extract.py`):** Test blocked loopback/private/link-local/CGNAT/local-hostname targets against an actual local `http.server` (including HTTP `301`/`302`/`307`/`308` redirects onto loopback), and prove public-IP allowance with mocked `socket.getaddrinfo` + mocked HTTP responses rather than external network calls.
- **Webhooks & SMTP (`lookout/notify.py`):** Use local `http.server` stubs and `unittest.mock` to test JSON POST payloads, `ntfy` header/body formatting, private-target blocking by default vs. explicit opt-in, and TLS context usage (`ssl.create_default_context()` / port 465 `SMTP_SSL`).
- **Per-OS scheduling (`lookout/notify.py`):** Patch `sys.platform`, `shutil.which`, `subprocess.run`, and filesystem paths (`XDG_CONFIG_HOME`, `Path.home()`) into temporary directories when testing Windows `schtasks`/PowerShell, Linux `systemd --user` unit files, macOS `launchd` plists, and `crontab` install/remove paths.

## Adding an agent adapter

Agents live in `AGENTS` in [`lookout/agents.py`](lookout/agents.py). Each entry specifies the executable and a command builder that:

- passes the one-line prompt from `prompt_for(workdir)`,
- enables non-interactive execution of `agent-browser` and file reads/writes in `workdir`,
- passes the selected `--model` / `-m` flag when a model is set.

If the CLI supports listing models, add `"list_models": [...]`. Keep `"verified": False` until flags have been checked on a real installation (and state the tested CLI version in your pull request). Update the permission & safety tables in [README.md](README.md) and [SECURITY.md](SECURITY.md) whenever adding or changing agent flags.

Add a command-building test in `tests/test_agents.py` patching `shutil.which`, calling `agents.build_command("<your-id>", "my-model", workdir, 120)` (and `model=""`), and asserting the executable, model flag, and `agents.prompt_for(workdir)` appear in the returned command.

## Plugins

Example plugins are welcome as `_example_<name>.py` in `plugins/` (files starting with `_` are ignored by the loader). Keep examples focused on public, documented endpoints and include no credentials.

## Pull requests

- Keep each pull request focused, with a short description of what changed and why.
- Include a screenshot for UI changes.
- Verify `compileall`, `node --check web/app.js`, and `python -m unittest discover -s tests -v` all pass.
- Never commit files from `data/` or personal `plugins/`.
