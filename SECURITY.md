# Security Policy

Lookout automates web monitoring by pairing locally installed coding-agent CLIs with a local Python/SQLite application. This document describes Lookout's threat model, implemented defenses, known limitations, and how to report vulnerabilities privately.

## Threat model

Lookout processes untrusted external data across five trust boundaries:

1. **Untrusted web pages during Discover / Learn / Quick Read (prompt injection).** During source discovery, learning, self-healing, and unlearned source previews, the selected agent CLI visits third-party web pages using `agent-browser` (or built-in web tools). Page HTML and text enter the agent's context and may contain prompt-injection payloads attempting to hijack the agent's tools.
2. **Untrusted scraped items during Classify (second-stage prompt injection).** Scraped item fields (`title`, `org`, `category`, `location`, `summary`) are written to `items.json` for batch semantic filtering (`lookout/tasks.py:classify`). Without prompt rules and tool restrictions, malicious item text could attempt to trigger browsing, file edits, or command execution during filtering.
3. **Untrusted collected fields in exports and feeds (CSV / ICS / XML injection).** Item fields persisted in SQLite are exported to CSV (`/api/export.csv`), iCalendar (`/api/agenda.ics`), and RSS/Atom (`/api/watches/<id>/feed.xml`, `/atom.xml`). Without escaping and control-character stripping, malicious strings can trigger spreadsheet formula evaluation, iCalendar property injection, or malformed XML.
4. **Agent-generated readers (`extractor.py`).** An agent writes Python reader code that Lookout executes on future scans in [`lookout/extractors.py`](lookout/extractors.py). Without validation and isolation, generated code could attempt arbitrary filesystem reads, local shell execution, or requests to local network services (SSRF).
5. **Local HTTP API ([`lookout/server.py`](lookout/server.py)).** Lookout runs a local HTTP server (default port `8780`) for the UI. Without launch-secret authentication, cookie protections, CSP/framing headers, and removal of command configuration from HTTP, unrelated websites (via CSRF, clickjacking, or DNS rebinding) or unauthorized local processes could mutate state or execute arbitrary commands.

## Agent permissions and safety

Prompt rules in [`lookout/tasks.py`](lookout/tasks.py) (`SAFETY`) instruct agents across `LEARN`, `QUICK_READ`, `DISCOVER`, and `CLASSIFY` to treat web pages and `items.json` strictly as data, avoid logging in or submitting forms, skip CAPTCHAs, never run commands or browse during `CLASSIFY`, and write only named output files in their temporary working folder. **Prompt rules are instructions to the model, not OS-level security enforcement.**

What constrains tool execution at the CLI layer is the command-line permission mode configured per adapter in [`lookout/agents.py`](lookout/agents.py):

| Agent | Exact flags passed by Lookout | CLI `--help` checked | Permission model | Risk profile |
|---|---|---|---|---|
| **Claude Code** (`claude`) | `-p <prompt> --add-dir <workdir> --permission-mode acceptEdits --allowedTools <task-tools>` (+ `--model <model>`) | Yes | **Tool-restricted per task** | Lowest risk among built-in adapters: tools are restricted per task kind (`CLAUDE_TASK_TOOLS`), and `classify` only gets `Read` and `Write`. See residual note below. |
| **Gemini CLI** (`gemini`) | `-p <prompt> --yolo` (+ `-m <model>`) | Yes | **Auto-approve** | `--yolo` approves all tool calls without asking. |
| **Antigravity CLI** (`agy`) | `-p <prompt> --add-dir <workdir> --dangerously-skip-permissions --print-timeout <timeout>s` (+ `--model <model>`) | Yes | **Auto-approve** | `--dangerously-skip-permissions` bypasses interactive confirmation prompts. |
| **opencode** (`opencode`) | `run <prompt> --auto` (+ `-m <model>`) | Yes | **Auto-approve** | `--auto` executes tool calls non-interactively without confirmation. |
| **Codex CLI** (`codex`) | `exec <prompt> --full-auto` (+ `-m <model>`) | No (experimental) | **Auto-approve** | `--full-auto` enables unattended tool execution (flags unverified on a real install). |
| **Cursor Agent** (`cursor-agent`) | `-p <prompt> --force` (+ `--model <model>`) | No (experimental) | **Auto-approve** | `--force` skips confirmation prompts (flags unverified on a real install). |
| **Qwen Code** (`qwen`) | `-p <prompt> --yolo` (+ `-m <model>`) | No (experimental) | **Auto-approve** | `--yolo` auto-approves tool execution (flags unverified on a real install). |
| **GitHub Copilot CLI** (`copilot`) | `-p <prompt> --allow-all-tools` (+ `--model <model>`) | No (experimental) | **Auto-approve** | `--allow-all-tools` permits all tools without prompting (flags unverified on a real install). |
| **Custom command** (`custom`) | Local command template from `data/agent_command.txt` or `--custom-command` split with `shlex.split` (`{prompt}`, `{model}`, `{dir}`) | No | **User-defined (local-only)** | Not configurable over HTTP. Safety depends entirely on the binary and flags you supply locally. |

