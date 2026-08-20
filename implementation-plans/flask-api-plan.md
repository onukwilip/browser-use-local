# Local BU Wrapper — Detailed Implementation Plan: Points 1 & 2

> **For Claude Code.** Implement exactly in the order described.
> Each section includes the why, the what, and the exact commands/code shapes.
> Point 1 must be fully working before starting Point 2.

---

## OS: macOS Tahoe (confirmed)

All commands and paths in this plan are written for **macOS Tahoe**.

**Chrome binary:** On macOS, `channel="chrome"` in `browser.py` automatically
resolves to `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
No path needs to be specified — Patchright finds it by convention. Just ensure
**Google Chrome** is installed (not just Chromium). If Chrome is missing,
`channel="chrome"` will raise a `BrowserNotFound` error at runtime. Install
Chrome first if that happens.

No other macOS-specific steps are required. All `uv`, `patchright`, and
`uvicorn` commands work identically on macOS Tahoe.

---

## Endpoint style: blocking (confirmed)

The `/run` endpoint is **blocking** — it `await`s the BU task to full
completion before returning `{ output }` to the caller. The TypeScript adapter
in JAT waits for the HTTP response, then continues. This is the right choice
for a local internal tool. No polling, no job IDs, no callbacks.

---

## Concurrency architecture: two levels, two different jobs

There are two independent concurrency controls in the overall system. They are
NOT redundant — they protect different resources.

**Level 1 — JAT's `p-limit` (TypeScript)**
Controls how many *company pipelines* run in parallel end-to-end. One pipeline
= research → email generation → contact finding in sequence. `CONCURRENCY=3`
means 3 companies being processed simultaneously. `p-limit` decides when to
*start* a new pipeline.

**Level 2 — BU local API's `asyncio.Semaphore` (Python)**
Controls how many *Chrome browser instances* exist at the same time on your
machine. One Chrome instance ≈ 300-500MB RAM. This protects your Mac from
memory exhaustion. The semaphore doesn't reject requests — it **queues** them.
When all slots are occupied, new requests wait at `async with session_semaphore:`
until a slot frees up. Every request eventually runs; none are dropped.

In practice: if JAT sends 3 concurrent `/run` requests and
`MAX_CONCURRENT_SESSIONS=2`, two browsers launch immediately and the third
request waits in the Python event loop until one finishes. JAT's HTTP client
just waits for the response, unaware of the queue.

**Recommended value for macOS Tahoe:** `MAX_CONCURRENT_SESSIONS=2`.
Two simultaneous Chrome instances is comfortable. Three starts to feel heavy,
especially if other apps are running.

### Is `p-limit` a semaphore?

Yes. Both implement a **counting semaphore** — the same classic concurrency
primitive. The counter starts at N, decrements when a task starts, increments
when it finishes, and blocks new tasks when it hits zero. The only difference
is the runtime:

| | `p-limit` | `asyncio.Semaphore` |
|---|---|---|
| Language | TypeScript/JavaScript | Python |
| Wraps | Promise executions | async coroutine executions |
| API style | Function wrapper | `async with` context manager |
| Concept | Counting semaphore | Counting semaphore |

`p-limit(3)` and `asyncio.Semaphore(3)` do the same thing in their respective
runtimes.

---

## Point 1 — FastAPI + Patchright Setup

### What this point does

Creates the Python project skeleton: package manager, dependencies, environment
config, a running FastAPI + Uvicorn server, a health-check endpoint, and a
verified Patchright Chromium binary. Nothing BU-specific lives here — this is
pure infrastructure.

---

### Step 1 — Create the project with `uv`

`uv` is the package manager recommended by FastAPI's own docs. It replaces
`pip` + `venv` and is significantly faster.

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create the project directory inside the job-application-tool repo
# (or as a sibling directory — wherever makes sense for your layout)
mkdir bu-local && cd bu-local

# Initialise a uv project (creates pyproject.toml + .venv)
uv init
```

---

### Step 2 — Install dependencies

```bash
uv add "fastapi[standard]" \
       browser-use \
       patchright \
       python-dotenv \
       langchain-openai
```

**What each dependency does:**

| Package | Purpose |
|---|---|
| `fastapi[standard]` | FastAPI framework + Uvicorn + Pydantic (all in one) |
| `browser-use` | Open-source BU agent library |
| `patchright` | Patched Playwright fork for stealth Chrome automation |
| `python-dotenv` | Load `.env` into `os.environ` |
| `langchain-openai` | `ChatOpenAI` — used to connect to DeepSeek's OpenAI-compatible API |

---

### Step 3 — Install the Patchright browser binary

```bash
# Downloads and installs the patched Chromium binary
python -m patchright install chromium

# Verify installation
python -c "from patchright.async_api import async_playwright; print('Patchright OK')"
```

