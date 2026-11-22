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

## Adding an agent

Agents live in `AGENTS` in [`lookout/agents.py`](lookout/agents.py). Each entry says which executable
to look for and how to build a non-interactive command that:

- passes the one-line prompt from `prompt_for(workdir)`,
- lets the agent run `agent-browser` and read/write files in `workdir` without asking,
- uses the model id the user picked.

If the CLI can list its models, add `"list_models": [...]` with the sub-command. Mark the entry
`"verified": False` until it has been tested on a real install, and say in the pull request which
version you tested.

## Plugins

Example plugins are welcome as `_example_<name>.py` in `plugins/` (files starting with `_` are not
loaded). Keep them to sites with public, documented or clearly public endpoints.

## Pull requests

- One change per pull request, with a short description of what and why.
- Include a screenshot for UI changes.
- Don't commit anything from `data/` or your personal plugins.