### Claude Code per-task tool sets (`CLAUDE_TASK_TOOLS`)

[`lookout/agents.py`](lookout/agents.py) passes different `--allowedTools` lists to Claude Code depending on `task_kind`:

- **`classify`:** `Read`, `Write` (no `agent-browser`, no `WebFetch`, no `WebSearch`, no `Edit`, no `Bash`).
- **`quick_read`:** `Bash(agent-browser:*)`, `Bash(agent-browser *)`, `Read`, `Write`.
- **`learn`:** `Bash(agent-browser:*)`, `Bash(agent-browser *)`, `Bash(python test_extractor.py)`, `Bash(python3 test_extractor.py)`, `Read`, `Write`, `Edit`, `WebFetch`.
- **`discover`:** `Bash(agent-browser:*)`, `Bash(agent-browser *)`, `Read`, `Write`, `Edit`, `WebSearch`, `WebFetch`.

### What auto-approve and CLI tool flags do — and do not — enforce

- **Auto-approve adapters (`--yolo`, `--dangerously-skip-permissions`, `--auto`, `--full-auto`, `--force`, `--allow-all-tools`):** Run the agent non-interactively without asking you to confirm individual file edits, shell commands, or network requests. If a monitored site contains prompt-injection text that tricks the agent, an auto-approve CLI can run commands or read/write files anywhere your OS user account has access.
- **Claude Code residual scope:** While Claude Code restricts *which* tools can run (blocking general shell commands and blocking all browser/network tools during `classify`), during `learn` and `discover` its `Read`, `Write`, `Edit`, `WebFetch`, and `Bash(agent-browser:*)` tools are not an OS-level filesystem sandbox and do not prevent a prompt-injected session from reading other user-readable files on the host (such as `~/.ssh` or `data/lookout.db`) and sending data via `WebFetch` or `agent-browser`.

**Recommendations:**

- Prefer **Claude Code** when available.
- Run Lookout inside the official rootless **[`Dockerfile`](Dockerfile) container**, a **throwaway VM**, or a **dedicated low-privilege OS user** whenever scanning untrusted public websites.
- Keep `agent-browser` and your chosen agent CLI updated.

## Local HTTP API, session authentication, and export protections

Lookout's local server ([`lookout/server.py`](lookout/server.py)) and UI apply the following safeguards:

