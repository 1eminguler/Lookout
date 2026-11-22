# Security Policy

Lookout automates web monitoring by pairing locally installed coding-agent CLIs with a local Python/SQLite desktop application. This document describes Lookout's threat model, implemented defenses, known limitations, and how to report vulnerabilities privately.

## Threat model

Lookout processes untrusted external data across four trust boundaries:

1. **Untrusted web pages (prompt injection).** During source discovery, learning, and self-healing, the selected agent CLI visits third-party web pages using `agent-browser` (or built-in web tools). Page HTML and text enter the agent's context and may contain prompt-injection payloads attempting to hijack the agent's tools.
2. **Untrusted collected fields (export injection).** Item fields parsed from external websites (`title`, `org`, `category`, `location`, `summary`, `url`) are persisted in SQLite and exported to CSV (`/api/export.csv`) and iCalendar (`/api/agenda.ics`). Without escaping, malicious strings can trigger spreadsheet formula execution or iCalendar line/property injection.
3. **Agent-generated readers (`extractor.py`).** An agent writes Python reader code that Lookout executes on future scans in [`lookout/extractors.py`](lookout/extractors.py). Without validation and isolation, generated code could attempt arbitrary filesystem reads, local shell execution, or requests to local network services (SSRF).
4. **Local HTTP API ([`lookout/server.py`](lookout/server.py)).** Lookout runs a local HTTP server (default port `8780`) for the UI. Without origin, host, and token protections, unrelated websites (via CSRF or DNS rebinding) or other processes on the same machine could trigger scans, modify settings, or reconfigure the `custom_command` runner.

## Agent permissions and safety

Prompt rules in [`lookout/tasks.py`](lookout/tasks.py) (`SAFETY`) instruct agents to treat page content strictly as data, avoid logging in or submitting forms, skip CAPTCHAs, and write only named output files in their temporary working folder. **Prompt rules are instructions to the model, not OS-level security enforcement.**

What actually constrains tool execution is the command-line permission mode configured per adapter in [`lookout/agents.py`](lookout/agents.py):

