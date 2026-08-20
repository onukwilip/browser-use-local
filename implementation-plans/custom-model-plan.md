# BU Local Wrapper — Model Router Implementation Plan

---

## Overview

Modify the local BU wrapper so the `/run` endpoint accepts a `model` string
that determines which LLM provider and class to use. The `model` string is
split on `-` and the first segment (the prefix) is used to route to the correct
BU LLM class. If no model is supplied, the server falls back to `DEFAULT_MODEL`
from the environment.

Examples:
- `gemini-3.6-flash` → `ChatGoogle`
- `gemini-3.7-flash` → `ChatGoogle`
- `deepseek-chat` → `ChatDeepSeek`
- `claude-haiku-4-5` → `ChatAnthropic`

---

## Files to modify

```
bu-local/
├── config.py     ← add GOOGLE_API_KEY, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY, DEFAULT_MODEL
├── agent.py      ← replace single make_llm() with provider router
├── main.py       ← pass model through to run_task, handle 400 on bad model string
└── .env          ← add new keys
```

---

## Step 1 — Update `.env`

Add the following keys. Keys for providers not in use can be left empty.

```dotenv
# ── LLM providers ──────────────────────────────────────────────────────────────
DEFAULT_MODEL=gemini-3.6-flash        # fallback when /run is called without a model field
GOOGLE_API_KEY=your-google-key
DEEPSEEK_API_KEY=your-deepseek-key
ANTHROPIC_API_KEY=your-anthropic-key  # optional — only needed if claude-* models are used
```

**What each var does:**

| Variable | Purpose |
|---|---|
| `DEFAULT_MODEL` | Model used when `req.model` is `None` or omitted from the request body |
| `GOOGLE_API_KEY` | Passed to `ChatGoogle` for all `gemini-*` models |
| `DEEPSEEK_API_KEY` | Passed to `ChatDeepSeek` for all `deepseek-*` models |
| `ANTHROPIC_API_KEY` | Passed to `ChatAnthropic` for all `claude-*` models |

---

## Step 2 — Update `config.py`

**What changes:** Add four new typed env var reads. Everything else stays the same.

**Add these lines** to the existing LLM section (replacing the old single
`DEEPSEEK_API_KEY` and `DEEPSEEK_MODEL` vars):

```python
# ── LLM providers ──────────────────────────────────────────────────────────────
DEFAULT_MODEL: str    = os.getenv("DEFAULT_MODEL", "gemini-3.6-flash")
GOOGLE_API_KEY: str   = os.getenv("GOOGLE_API_KEY", "")
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
```

**Remove** any old `DEEPSEEK_MODEL` or single-provider key reads that no longer
apply.

---

## Step 3 — Rewrite `agent.py`

**What changes:** Replace the single `make_llm()` function with a provider
router. `run_task()` now receives `model` as a third parameter.

**Full replacement for `agent.py`:**

```python
# agent.py
from browser_use import Agent, BrowserSession, BrowserProfile, ChatGoogle
from browser_use.llm.deepseek.chat import ChatDeepSeek
from config import (
    DEFAULT_MODEL,
    GOOGLE_API_KEY,
    DEEPSEEK_API_KEY,
    ANTHROPIC_API_KEY,
)

# ── Per-provider factory functions ─────────────────────────────────────────────

def _make_google(model: str):
    """
    Handles all gemini-* model strings.
    ChatGoogle resolves GOOGLE_API_KEY automatically if not passed,
    but we pass it explicitly for clarity.
    """
    return ChatGoogle(
        model=model,
        api_key=GOOGLE_API_KEY,
    )

def _make_deepseek(model: str):
    """
    Handles all deepseek-* model strings.
    base_url points at DeepSeek's API endpoint.
    dont_force_structured_output must remain True — DeepSeek's API
    does not support BU's response_format parameter.
    """
    return ChatDeepSeek(
        model=model,
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
        max_tokens=4096,
    )

def _make_anthropic(model: str):
    """
    Handles all claude-* model strings.
    Verify the correct import path for ChatAnthropic in BU 0.13.7
    before using — see the verification note at the bottom of this plan.
    """
    try:
        from browser_use.llm.anthropic.chat import ChatAnthropic
    except ImportError:
        from browser_use import ChatAnthropic  # fallback if path differs

    return ChatAnthropic(
        model=model,
        api_key=ANTHROPIC_API_KEY,
        max_tokens=4096,
    )

# ── Provider map ───────────────────────────────────────────────────────────────

PROVIDER_MAP = {
    "gemini":   _make_google,
    "deepseek": _make_deepseek,
    "claude":   _make_anthropic,
}

# ── Router ─────────────────────────────────────────────────────────────────────

def make_llm(model: str):
    """
    Splits model string on '-' and uses the first segment as the provider key.

    Examples:
        "gemini-3.6-flash"  → provider "gemini"  → _make_google("gemini-3.6-flash")
        "deepseek-chat"     → provider "deepseek" → _make_deepseek("deepseek-chat")
        "claude-haiku-4-5"  → provider "claude"   → _make_anthropic("claude-haiku-4-5")

    Raises ValueError if the prefix doesn't match any registered provider.
    The ValueError is caught in main.py and returned as HTTP 400.
    """
    provider = model.split("-")[0].lower()
    factory  = PROVIDER_MAP.get(provider)

    if not factory:
        raise ValueError(
            f"Unsupported provider prefix '{provider}' in model string '{model}'. "
            f"Supported prefixes: {list(PROVIDER_MAP.keys())}"
        )

    return factory(model)

# ── Task runner ────────────────────────────────────────────────────────────────

async def run_task(task: str, cdp_url: str, model: str) -> str:
    """
    Creates a BU Agent connected to the Patchright browser via CDP.
    model is resolved to the correct LLM class by make_llm().
    """
    browser_session = BrowserSession(
        browser_profile=BrowserProfile(
            cdp_url=cdp_url,
            is_local=True,
        )
    )

    agent = Agent(
        task=task,
        llm=make_llm(model),
        browser=browser_session,
    )

    history = await agent.run()
    return history.final_result() or "No output returned from agent"
```

