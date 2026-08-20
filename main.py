# main.py
import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from config import (
    HOST, PORT,
    MAX_CONCURRENT_SESSIONS,
    BROWSER_DEBUG_PORT,
    TASK_TIMEOUT_SECONDS,
    DEFAULT_MODEL,
    SUMMARY_MAX_LENGTH,
)
from browser import PatchrightBrowser
from agent import run_task
from db import init_db, record_session, get_session

# ── Request / Response models ─────────────────────────────────────────────────

class RunRequest(BaseModel):
    task: str
    model: str | None = None  # optional — falls back to DEFAULT_MODEL when omitted

class RunResponse(BaseModel):
    output: str | None
    error: str | None = None
    session_id: int | None = None  # ID of the stored session record

# ── Lifespan ──────────────────────────────────────────────────────────────────

# Semaphore: caps how many Chrome browser instances exist simultaneously.
# Protects Mac RAM — one Chrome instance is ~300-500MB.
# Excess requests are QUEUED (not rejected) — every request eventually runs.
# Default: 2 for macOS Tahoe. Tune via MAX_CONCURRENT_SESSIONS env var.
# Note: JAT's p-limit already controls pipeline-level concurrency.
# This semaphore is a separate layer protecting the browser/memory layer.
session_semaphore: asyncio.Semaphore = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global session_semaphore
    session_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
    init_db()
    print(f"✓ BU Local wrapper ready — concurrency cap: {MAX_CONCURRENT_SESSIONS}")
    yield
    print("BU Local wrapper shutting down.")

app = FastAPI(
    title="BU Local Wrapper",
    description="Local open-source Browser Use — mirrors BU Cloud SDK interface.",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}

# ── /run endpoint ─────────────────────────────────────────────────────────────

def _summarize(task: str) -> str:
    return task[:SUMMARY_MAX_LENGTH] + ("..." if len(task) > SUMMARY_MAX_LENGTH else "")

@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest):
    """
    Generic BU task runner. Mirrors BU Cloud SDK's client.run(task, {model}).
    model resolves to DEFAULT_MODEL when not supplied in the request body.
    Supported prefixes: gemini-*, deepseek-*, claude-*
    Returns HTTP 400 if the model prefix is not recognised.

    Every session that actually reaches the browser (success, timeout, or an
    unexpected error) is recorded in the sessions DB and its ID returned as
    session_id. Bad model prefixes (HTTP 400) are not recorded — the request
    never touched the browser.

    Returns: { output: string, session_id: int } on success
             { output: null, error: string } on failure (HTTP 400/500/504)
    """
    model      = req.model or DEFAULT_MODEL
    summary    = _summarize(req.task)
    started_at = datetime.now(timezone.utc).isoformat()
    started_perf = time.monotonic()

    # Blocking endpoint: awaits full task completion before returning.
    # Semaphore queues excess requests — at most MAX_CONCURRENT_SESSIONS
    # browsers run at once; others wait here until a slot frees up.
    async with session_semaphore:
        # One Patchright browser per request — clean state, no session leakage
        patchright = PatchrightBrowser()

        try:
            cdp_url = await patchright.start()

            task_result = await asyncio.wait_for(
                run_task(req.task, cdp_url, model),
                timeout=TASK_TIMEOUT_SECONDS,
            )

            completed_at = datetime.now(timezone.utc).isoformat()
            session_id = await asyncio.to_thread(
                record_session,
                task=req.task,
                summary=summary,
                model=model,
                steps_readable=task_result.steps_readable,
                steps_json=task_result.steps_json,
                output=task_result.output,
                status=task_result.status,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=task_result.duration_ms,
                total_tokens=task_result.total_tokens,
                total_cost=task_result.total_cost,
            )

            return RunResponse(output=task_result.output, session_id=session_id)

        except asyncio.TimeoutError:
            completed_at = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(
                record_session,
                task=req.task,
                summary=summary,
                model=model,
                steps_readable="",
                steps_json="[]",
                output=None,
                status="timeout",
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=int((time.monotonic() - started_perf) * 1000),
            )
            raise HTTPException(
                status_code=504,
                detail=f"Task timed out after {TASK_TIMEOUT_SECONDS}s",
            )

        except ValueError as e:
            # Unknown provider prefix — bad model string in the request.
            # Never reached the browser, so nothing is recorded.
            raise HTTPException(status_code=400, detail=str(e))

        except Exception as e:
            # Unexpected failure (browser crash, LLM error not handled
            # internally by BU, etc). Still recorded so it's visible in the DB.
            completed_at = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(
                record_session,
                task=req.task,
                summary=summary,
                model=model,
                steps_readable="",
                steps_json="[]",
                output=None,
                status="error",
                error_message=str(e),
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=int((time.monotonic() - started_perf) * 1000),
            )
            # Return 500 with error detail — TypeScript adapter will handle it
            raise HTTPException(status_code=500, detail=str(e))

        finally:
            try:
                await asyncio.wait_for(patchright.stop(), timeout=10)
            except Exception:
                pass

# ── /session/{id} endpoint ───────────────────────────────────────────────────

@app.get("/session/{session_id}")
async def get_session_endpoint(session_id: int):
    """
    Retrieves a stored session record by ID (the session_id returned by /run).
    Returns 404 if no session with that ID exists.
    """
    session = await asyncio.to_thread(get_session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, log_level="info")