> **Why Patchright instead of plain Playwright?**
> Standard Playwright/Playwright-stealth inject JavaScript to mask bot signals.
> Patchright patches Chrome at the CDP (Chrome DevTools Protocol) level —
> below JS — so the patches are invisible to fingerprinting scripts.
> Importantly, the integration with BU uses a CDP URL connection
> (not direct context injection), which avoids the Pydantic validation error
> that occurs when passing a Patchright browser_context directly to
> BrowserSession (GitHub issue #1934).

---

### Step 4 — Create `.env`

```dotenv
# ── LLM ──────────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY=your-deepseek-api-key-here
DEEPSEEK_MODEL=deepseek-chat          # deepseek-chat = DeepSeek V3

# ── Browser ───────────────────────────────────────────────────────────────────
BROWSER_HEADLESS=true                 # set to false for debugging
BROWSER_DEBUG_PORT=9222               # CDP remote debugging port for Patchright
BROWSER_USER_DATA_DIR=./chrome-profile # persistent profile dir (keeps cookies/state)

# ── Server ────────────────────────────────────────────────────────────────────
PORT=8000
HOST=0.0.0.0

# ── Concurrency ───────────────────────────────────────────────────────────────
MAX_CONCURRENT_SESSIONS=2             # Chrome instances allowed in parallel
                                      # 2 is comfortable on macOS Tahoe (~600-1000MB RAM)
                                      # raise to 3 cautiously; lower to 1 if memory is tight
                                      # queues excess requests — nothing is dropped

# ── Task ──────────────────────────────────────────────────────────────────────
TASK_TIMEOUT_SECONDS=300              # max seconds a single BU task can run (5 min)
```

---

### Step 5 — Project file structure

Create the following files (contents defined in steps below):

```
bu-local/
├── main.py           # FastAPI app — lifespan, routes, error handling
├── browser.py        # Patchright process manager (launch + CDP connect)
├── agent.py          # BU agent factory — creates Agent per request
├── config.py         # Typed env var loader via python-dotenv
├── .env              # Environment variables (gitignored)
├── .gitignore
├── pyproject.toml    # Created by uv init
└── README.md
```

---

### Step 6 — `config.py`

Loads and validates all env vars in one place. Any missing required var raises
at startup, not mid-request.

```python
# config.py
import os
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY: str   = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_MODEL: str     = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

BROWSER_HEADLESS: bool  = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
BROWSER_DEBUG_PORT: int = int(os.getenv("BROWSER_DEBUG_PORT", "9222"))
BROWSER_USER_DATA_DIR: str = os.getenv("BROWSER_USER_DATA_DIR", "./chrome-profile")

PORT: int               = int(os.getenv("PORT", "8000"))
HOST: str               = os.getenv("HOST", "0.0.0.0")

MAX_CONCURRENT_SESSIONS: int  = int(os.getenv("MAX_CONCURRENT_SESSIONS", "2"))
TASK_TIMEOUT_SECONDS: int     = int(os.getenv("TASK_TIMEOUT_SECONDS", "300"))
```

---

### Step 7 — `main.py` (base — health check only, no /run yet)

```python
# main.py
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from config import HOST, PORT, MAX_CONCURRENT_SESSIONS

# Semaphore: caps how many Chrome browser instances exist simultaneously.
# Protects Mac RAM — one Chrome instance is ~300-500MB.
# Excess requests are QUEUED (not rejected) — every request eventually runs.
# Default: 2 for macOS Tahoe. Tune via MAX_CONCURRENT_SESSIONS env var.
# Note: JAT's p-limit already controls pipeline-level concurrency.
# This semaphore is a separate layer protecting the browser/memory layer.
session_semaphore: asyncio.Semaphore = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once on startup and once on shutdown."""
    global session_semaphore
    session_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
    print(f"✓ BU Local wrapper ready — concurrency cap: {MAX_CONCURRENT_SESSIONS}")
    yield
    # Shutdown: nothing to clean up yet (browser launched per-request in Point 2)
    print("BU Local wrapper shutting down.")

app = FastAPI(
    title="BU Local Wrapper",
    description="Local open-source Browser Use — mirrors BU Cloud SDK interface.",
    version="1.0.0",
    lifespan=lifespan,
)

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, log_level="info")
```

---

### Step 8 — Verify Point 1

```bash
# Start the server
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# In a second terminal, hit the health check
curl http://localhost:8000/health
# Expected: {"status":"ok"}

# Also verify the auto-generated docs work
# Open http://localhost:8000/docs in a browser
```

Point 1 is complete when the health check returns `{"status":"ok"}` and the
server starts without errors.

---

---

## Point 2 — Single `/run` Endpoint

### What this point does

Adds the one endpoint that mirrors the BU Cloud SDK's `client.run(task, {model})`
interface. Any task string — `RESEARCH_PROMPT`, `OUTREACH_PROMPT`,
`DISCOVERY_PROMPT`, or any future prompt — passes through this endpoint
unchanged. The endpoint:

1. Receives `{ task, model? }`
2. Acquires the concurrency semaphore
3. Launches Patchright Chromium with a remote debugging port
4. Connects BU to it via `cdp_url` (avoids the Pydantic validation error)
5. Runs the BU agent to completion
6. Returns `{ output: string }`
7. Closes the browser and releases the semaphore

---

### Why CDP connection instead of direct Patchright context injection

Passing a Patchright `BrowserContext` directly to BU's `BrowserSession`
raises a Pydantic validation error (GitHub issue #1934) because BU's internal
Pydantic model validates that `browser_context` is specifically a Playwright
instance. Instead:

- Patchright Chromium is launched as a subprocess with `--remote-debugging-port`
- BU's `BrowserSession` connects to it via `cdp_url='http://localhost:{port}'`
- BU sees a standard Chrome CDP endpoint — it has no idea Patchright is involved
- All stealth patches happen inside the Patchright binary itself, transparently

---

### Step 1 — `browser.py` — Patchright browser manager

```python
# browser.py
import asyncio
from patchright.async_api import async_playwright, Playwright, Browser
from config import BROWSER_HEADLESS, BROWSER_USER_DATA_DIR

class PatchrightBrowser:
    """
    Manages one Patchright Chromium instance per request lifecycle.
    Launched fresh per task, closed after. This avoids state leaking
    between company research sessions.
    """

    def __init__(self, debug_port: int):
        self.debug_port = debug_port
        self._playwright: Playwright = None
        self._browser: Browser = None

    async def start(self) -> str:
        """
        Launches Patchright Chromium with remote debugging enabled.
        Returns the CDP URL BU should connect to.
        """
        self._playwright = await async_playwright().start()

        # launch_persistent_context is Patchright's recommended stealth config.
        # channel="chrome" uses the installed Google Chrome binary (more stealth
        # than Chromium). Falls back to Chromium if Chrome is not installed.
        # no_viewport=True is a key stealth patch Patchright applies.
        self._browser = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=BROWSER_USER_DATA_DIR,
            channel="chrome",
            headless=BROWSER_HEADLESS,
            no_viewport=True,
            args=[
                f"--remote-debugging-port={self.debug_port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        cdp_url = f"http://localhost:{self.debug_port}"
        # Give Chrome a moment to open the CDP socket
        await asyncio.sleep(1)
        return cdp_url

    async def stop(self):
        """Closes the browser and Playwright instance."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
```

---

### Step 2 — `agent.py` — BU agent factory

```python
# agent.py
from browser_use import Agent, BrowserSession, BrowserProfile
from langchain_openai import ChatOpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL

def make_llm() -> ChatOpenAI:
    """
    DeepSeek V3 via its OpenAI-compatible API.
    langchain_openai.ChatOpenAI accepts a custom base_url,
    making it compatible with any OpenAI-spec provider.
    """
    return ChatOpenAI(
        model=DEEPSEEK_MODEL,
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
        temperature=0,        # deterministic output for research tasks
        max_tokens=4096,
    )

async def run_task(task: str, cdp_url: str) -> str:
    """
    Creates a BU Agent connected to the Patchright browser via CDP,
    runs the task to completion, and returns the final result string.
    """
    browser_session = BrowserSession(
        browser_profile=BrowserProfile(
            cdp_url=cdp_url,
            is_local=True,
        )
    )

    agent = Agent(
        task=task,
        llm=make_llm(),
        browser=browser_session,
    )

    history = await agent.run()
    return history.final_result() or "No output returned from agent"
```

---

### Step 3 — Update `main.py` — add `/run` endpoint

Add these imports and the endpoint to `main.py`:

```python
# main.py — additions (merge with the base from Point 1)
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from config import (
    HOST, PORT,
    MAX_CONCURRENT_SESSIONS,
    BROWSER_DEBUG_PORT,
    TASK_TIMEOUT_SECONDS,
)
from browser import PatchrightBrowser
from agent import run_task

# ── Request / Response models ─────────────────────────────────────────────────

class RunRequest(BaseModel):
    task: str
    model: str | None = None  # accepted for BU Cloud SDK parity; ignored locally

class RunResponse(BaseModel):
    output: str | None
    error: str | None = None

# ── Lifespan (unchanged from Point 1) ────────────────────────────────────────

session_semaphore: asyncio.Semaphore = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global session_semaphore
    session_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
    print(f"✓ BU Local wrapper ready — concurrency cap: {MAX_CONCURRENT_SESSIONS}")
    yield
    print("BU Local wrapper shutting down.")

app = FastAPI(
    title="BU Local Wrapper",
    description="Local open-source Browser Use — mirrors BU Cloud SDK interface.",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Health check (unchanged) ──────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}

# ── /run endpoint ─────────────────────────────────────────────────────────────

@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest):
    """
    Generic BU task runner. Mirrors BU Cloud SDK's client.run(task, {model}).
    Accepts any task string. The model param is ignored — DeepSeek V3
    is always used (configured via DEEPSEEK_MODEL env var).

    Returns: { output: string } on success
             { output: null, error: string } on failure (HTTP 500)
    """
    # Blocking endpoint: awaits full task completion before returning.
    # Semaphore queues excess requests — at most MAX_CONCURRENT_SESSIONS
    # browsers run at once; others wait here until a slot frees up.
    async with session_semaphore:
        # One Patchright browser per request — clean state, no session leakage
        patchright = PatchrightBrowser(debug_port=BROWSER_DEBUG_PORT)

        try:
            cdp_url = await patchright.start()

            output = await asyncio.wait_for(
                run_task(req.task, cdp_url),
                timeout=TASK_TIMEOUT_SECONDS,
            )

            return RunResponse(output=output)

        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=f"Task timed out after {TASK_TIMEOUT_SECONDS}s",
            )

        except Exception as e:
            # Return 500 with error detail — TypeScript adapter will handle it
            raise HTTPException(status_code=500, detail=str(e))

        finally:
            # Always close the browser, even if the task raised an exception
            await patchright.stop()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, log_level="info")
```

---

### Step 4 — Start and verify Point 2

```bash
# Start the server
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Test with a simple task (in a second terminal)
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task": "Go to https://example.com and return the page title."}'

# Expected response shape:
# {"output": "Example Domain", "error": null}

# Test health still works
curl http://localhost:8000/health
# {"status": "ok"}

# Interactive docs — test /run manually via Swagger UI
# http://localhost:8000/docs
```

Point 2 is complete when:
- `/run` returns `{ "output": "..." }` for a simple task
- The browser launches and closes cleanly per request (check logs)
- A third concurrent request queues correctly and runs after one of the first two finishes
- The endpoint blocks until the BU task completes — no polling or callbacks needed

---

## Final File Structure After Points 1 & 2

```
bu-local/
├── main.py           ✓ FastAPI app, lifespan, /health, /run
├── browser.py        ✓ Patchright browser manager
├── agent.py          ✓ BU agent factory (DeepSeek + CDP connection)
├── config.py         ✓ Typed env var loader
├── .env              ✓ Secrets and config (gitignored)
├── .gitignore
├── pyproject.toml    ✓ Created by uv init
└── README.md
```

---

## Known Issues and Mitigations

### Issue 1: Port already in use (BROWSER_DEBUG_PORT)
If a previous run crashed without closing the browser, port 9222 may still be
occupied. Fix: kill the hanging Chrome process, or temporarily use a different
`BROWSER_DEBUG_PORT` value.

### Issue 2: Chrome channel not found
If `channel="chrome"` fails, Google Chrome is not installed. Either install
Chrome or remove `channel="chrome"` from `browser.py` to fall back to
Chromium (slightly less stealth).

### Issue 3: `final_result()` returns None
Some BU tasks end without producing a final structured output — the agent
finishes but returns an empty result. The `or "No output returned from agent"`
fallback in `agent.py` prevents a null response from reaching the TypeScript
adapter.

### Issue 4: BU version compatibility
Browser Use's API (Agent, BrowserSession, BrowserProfile) evolves quickly.
Pin the version in pyproject.toml after validating (`uv add browser-use==x.y.z`).
Check the BU GitHub changelog if `agent.run()` or `BrowserSession` signatures
differ from what's documented here.

---

## Environment Variables — Full Reference

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | ✓ | — | DeepSeek API key |
| `DEEPSEEK_MODEL` | | `deepseek-chat` | DeepSeek model to use |
| `BROWSER_HEADLESS` | | `true` | Headless mode toggle |
| `BROWSER_DEBUG_PORT` | | `9222` | Patchright CDP port |
| `BROWSER_USER_DATA_DIR` | | `./chrome-profile` | Chrome profile dir |
| `PORT` | | `8000` | FastAPI server port |
| `HOST` | | `0.0.0.0` | FastAPI server host |
| `MAX_CONCURRENT_SESSIONS` | | `2` | Chrome instance cap (queues excess requests) |
| `TASK_TIMEOUT_SECONDS` | | `300` | Per-task timeout |