- **Loopback bind by default:** The HTTP server binds to `127.0.0.1` (`ThreadingHTTPServer((bind_host, port), Handler)`). An optional `LOOKOUT_BIND` environment variable is supported for container bridge networking (inside [`Dockerfile`](Dockerfile), pair `LOOKOUT_BIND=0.0.0.0` with `docker run -p 127.0.0.1:8780:8780` so the port is published only on host loopback).
- **`Host`, `Origin`, `Referer`, and `Sec-Fetch-Site` validation:** `Handler.allowed()` rejects requests whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>`, rejects requests with `Sec-Fetch-Site: cross-site`, and rejects `POST` requests with an untrusted `Origin` or `Referer` header (`403`).
- **One-time launch secret exchanged for an `HttpOnly` session cookie (no token in HTML):**
  - On startup, `server.make_server()` generates a random one-time launch secret (`srv.launch_secret`) and writes it to `data/.launch_secret` (`0o600` on POSIX) so `run.py` can launch the desktop window or browser at `http://127.0.0.1:<port>/?k=<secret>`.
  - `GET /?k=<secret>` (or `/index.html?k=<secret>`) consumes and invalidates the secret on first use, immediately issues a fresh one-time secret into `data/.launch_secret` (`0o600` on POSIX) so later launches or desktop notifications on the machine can open Lookout, sets `Set-Cookie: lookout_session=<token>; HttpOnly; SameSite=Strict; Path=/`, and returns a `302` redirect to `/`. Replaying a consumed or invalid `?k=` secret returns `401`.
  - Visiting `GET /` without a valid `lookout_session` cookie (or `X-Lookout-Token` header for local programmatic tests) returns a minimal `"Open Lookout from the app"` gate page. Neither `index.html` nor the gate page embeds any API token.
  - All `/api/*` endpoints require a valid `lookout_session` cookie or `X-Lookout-Token` header (returning `401` when missing or invalid), except `GET /api/watches/<id>/(feed|atom).xml` when the user explicitly enables `rss_public` in Settings (which remains localhost-only due to loopback binding and `Host` validation).