---

## Step 4 — Update `main.py`

**What changes:** Two targeted edits only.

### 4a. Import `DEFAULT_MODEL` from config

Add `DEFAULT_MODEL` to the existing config import:

```python
from config import (
    HOST, PORT,
    MAX_CONCURRENT_SESSIONS,
    BROWSER_DEBUG_PORT,
    TASK_TIMEOUT_SECONDS,
    DEFAULT_MODEL,              # ← add this
)
```

### 4b. Update the `/run` endpoint

Replace the `run_task` call inside the endpoint with the version below. The only
changes are: resolve `model` before entering the semaphore, pass `model` to
`run_task`, and add a `ValueError` catch that returns HTTP 400 for unknown
provider strings.

```python
@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest):
    """
    Generic BU task runner.
    model resolves to DEFAULT_MODEL when not supplied in the request body.
    Supported prefixes: gemini-*, deepseek-*, claude-*
    Returns HTTP 400 if the model prefix is not recognised.
    """
    model = req.model or DEFAULT_MODEL

    async with session_semaphore:
        patchright = PatchrightBrowser(debug_port=BROWSER_DEBUG_PORT)
        try:
            cdp_url = await patchright.start()
            output  = await asyncio.wait_for(
                run_task(req.task, cdp_url, model),
                timeout=TASK_TIMEOUT_SECONDS,
            )
            return RunResponse(output=output)

        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=f"Task timed out after {TASK_TIMEOUT_SECONDS}s",
            )
        except ValueError as e:
            # Unknown provider prefix — bad model string in the request
            raise HTTPException(status_code=400, detail=str(e))

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        finally:
            await patchright.stop()
```

---

## Step 5 — Verify and test

### 5a. Verify the ChatAnthropic import path

Run this before starting the server to confirm the correct import:

```bash
uv run python -c "from browser_use.llm.anthropic.chat import ChatAnthropic; print('path OK')"
# If that fails, try:
uv run python -c "from browser_use import ChatAnthropic; print('fallback path OK')"
```

Use whichever path succeeds. The `_make_anthropic` factory in `agent.py` already
tries both via a try/except, but confirm and update to the correct single import
once known.

### 5b. Start the server

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 5c. Test each provider

```bash
# Default model (no model field — should use DEFAULT_MODEL=gemini-3.6-flash)
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task": "Go to https://example.com and return the page title."}'

# Gemini explicitly
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task": "Go to https://example.com and return the page title.", "model": "gemini-3.6-flash"}'

# DeepSeek
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task": "Go to https://example.com and return the page title.", "model": "deepseek-chat"}'

# Bad model string — should return HTTP 400
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task": "...", "model": "grok-2-mini"}'
# Expected: {"detail": "Unsupported provider prefix 'grok' ..."}
```

### 5d. Success criteria

- Default request (no model) uses `DEFAULT_MODEL` and returns a valid result
- `gemini-*` requests use `ChatGoogle` (confirmed in logs: `provider=google`)
- `deepseek-*` requests use `ChatDeepSeek` (confirmed in logs: `provider=deepseek`)
- Unknown prefix returns HTTP 400, not 500
- No regression on existing behaviour (health check, semaphore, timeout)

---

## Adding a new provider in future

To support a new provider (e.g. `openai-*`, `kimi-*`), add two things:

1. A factory function:
```python
def _make_openai(model: str):
    from browser_use import ChatOpenAI
    return ChatOpenAI(model=model, api_key=OPENAI_API_KEY, ...)
```

2. One entry in `PROVIDER_MAP`:
```python
PROVIDER_MAP = {
    "gemini":   _make_google,
    "deepseek": _make_deepseek,
    "claude":   _make_anthropic,
    "openai":   _make_openai,   # ← new
}
```

No changes needed anywhere else.