# browser-use-local

A local FastAPI wrapper around the open-source [Browser Use](https://github.com/browser-use/browser-use)
(BU) agent, using a Patchright-controlled Chrome instance for stealth browser
automation. The `model` field in each request routes to one of several LLM
providers (Google Gemini, DeepSeek, Anthropic Claude). It mirrors the
interface of the BU Cloud SDK (`client.run(task, { model })`) so a TypeScript
caller can swap between cloud and local BU with minimal changes.

## How it works

```
Caller (e.g. JAT)
      │  POST /run { task, model? }
      ▼
FastAPI (main.py)
      │  acquire session_semaphore  (MAX_CONCURRENT_SESSIONS)
      ▼
PatchrightBrowser (browser.py)
      │  launches Chrome with --remote-debugging-port
      ▼
run_task (agent.py)
      │  make_llm(model) routes to the right provider, BU Agent
      │  connects to Chrome via CDP and drives it with that LLM
      ▼
TaskResult (output, steps, status, usage) ──▶ record_session() (db.py)
      ▼
returns { output, session_id } ──▶ browser closed ──▶ semaphore released
```

- **`config.py`** — loads and validates all env vars from `.env` at startup.
- **`browser.py`** — `PatchrightBrowser` launches a fresh, persistent Patchright
  Chromium instance per request with `--remote-debugging-port`, and closes it
  when the request finishes. One browser per request avoids state leaking
  between tasks.
- **`agent.py`** — routes a `model` string to the matching BU LLM class (see
  "Model routing" below) and runs a BU `Agent` against the given task,
  connected to the Patchright browser over its CDP URL (not direct context
  injection — see "Why CDP" below). Returns a `TaskResult` with the output,
  a readable + JSON step breakdown, status, duration, and token/cost usage.
- **`db.py`** — SQLite persistence for completed sessions (see
  "Session storage" below).
- **`main.py`** — FastAPI app. Holds the `asyncio.Semaphore` that caps how
  many Chrome instances can run at once (`MAX_CONCURRENT_SESSIONS`), and
  exposes `/health` and `/run`.

### Model routing

`agent.py` splits the `model` string on `-` and uses the first segment as a
provider prefix to pick the BU LLM class:

| Prefix | Provider | BU class | Example |
|---|---|---|---|
| `gemini` | Google | `ChatGoogle` | `gemini-3.6-flash` |
| `deepseek` | DeepSeek | `ChatDeepSeek` | `deepseek-chat` |
| `claude` | Anthropic | `ChatAnthropic` | `claude-haiku-4-5` |

If `model` is omitted from the request, it falls back to `DEFAULT_MODEL` from
`.env`. An unrecognised prefix returns `400` rather than falling through to a
provider call. Only the API key for whichever provider(s) you actually use
needs to be set — an unset key just means that provider's requests fail at
the LLM call, not at startup.

To add a new provider, add a factory function and one entry to
`PROVIDER_MAP` in `agent.py` — no other files need to change.

### Session storage

Every `/run` call that reaches the browser — whether it succeeds, times out,
or hits an unexpected error — is recorded as a row in a local SQLite database
(`DB_PATH`, default `./data/sessions.db`), created automatically on startup.
Requests rejected before the browser launches (an unrecognised `model`
prefix, HTTP 400) are **not** recorded, since nothing actually ran.

**`sessions` table columns:**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER | Primary key, also returned as `session_id` in the API response |
| `task` | TEXT | Full task/prompt string |
| `summary` | TEXT | First `SUMMARY_MAX_LENGTH` chars of `task` |
| `model` | TEXT | Resolved model string actually used |
| `steps_readable` | TEXT | Human-readable, one paragraph per agent step |
| `steps_json` | TEXT | JSON array of step objects (eval/memory/next_goal/actions/results/duration) |
| `output` | TEXT, nullable | `final_result()` from the agent; `NULL` if none produced |
| `status` | TEXT | `success` \| `incomplete` \| `failed` \| `timeout` \| `error` |
| `error_message` | TEXT, nullable | Exception message; only set when `status = "error"` |
| `total_tokens` | INTEGER | Aggregate token usage for the whole task (from BU's `history.usage`) |
| `total_cost` | REAL | Aggregate USD cost for the whole task; `0` if BU has no pricing data for the model |
| `started_at` / `completed_at` | TEXT | ISO 8601 UTC timestamps |
| `duration_ms` | INTEGER | Wall-clock duration |

**Status meanings:** `success` and `failed` come from BU's own judgment of
whether the agent's final "done" call reported success. `incomplete` means
the agent never called "done" at all (e.g. it got stuck or hit its internal
step limit) — distinct from an explicit failure. `timeout` means the
`TASK_TIMEOUT_SECONDS` wall-clock limit was hit. `error` means an unhandled
exception occurred (browser crash, an LLM error BU didn't retry past, etc.).

Note: per-step token counts aren't available from BU's step metadata in the
pinned `browser-use` version — only the aggregate `total_tokens`/`total_cost`
per session is tracked.

**Reading a session directly:**

```bash
uv run python -c "
from db import get_session
s = get_session(1)
print(s['status'], s['output'])
print(s['steps_readable'])
"
```

`db.py`'s SQLite connections are opened per call and run off the event loop
via `asyncio.to_thread` in `main.py`, so a DB write never blocks other
concurrent requests.

### Two levels of concurrency control

- **Caller-side** (e.g. JAT's `p-limit`) controls how many *pipelines* run in
  parallel — each pipeline may call `/run` multiple times in sequence.
- **This service's `asyncio.Semaphore`** controls how many *Chrome instances*
  exist at once, to protect local RAM (~300-500MB per Chrome instance).
  Requests beyond the cap are queued, not rejected — every request eventually
  runs.

### Why CDP instead of direct browser injection

Passing a Patchright `BrowserContext` directly to BU's `BrowserSession` raises
a Pydantic validation error, because BU validates that `browser_context` is
specifically a Playwright instance. Instead, Patchright Chrome is launched as
a subprocess with a remote debugging port, and BU connects to it as a
standard CDP endpoint (`cdp_url=http://localhost:{port}`) — BU has no idea
Patchright is involved, so all of Patchright's stealth patches still apply
transparently.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Google Chrome installed.

```bash
# Install dependencies into .venv
uv sync

# Install the Patchright-managed Chrome binary (one-time)
uv run python -m patchright install chromium
```

Put API keys for whichever provider(s) you plan to use into `.env`:

```dotenv
DEFAULT_MODEL=gemini-3.6-flash        # used when /run is called without a model field
GOOGLE_API_KEY=your-google-key
DEEPSEEK_API_KEY=your-deepseek-key
ANTHROPIC_API_KEY=your-anthropic-key  # optional — only needed for claude-* models
```

See [Environment variables](#environment-variables) below for the rest of the
config knobs.

## Running

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

- Health check: `http://localhost:8000/health`
- Interactive API docs (Swagger UI): `http://localhost:8000/docs`

## API endpoints

### `GET /health`

Liveness check.

```bash
curl http://localhost:8000/health
```

```json
{ "status": "ok" }
```

### `POST /run`

Runs a BU agent task to completion and returns its result. Mirrors the BU
Cloud SDK's `client.run(task, { model })`. This is a **blocking** endpoint —
it awaits the full task before responding, so no polling or callbacks are
needed. It queues behind the concurrency semaphore if all Chrome slots are
busy.

**Request body**

| Field   | Type   | Required | Notes |
|---|---|---|---|
| `task`  | string | yes | The task/prompt for the BU agent to execute. |
| `model` | string | no  | Provider-prefixed model string (see [Model routing](#model-routing)). Falls back to `DEFAULT_MODEL` when omitted. |

```bash
# Uses DEFAULT_MODEL
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task": "Go to https://example.com and return the page title."}'

# Explicit provider
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task": "Go to https://example.com and return the page title.", "model": "deepseek-chat"}'
```

**Success response** — `200 OK`

```json
{ "output": "Example Domain", "error": null, "session_id": 1 }
```

`session_id` references the row in the sessions DB — retrieve it with
`GET /session/{id}` (below) or see [Session storage](#session-storage) for
the full schema.

**Bad model prefix** — `400` if `model`'s prefix doesn't match a registered
provider (`gemini`, `deepseek`, `claude`). Not recorded in the sessions DB:

```json
{ "detail": "Unsupported provider prefix 'grok' in model string 'grok-2-mini'. Supported prefixes: ['gemini', 'deepseek', 'claude']" }
```

**Timeout** — `504` if the task exceeds `TASK_TIMEOUT_SECONDS`. Recorded with
`status = "timeout"`.

**Failure** — `500` with `detail` describing the error (e.g. browser launch
failure, invalid LLM API key, unhandled agent exception). Recorded with
`status = "error"` and the exception message in `error_message`.

### `GET /session/{session_id}`

Retrieves a stored session record by its ID (the `session_id` returned by
`/run`). See [Session storage](#session-storage) for the full column
reference.

```bash
curl http://localhost:8000/session/1
```

**Success response** — `200 OK` — the full session row, with `steps_json`
parsed back into a JSON array:

```json
{
  "id": 1,
  "task": "Go to https://example.com and return the page title.",
  "summary": "Go to https://example.com and return the page title.",
  "model": "gemini-3.6-flash",
  "steps_readable": "Step 0\n  Eval: ...",
  "steps_json": [ { "step": 0, "eval": "Start", "...": "..." } ],
  "output": "The page title of https://example.com is 'Example Domain'.",
  "status": "success",
  "error_message": null,
  "total_tokens": 10317,
  "total_cost": 0.0,
  "started_at": "2026-08-16T08:19:03.197902+00:00",
  "completed_at": "2026-08-16T08:19:15.977192+00:00",
  "duration_ms": 9400
}
```

**Not found** — `404` if no session with that ID exists:

```json
{ "detail": "Session not found" }
```

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DEFAULT_MODEL` | | `gemini-3.6-flash` | Model used when `/run` is called without a `model` field |
| `GOOGLE_API_KEY` | for `gemini-*` | `""` | Passed to `ChatGoogle` |
| `DEEPSEEK_API_KEY` | for `deepseek-*` | `""` | Passed to `ChatDeepSeek` |
| `ANTHROPIC_API_KEY` | for `claude-*` | `""` | Passed to `ChatAnthropic` |
| `BROWSER_HEADLESS` | | `true` | Headless mode toggle |
| `BROWSER_DEBUG_PORT` | | `9222` | Patchright CDP port |
| `BROWSER_USER_DATA_DIR` | | `./chrome-profile` | Chrome profile dir (persists cookies/state) |
| `PORT` | | `8000` | FastAPI server port |
| `HOST` | | `0.0.0.0` | FastAPI server host |
| `MAX_CONCURRENT_SESSIONS` | | `2` | Chrome instance cap (queues excess requests) |
| `TASK_TIMEOUT_SECONDS` | | `300` | Per-task timeout, in seconds |
| `DB_PATH` | | `./data/sessions.db` | SQLite file path (parent dir created automatically) |
| `SUMMARY_MAX_LENGTH` | | `150` | Max chars for the auto-truncated task summary stored per session |

## Known issues

- **Port already in use** — if a previous run crashed without closing Chrome,
  `BROWSER_DEBUG_PORT` (default `9222`) may still be occupied. Kill the
  hanging Chrome process, or use a different port.
- **Chrome channel not found** — `browser.py` uses `channel="chrome"`, which
  requires Google Chrome (not just Chromium) to be installed. If missing,
  either install Chrome or remove `channel="chrome"` to fall back to the
  Patchright-managed Chromium (slightly less stealth).
- **`final_result()` returns `None`** — some tasks end without a final
  structured output; `output` is `null` in the API response and in the
  `output` column for that session.
- **`total_cost` is `0`** — BU only computes cost when it has pricing data
  for the given model; for models it doesn't recognize (e.g. very new
  releases), `total_tokens` is still populated but `total_cost` stays `0`.
- **SQLite under concurrent writes** — `MAX_CONCURRENT_SESSIONS` browsers can
  finish and write around the same time. SQLite serializes writers
  automatically; at the default cap of 2 this is a non-issue, but a much
  higher `MAX_CONCURRENT_SESSIONS` could occasionally see a `database is
  locked` error on write.
- **BU API drift** — `browser-use`'s API evolves quickly. This project pins
  it via `uv.lock`; check the BU changelog before upgrading if `Agent`,
  `BrowserSession`, or the LLM wrapper interface (`browser_use.llm.*`)
  changes shape. `ChatAnthropic` in particular lives at
  `browser_use.llm.anthropic.chat.ChatAnthropic` in the pinned version —
  re-verify this path after any `browser-use` upgrade. Per-step token counts
  (`StepMetadata.input_tokens`) also don't exist in the pinned version — only
  aggregate usage (`history.usage`) is available.

---

Authenticate Genini

```bash
bash <(curl -sSL \
https://storage.googleapis.com/cloud-samples-data/adc/setup_adc.sh)
```