- **`custom_command` removed from HTTP:** `POST /api/settings` rejects any payload containing `"custom_command"` with `403`. `public_settings()` and `/api/agents` never expose the raw command string over HTTP (returning only `custom_command_configured`, `custom_command_path`, and `source`). A custom command can only be configured locally in `data/agent_command.txt` or via `python run.py --custom-command "..."`. When executed, [`lookout/agents.py`](lookout/agents.py) rejects shell chaining/substitution metacharacters (`&&`, `||`, `|`, `;`, backticks `` ` ``, `$(`, newlines), splits arguments with `shlex.split`, and runs `subprocess.Popen` without `shell=True`. (Note: metacharacter validation prevents accidental shell chaining in templates; it is not a sandbox against a local user who configures an interpreter binary such as `python -c` or `cmd /c`.)
- **Content-Security-Policy and framing headers:** Every response (including `302` redirects and `401`/`403`/`404` errors) includes:
  - `Content-Security-Policy: default-src 'self'; img-src 'self' data:; frame-ancestors 'none'`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: no-referrer`
  - `X-Content-Type-Options: nosniff`
  - `Cache-Control: no-store`
  All UI styles and scripts live in static files (`web/app.css`, `web/app.js`, `web/index.html`) with no inline `style="..."` or inline scripts.
- **CSV formula neutralization:** `/api/export.csv` prefixes any cell starting with `=`, `+`, `-`, `@`, `\t`, or `\r` with `'` so spreadsheet applications do not evaluate scraped text as formulas.
- **ICS and RSS/Atom sanitization:** `/api/agenda.ics` escapes text properties (`SUMMARY`, `DESCRIPTION`), writes `URL:` with control characters stripped and without corrupting `,` or `;` in links, folds lines at 75 UTF-8 octets, and writes UTC `DTSTAMP` timestamps ending in `Z`. `/api/watches/<id>/feed.xml` and `/atom.xml` strip XML-illegal control characters (`\x00-\x08`, `\x0b`, `\x0c`, `\x0e-\x1f`, `\x7f`, `\ufffe`, `\uffff`) before XML escaping.

## SSRF guard (`lookout_extract.py` and webhooks)

Network helpers available to generated readers (`get`, `get_json`, `post_json`, `browser_eval`, and `allowed` in [`lookout/lookout_extract.py`](lookout/lookout_extract.py)) and webhook notifications (`webhook` in [`lookout/notify.py`](lookout/notify.py)) enforce `check_url()` and `_SafeRedirectHandler`:

- **Scheme & hostname restrictions:** Only `http` and `https` URLs with a valid hostname and numeric port (`1..65535`) are accepted. Zone-scoped addresses (`%`), `localhost`, `ip6-localhost`, `ip6-loopback`, `local`, `*.localhost`, and `*.local` are blocked.
- **Full DNS resolution check:** Hostnames are resolved via `socket.getaddrinfo(..., type=socket.SOCK_STREAM)`, and **every** resolved IP address (along with literal IP targets) is checked against non-public ranges:
  - Loopback (`127.0.0.0/8`, `::1`)
  - Private / RFC 1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
  - Link-local (`169.254.0.0/16`, `fe80::/10`, cloud metadata endpoints)
  - CGNAT (`100.64.0.0/10`)
  - Unspecified (`0.0.0.0`, `::`), multicast, reserved, and non-global addresses
  - IPv6 unique-local (`fc00::/7`), site-local (`fec0::/10`), deprecated IPv4-compatible (`::/96`), and embedded non-public addresses inside IPv4-mapped (`::ffff:...`), 6to4, and Teredo addresses
- **Redirect re-validation:** `_SafeRedirectHandler` re-runs `check_url()` on the resolved target of every HTTP redirect (including `307` and `308`) before following it, closing rejected connections immediately.
- **Webhooks:** `notify.webhook()` uses `check_url()` and `_SafeRedirectHandler` with private/loopback targets blocked by default unless the user explicitly enables local webhooks in Settings.

### Residual SSRF limits

Be clear about what this guard does **not** solve:

- **Not an OS or network firewall.**
- **DNS rebinding TOCTOU:** `urllib.request` (and `agent-browser`) opens the TCP socket after `check_url()` calls `socket.getaddrinfo()`. A malicious authoritative DNS server returning a public IP during validation and `127.0.0.1` milliseconds later on connect can bypass application-level DNS checks.
- **Browser subrequests:** `browser_eval()` validates the initial URL before calling `agent-browser`, which spawns Chromium. Redirects, `fetch()`/XHR calls, or subresources initiated inside Chromium after page load are handled by Chromium rather than `_SafeRedirectHandler`.
- **Agent-phase browsing:** During Discover, Learn/Heal, and Quick Read, the agent CLI invokes `agent-browser` or its own web tools directly rather than through `lookout_extract.py`.

## Reader sandbox (`extractors.py` and `lookout_extract.py`)

Agent-written readers (`extractor.py`) go through static and runtime checks in [`lookout/extractors.py`](lookout/extractors.py):

- **Single-source-of-truth allow/deny lists:** `SANDBOX_HELPERS`, `SANDBOX_MODULES`, `SANDBOX_BANNED_CALLS`, and `SANDBOX_BANNED_ATTRS` are defined once in [`lookout/lookout_extract.py`](lookout/lookout_extract.py) and imported by both the AST validator (`extractors.check`) and the runtime `RUNNER` (`_safe_import`), avoiding parse-time vs. runtime drift.
- **AST check before execution:** Permits only `fetch()` at top level, imports from `SANDBOX_MODULES` (`json`, `re`, `html`, `math`, `time`, `datetime`, `itertools`, `collections`, `typing`, `urllib.parse`, `unicodedata`, `string`, `functools`), and `SANDBOX_HELPERS` (`get`, `get_json`, `post_json`, `browser_eval`, `absolute`, `allowed`) from `lookout_extract`. Blocks class definitions, relative/star imports, `__`-prefixed identifiers, `_`-prefixed attributes, banned calls (`eval`, `exec`, `compile`, `open`, `__import__`, `getattr`, `globals`, `type`, etc.), banned attributes (frame/code/traceback/generator attributes and module attributes such as `os`, `sys`, `subprocess`, `socket`, `ctypes`), dynamic `.format()`, and unsafe `.format()` field specs.
- **Isolated subprocess & resource caps:** Executes via `python -I` (`extractors.run`) in a temporary working directory with a stripped `_SAFE_BUILTINS` dict, `_safe_import` hook, wall-clock timeout (default `180s`), process-tree termination (`taskkill /T /F` on Windows; `os.killpg(..., SIGKILL)` on POSIX), stdout/stderr size cap (`MAX_OUTPUT_BYTES` = 5 MiB), HTTP response cap (`MAX_HTTP_BYTES` = 10 MiB), HTTP timeout cap (`MAX_HTTP_TIMEOUT` = 60s), and `browser_eval` wait/scroll limits (`MAX_WAIT_MS` = 30,000 ms, `MAX_SCROLLS` = 30).
- **Non-empty verification (`agrees` & `verify_reader_output`):** `extractors.agrees()` returns `UNVERIFIED` when `agent_items` is empty, and `scanner.verify_reader_output()` requires non-empty `agent_items`, `agrees(...) is True`, field fill-rate consistency, and low duplication before promoting a reader to `last_good_extractor`.

### Residual sandbox limits

- **Defense-in-depth guard-rail, not a kernel/VM sandbox.** Python AST filtering plus restricted builtins shrinks the attack surface against accidental or naive unsafe code, but Python's object model is not a security boundary on par with OS containers or VMs. You can inspect every learned reader directly in **Sources**.
- **`browser_eval` spawns Chromium:** When a reader uses `browser_eval()`, Lookout launches `agent-browser` (Node.js + headless Chromium) as a child process to render the page and execute the reader's JavaScript expression.
- **Local plugins run unsandboxed:** Files in `plugins/*.py` are user-authored Python executed directly in Lookout's process. Only add plugins you wrote or trust.

## Data, privacy, and secrets

- **Stored locally:** Watches, sources, learned readers, filters, collected items, agenda notes, notification logs, scan/discovery logs, usage counters, and settings live in `data/lookout.db` (SQLite, WAL mode).
- **Sent off your machine:**
  - During **Discover**, **Learn / Heal**, **Quick Read** (unlearned source test), and **Filter** (semantic filter evaluation), visited web pages, extracted item metadata (`title`, `org`, `category`, `location`, `deadline`, `start_date`, `summary`), and your filter/watch prompts are passed to your selected agent CLI and sent to **that CLI's model provider** (plus any web search backend the CLI uses).
  - During normal **Read** scans with healthy readers, only requests to the monitored websites leave your machine (plus optional notifications you configure).
  - **E-mail digest** (`notify.mail_digest`) connects to your configured SMTP server using TLS certificate verification (`ssl.create_default_context()` via STARTTLS or SMTPS port 465).
  - **Webhooks** (`notify.webhook`) send item summaries to your configured webhook URL (JSON, Slack, Discord, Telegram, or ntfy).
- **Credential storage:** `smtp_password` is stored in the local `settings` table inside `data/lookout.db` (it is omitted from `/api/state` and `/api/backup.json`, which expose only `smtp_password_set`, but is not stored in an OS keyring). `data/.launch_secret` is created with `0o600` permissions on POSIX and deleted as soon as the launch URL is consumed. Protect filesystem permissions on `data/` accordingly.
- **Backups:** `/api/backup.json` exports watches, non-plugin sources, and filters only — it deliberately excludes `smtp_password`, learned readers, collected items, and scan history.

## Reporting a security issue

Please do **not** open a public GitHub issue for security vulnerabilities, sandbox escapes, or SSRF bypasses.

Report issues privately through **GitHub Security Advisories** on the repository:
`https://github.com/1eminguler/lookout/security/advisories/new`

When reporting, please include:

- A clear description of the vulnerability and affected file(s) or endpoint(s).
- Steps to reproduce or a minimal proof of concept.
- Which OS, Python version, and agent CLI (if relevant) were involved.
