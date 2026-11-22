# Contributing to Lookout

Thanks for helping. Lookout is deliberately small: plain Python standard library on the back end,
plain HTML/CSS/JS on the front end, no build step.

## Run it from source

```bash
pip install -r requirements.txt
python run.py            # desktop window
python run.py --browser  # or in your browser, handy with dev tools
```

Use a separate data folder while developing so your real data stays untouched:

```bash
LOOKOUT_DATA_DIR=/tmp/lookout-dev LOOKOUT_PORT=8790 python run.py --browser
```

## Run the tests

The test suite uses Python's built-in `unittest` framework with no network calls and no real agent CLIs:

```bash
python -m unittest discover -s tests -v
```

Before pushing, also run the bytecode check:

```bash
python -m compileall -q run.py lookout plugins tests
```

An optional Playwright smoke test lives in `tests/test_ui.py`. If `playwright` and Chromium are
not installed, that file skips automatically; otherwise it starts a local server against a temporary
database, visits every route, opens the model picker, checks for console errors, and verifies scroll
preservation during a scan refresh.

## How the test fakes work

Shared helpers live in `tests/helpers.py`:

- `TempLookoutTestCase` (`LookoutTestCase`): subclasses `unittest.TestCase` and redirects `LOOKOUT_DATA_DIR`,
  `LOOKOUT_PLUGIN_DIR`, `LOOKOUT_PORT`, `lookout.db.DB_PATH`, `lookout.scanner.PLUGIN_DIR`, and `lookout.tasks.WORK`
  to a fresh temporary directory per test while resetting global module caches (`scanner.progress`,
  `server.discovery`, `agents._model_cache`, `notify._schedule_cache`). Real user data in `data/` and port `8780`
  are never touched.
- `seed_db()`: inserts realistic sources, filters, scans, items, discoveries, settings, and agent usage rows
  into the temporary database.
- `LiveServer`: starts `lookout.server.Handler` on an ephemeral local port (`127.0.0.1:0`) inside a
  background thread and offers a `.request(method, path, body, headers)` helper for API and origin tests.
- `make_fake_agent(responses)`: returns a drop-in replacement for `lookout.agents.run_task` that records
  every call, reads `TASK.md`, writes configured files (`results.json`, `extractor.py`, `decisions.json`,
  `sources.json`, `title.txt`) into the task working folder, and optionally raises simulated errors.

## Adding an agent

Agents live in `AGENTS` in [`lookout/agents.py`](lookout/agents.py). Each entry says which executable
to look for and how to build a non-interactive command that:

- passes the one-line prompt from `prompt_for(workdir)`,
- lets the agent run `agent-browser` and read/write files in `workdir` without asking,
- uses the model id the user picked.

If the CLI can list its models, add `"list_models": [...]` with the sub-command. Mark the entry
`"verified": False` until it has been tested on a real install, and say in the pull request which
version you tested.

When adding a new agent, add a command-building test in `tests/test_agents.py`: patch `shutil.which`
to return a fake executable path, call `agents.build_command("<your-id>", "my-model", workdir, 120)`
(and also with `model=""`), and assert that the executable, model flag, and `agents.prompt_for(workdir)`
appear in the returned argument list.

## Plugins

Example plugins are welcome as `_example_<name>.py` in `plugins/` (files starting with `_` are not
loaded). Keep them to sites with public, documented or clearly public endpoints.

## Pull requests

- One change per pull request, with a short description of what and why.
- Include a screenshot for UI changes.
- Don't commit anything from `data/` or your personal plugins.