| Agent | Exact flags passed by Lookout | CLI verified | Permission model | Risk profile |
|---|---|---|---|---|
| **Claude Code** (`claude`) | `-p <prompt> --add-dir <workdir> --permission-mode acceptEdits --allowedTools "Bash(agent-browser:*)" "Bash(agent-browser *)" "Bash(python test_extractor.py)" "Bash(python3 test_extractor.py)" Read Write Edit WebSearch WebFetch` (+ `--model <model>`) | Yes | **Tool-restricted** | Lowest risk: only `agent-browser`, `test_extractor.py`, scoped file tools, and web search/fetch are allowed; no general shell access. |
| **Gemini CLI** (`gemini`) | `-p <prompt> --yolo` (+ `-m <model>`) | Yes | **Auto-approve** | `--yolo` approves all tool calls without asking. |
| **Antigravity CLI** (`agy`) | `-p <prompt> --add-dir <workdir> --dangerously-skip-permissions --print-timeout <timeout>s` (+ `--model <model>`) | Yes | **Auto-approve** | `--dangerously-skip-permissions` bypasses interactive confirmation prompts. |
| **opencode** (`opencode`) | `run <prompt> --auto` (+ `-m <model>`) | Yes | **Auto-approve** | `--auto` executes tool calls non-interactively without confirmation. |
| **Codex CLI** (`codex`) | `exec <prompt> --full-auto` (+ `-m <model>`) | No (experimental) | **Auto-approve** | `--full-auto` enables unattended tool execution (exact scope depends on Codex's sandbox mode; flags unverified on a real install). |
| **Cursor Agent** (`cursor-agent`) | `-p <prompt> --force` (+ `--model <model>`) | No (experimental) | **Auto-approve** | `--force` skips confirmation prompts (flags unverified on a real install). |
| **Qwen Code** (`qwen`) | `-p <prompt> --yolo` (+ `-m <model>`) | No (experimental) | **Auto-approve** | `--yolo` auto-approves tool execution (flags unverified on a real install). |
| **GitHub Copilot CLI** (`copilot`) | `-p <prompt> --allow-all-tools` (+ `--model <model>`) | No (experimental) | **Auto-approve** | `--allow-all-tools` permits all tools without prompting (flags unverified on a real install). |
| **Custom command** (`custom`) | User-configured command split with `shlex.split` (`{prompt}`, `{model}`, `{dir}`) | No | **User-defined** | Safety depends entirely on the binary and flags you supply. |

### What auto-approve means

Auto-approve flags (`--yolo`, `--dangerously-skip-permissions`, `--auto`, `--full-auto`, `--force`, `--allow-all-tools`) run the agent non-interactively without asking you to confirm individual file edits, shell commands, or network requests. If a monitored site contains prompt-injection text that tricks the agent, an auto-approve CLI can run commands or read/write files anywhere your OS user account has access.

**Recommendations:**

- Prefer **Claude Code** when available, because it is the only built-in adapter restricted at the CLI layer (`--allowedTools` and `--permission-mode acceptEdits`).
- Run Lookout inside a **container**, **throwaway VM**, or **dedicated low-privilege OS user** whenever scanning untrusted or arbitrary public websites—especially with any auto-approve agent or custom command.
- Keep `agent-browser` and your chosen agent CLI updated.

## Local HTTP API and export protections

Lookout's local server ([`lookout/server.py`](lookout/server.py)) and UI apply several safeguards:

- **Loopback bind only:** The HTTP server binds exclusively to `127.0.0.1` (`ThreadingHTTPServer(("127.0.0.1", port), Handler)`), never `0.0.0.0`.
- **`Host` and `Origin` validation:** `Handler.allowed()` rejects requests whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>`, and rejects `POST` requests with a mismatched `Origin` header (`403`), defending against cross-site request forgery and DNS rebinding from browser tabs.
- **Per-launch API token:** The server generates a random token on startup (`srv.api_token`), embeds it into `<meta name="lookout-token">` in served `index.html`, and requires it in the `X-Lookout-Token` header on `/api/*` requests (returning `401` when missing or invalid). This prevents unauthorized local processes or scripts that omit `Origin` from mutating state or triggering scans. Static UI files remain readable so the app can load, and CLI commands (`python run.py scan`) operate directly against SQLite without HTTP. Optional per-watch RSS/Atom endpoints (`/api/watches/<id>/feed.xml` and `/atom.xml`) are disabled for unauthenticated access by default and only exposed without the token if `rss_public` is explicitly enabled in Settings.
- **Hardened `custom_command` updates:** Updating `custom_command` over HTTP requires both the valid API token and an explicit confirmation flag sent by the Settings form, and rejects shell chaining or command-substitution metacharacters (`&&`, `|`, `;`, backticks `` ` ``, `$(`). When executing custom commands, [`lookout/agents.py`](lookout/agents.py) splits arguments with `shlex.split` and invokes `subprocess.Popen` without `shell=True`.
- **CSV formula neutralization:** `/api/export.csv` prefixes any cell starting with `=`, `+`, `-`, `@`, `\t`, or `\r` with `'` so spreadsheet applications do not evaluate scraped text as formulas.
- **ICS escaping:** `/api/agenda.ics` escapes `\r` alongside `\`, `;`, `,`, and `\n`, folds long lines, and writes UTC `DTSTAMP` timestamps ending in `Z`.

## SSRF guard (`lookout_extract.py` and webhooks)

Network helpers available to generated readers (`get`, `get_json`, `post_json`, and `browser_eval` in [`lookout/lookout_extract.py`](lookout/lookout_extract.py)) and webhook notifications (`webhook` in [`lookout/notify.py`](lookout/notify.py)) enforce `check_url()` and `_SafeRedirectHandler`:

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
- **Agent-phase browsing:** During Discover and Learn/Heal, the agent CLI invokes `agent-browser` or its own web tools directly rather than through `lookout_extract.py`.

## Reader sandbox (`extractors.py` and `lookout_extract.py`)

Agent-written readers (`extractor.py`) go through static and runtime checks in [`lookout/extractors.py`](lookout/extractors.py):

- **Single-source-of-truth allow/deny lists:** `SANDBOX_HELPERS`, `SANDBOX_MODULES`, `SANDBOX_BANNED_CALLS`, and `SANDBOX_BANNED_ATTRS` are defined once in [`lookout/lookout_extract.py`](lookout/lookout_extract.py) and imported by both the AST validator (`extractors.check`) and the runtime `RUNNER` (`_safe_import`), avoiding parse-time vs. runtime drift.
- **AST check before execution:** Permits only `fetch()` at top level, imports from `SANDBOX_MODULES` (`json`, `re`, `html`, `math`, `time`, `datetime`, `itertools`, `collections`, `typing`, `urllib.parse`, `unicodedata`, `string`, `functools`), and `SANDBOX_HELPERS` (`get`, `get_json`, `post_json`, `browser_eval`, `absolute`) from `lookout_extract`. Blocks class definitions, relative/star imports, `__`-prefixed identifiers, `_`-prefixed attributes, banned calls (`eval`, `exec`, `compile`, `open`, `__import__`, `getattr`, `globals`, `type`, etc.), banned attributes (frame/code/traceback/generator attributes and module attributes such as `os`, `sys`, `subprocess`, `socket`, `ctypes`), dynamic `.format()`, and unsafe `.format()` field specs.
- **Isolated subprocess & resource caps:** Executes via `python -I` (`extractors.run`) in a temporary working directory with a stripped `_SAFE_BUILTINS` dict, `_safe_import` hook, wall-clock timeout (default `180s`), process-tree termination (`taskkill /T /F` on Windows; `os.killpg(..., SIGKILL)` on POSIX), stdout/stderr size cap (`MAX_OUTPUT_BYTES` = 5 MiB), HTTP response cap (`MAX_HTTP_BYTES` = 10 MiB), HTTP timeout cap (`MAX_HTTP_TIMEOUT` = 60s), and `browser_eval` wait/scroll limits (`MAX_WAIT_MS` = 30,000 ms, `MAX_SCROLLS` = 30).

### Residual sandbox limits

- **Defense-in-depth guard-rail, not a kernel/VM sandbox.** Python AST filtering plus restricted builtins shrinks the attack surface against accidental or naive unsafe code, but Python's object model is not a security boundary on par with OS containers or VMs. You can inspect every learned reader directly in **Sources**.
- **`browser_eval` spawns Chromium:** When a reader uses `browser_eval()`, Lookout launches `agent-browser` (Node.js + headless Chromium) as a child process to render the page and execute the reader's JavaScript expression.
- **Local plugins run unsandboxed:** Files in `plugins/*.py` are user-authored Python executed directly in Lookout's process. Only add plugins you wrote or trust.

## Data, privacy, and secrets

- **Stored locally:** Watches, sources, learned readers, filters, collected items, agenda notes, scan/discovery logs, usage counters, and settings live in `data/lookout.db` (SQLite).
- **Sent off your machine:**
  - During **Discover**, **Learn / Heal**, and **Filter** (semantic filter evaluation), visited web pages, extracted item metadata (`title`, `org`, `category`, `location`, `deadline`, `start_date`, `summary`), and your filter/watch prompts are passed to your selected agent CLI and sent to **that CLI's model provider** (plus any web search backend the CLI uses).
  - During normal **Read** scans with healthy readers, only requests to the monitored websites leave your machine (plus optional notifications you configure).
  - **E-mail digest** (`notify.mail_digest`) connects to your configured SMTP server using TLS certificate verification (`ssl.create_default_context()` via STARTTLS or SMTPS port 465).
  - **Webhooks** (`notify.webhook`) send item summaries to your configured webhook/ntfy URL.
- **Credential storage:** `smtp_password` is stored in the local `settings` table inside `data/lookout.db` (it is omitted from `/api/state` and `/api/backup.json`, which expose only `smtp_password_set`, but is not stored in an OS keyring). Protect filesystem permissions on `data/` accordingly.
- **Backups:** `/api/backup.json` exports watches, non-plugin sources, and filters only — it deliberately excludes `smtp_password`, learned readers, collected items, and scan history.

## Reporting a security issue

Please do **not** open a public GitHub issue for security vulnerabilities, sandbox escapes, or SSRF bypasses.

Report issues privately through **GitHub Security Advisories** on the repository:
`https://github.com/1eminguler/lookout/security/advisories/new`

When reporting, please include:

- A clear description of the vulnerability and affected file(s) or endpoint(s).
- Steps to reproduce or a minimal proof of concept.
- Which OS, Python version, and agent CLI (if relevant) were involved.
