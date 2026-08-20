# BU Local Wrapper — Session Storage Implementation Plan

---

## Overview

After every `/run` call, record the full BU session in a local SQLite database.
Each record stores the task, an auto-truncated summary, the model used, both
readable and JSON step breakdowns, the final output, status, timestamps, and
total duration. The session ID is returned in the API response so the TypeScript
adapter in JAT can reference it.

---

## Files to create / modify

```
bu-local/
├── db.py          ← NEW — SQLite init, record_session(), get_session()
├── agent.py       ← ADD TaskResult dataclass, extract_steps(); update run_task() return type
├── main.py        ← UPDATE RunResponse, call record_session() after run_task()
├── config.py      ← ADD DB_PATH, SUMMARY_MAX_LENGTH
└── .env           ← ADD DB_PATH, SUMMARY_MAX_LENGTH
```

---

## Step 1 — Update `.env`

Add two new entries:

```dotenv
# ── Database ───────────────────────────────────────────────────────────────────
DB_PATH=./data/sessions.db        # SQLite file path, relative to bu-local/
SUMMARY_MAX_LENGTH=150       # Max chars for the auto-truncated task summary
```

---

## Step 2 — Update `config.py`

Add two new typed reads to the existing file:

```python
# ── Database ───────────────────────────────────────────────────────────────────
DB_PATH: str              = os.getenv("DB_PATH", "./data/sessions.db")
SUMMARY_MAX_LENGTH: int   = int(os.getenv("SUMMARY_MAX_LENGTH", "150"))
```

---

## Step 3 — Create `db.py` (new file)

**What it does:**
- Opens / creates the SQLite database at `DB_PATH` on first import
- Creates the `sessions` table if it doesn't exist
- Exposes `record_session()` to write a completed session row
- Exposes `get_session()` to retrieve a row by ID (for future use)

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task            TEXT    NOT NULL,          -- full task/prompt string
    summary         TEXT    NOT NULL,          -- first SUMMARY_MAX_LENGTH chars of task
    model           TEXT    NOT NULL,          -- model string used (e.g. gemini-3.6-flash)
    steps_readable  TEXT    NOT NULL,          -- human-readable step breakdown
    steps_json      TEXT    NOT NULL,          -- JSON array of step objects
    output          TEXT,                      -- final_result() from agent (NULL if failed)
    status          TEXT    NOT NULL,          -- success | failed | timeout | error
    started_at      TEXT    NOT NULL,          -- ISO datetime when /run was called
    completed_at    TEXT    NOT NULL,          -- ISO datetime when session finished
    duration_ms     INTEGER NOT NULL           -- wall-clock ms from start to finish
)
```

**Full `db.py`:**

```python
# db.py
import sqlite3
import json
from contextlib import contextmanager
from config import DB_PATH

# ── Connection helper ──────────────────────────────────────────────────────────

@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# ── Schema init ────────────────────────────────────────────────────────────────

def init_db() -> None:
    """
    Creates the sessions table if it doesn't exist.
    Call once on server startup from main.py's lifespan.
    """
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                task            TEXT    NOT NULL,
                summary         TEXT    NOT NULL,
                model           TEXT    NOT NULL,
                steps_readable  TEXT    NOT NULL,
                steps_json      TEXT    NOT NULL,
                output          TEXT,
                status          TEXT    NOT NULL,
                started_at      TEXT    NOT NULL,
                completed_at    TEXT    NOT NULL,
                duration_ms     INTEGER NOT NULL
            )
        """)
    print(f"✓ Sessions DB ready at {DB_PATH}")

# ── Write ──────────────────────────────────────────────────────────────────────

def record_session(
    task:           str,
    summary:        str,
    model:          str,
    steps_readable: str,
    steps_json:     str,
    output:         str | None,
    status:         str,
    started_at:     str,
    completed_at:   str,
    duration_ms:    int,
) -> int:
    """
    Inserts one session row and returns the new row ID.
    All string values are written as-is — no truncation happens here,
    truncation is the caller's responsibility.
    """
    with _get_conn() as conn:
        cursor = conn.execute("""
            INSERT INTO sessions (
                task, summary, model,
                steps_readable, steps_json,
                output, status,
                started_at, completed_at, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task, summary, model,
            steps_readable, steps_json,
            output, status,
            started_at, completed_at, duration_ms,
        ))
        return cursor.lastrowid

# ── Read ───────────────────────────────────────────────────────────────────────

def get_session(session_id: int) -> dict | None:
    """
    Returns a session row as a dict, or None if not found.
    Parses steps_json back into a Python list for convenience.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()

    if row is None:
        return None

    result = dict(row)
    result["steps_json"] = json.loads(result["steps_json"])
    return result
```

---

## Step 4 — Update `agent.py`

**What changes:**
- Add a `TaskResult` dataclass so `run_task()` can return structured data
- Add `extract_steps()` helper that reads the BU history object and produces
  both the readable text and the JSON array
- Update `run_task()` to return `TaskResult` instead of `str`

### 4a. BU history object structure (confirmed from source)

Each item in `history.history` is an `AgentHistory` with:

```
AgentHistory
├── model_output: AgentOutput | None
│   ├── current_state: AgentBrain
│   │   ├── evaluation_previous_goal: str
│   │   ├── memory: str
│   │   ├── next_goal: str
│   │   └── thinking: str | None
│   └── action: list[ActionModel]     ← one dict per action taken
├── result: list[ActionResult]
│   ├── extracted_content: str | None
│   ├── error: str | None
│   └── is_done: bool
└── metadata: StepMetadata | None
    ├── step_number: int
    ├── input_tokens: int
    └── duration_seconds: float        ← property: step_end_time - step_start_time
```

### 4b. `steps_readable` format per step

```
Step 1
  Eval:      Successfully navigated to the target URL.
  Memory:    The page is a portfolio for Prince C. Onukwili.
  Next goal: Scroll down to view more content.
  Actions:   scroll → {"down": true, "pages": 1.0}
  Result:    Scrolled down 469px
  Duration:  3.2s | Tokens: 5,432
```

### 4c. `steps_json` format per step

```json
{
  "step": 1,
  "eval": "Successfully navigated to the target URL.",
  "memory": "The page is a portfolio for Prince C. Onukwili.",
  "next_goal": "Scroll down to view more content.",
  "actions": [{"scroll": {"down": true, "pages": 1.0}}],
  "results": [{"content": "Scrolled down 469px", "error": null, "is_done": false}],
  "duration_s": 3.2,
  "input_tokens": 5432
}
```

### 4d. Full additions to `agent.py`

Add these imports at the top:

```python
import json
import time
from dataclasses import dataclass
```

Add the `TaskResult` dataclass and `extract_steps()` helper **above** `make_llm()`:

```python
# ── Result container ───────────────────────────────────────────────────────────

@dataclass
class TaskResult:
    output:         str | None
    steps_readable: str
    steps_json:     str
    status:         str       # "success" | "failed" | "error"
    duration_ms:    int

# ── Step extraction ────────────────────────────────────────────────────────────

def extract_steps(history) -> tuple[str, str]:
    """
    Reads the AgentHistoryList returned by agent.run() and produces:
    - steps_readable: human-readable multi-line string, one paragraph per step
    - steps_json:     JSON string of a list of step dicts

    Both are built from the same loop to avoid iterating history twice.
    Steps where model_output is None (initial navigation before first LLM call)
    are included with empty brain fields so the step count stays accurate.
    """
    steps_list   = []
    readable_parts = []

    for item in history.history:
        # ── Step number ────────────────────────────────────────────────────────
        step_num = (
            item.metadata.step_number
            if item.metadata is not None
            else len(steps_list) + 1
        )

        # ── Brain state (eval / memory / next_goal) ────────────────────────────
        if item.model_output and item.model_output.current_state:
            brain       = item.model_output.current_state
            eval_text   = brain.evaluation_previous_goal or ""
            memory_text = brain.memory or ""
            next_goal   = brain.next_goal or ""
        else:
            eval_text = memory_text = next_goal = ""

        # ── Actions ────────────────────────────────────────────────────────────
        actions = []
        if item.model_output and item.model_output.action:
            for action in item.model_output.action:
                try:
                    actions.append(action.model_dump(exclude_none=True, mode="json"))
                except Exception:
                    actions.append(str(action))

        # ── Results ────────────────────────────────────────────────────────────
        results = []
        if item.result:
            for r in item.result:
                results.append({
                    "content": r.extracted_content,
                    "error":   r.error,
                    "is_done": r.is_done,
                })

        # ── Timing + tokens ────────────────────────────────────────────────────
        duration_s   = item.metadata.duration_seconds if item.metadata else 0.0
        input_tokens = item.metadata.input_tokens     if item.metadata else 0

        # ── Readable block ─────────────────────────────────────────────────────
        action_str = " | ".join(json.dumps(a) for a in actions) if actions else "none"
        result_str = " | ".join(
            r["content"] or r["error"] or "no output" for r in results
        ) if results else "no result"

        readable_parts.append(
            f"Step {step_num}\n"
            f"  Eval:      {eval_text}\n"
            f"  Memory:    {memory_text}\n"
            f"  Next goal: {next_goal}\n"
            f"  Actions:   {action_str}\n"
            f"  Result:    {result_str}\n"
            f"  Duration:  {duration_s:.1f}s | Tokens: {input_tokens:,}"
        )

        # ── JSON step ──────────────────────────────────────────────────────────
        steps_list.append({
            "step":         step_num,
            "eval":         eval_text,
            "memory":       memory_text,
            "next_goal":    next_goal,
            "actions":      actions,
            "results":      results,
            "duration_s":   round(duration_s, 2),
            "input_tokens": input_tokens,
        })

    steps_readable = "\n\n".join(readable_parts)
    steps_json     = json.dumps(steps_list, indent=2, ensure_ascii=False)

    return steps_readable, steps_json
```

### 4e. Update `run_task()` return type

Replace the existing `run_task()` with this version:

```python
async def run_task(task: str, cdp_url: str, model: str) -> TaskResult:
    """
    Runs the BU agent and returns a TaskResult with the output,
    step breakdown (readable + JSON), status, and wall-clock duration.
    """
    started_ms = int(time.time() * 1000)

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

    duration_ms      = int(time.time() * 1000) - started_ms
    output           = history.final_result() or None
    steps_readable, steps_json = extract_steps(history)
    status           = "success" if history.is_successful() else "failed"

    return TaskResult(
        output=output,
        steps_readable=steps_readable,
        steps_json=steps_json,
        status=status,
        duration_ms=duration_ms,
    )
```

---

## Step 5 — Update `main.py`

**What changes:** Four targeted edits.

### 5a. Add imports

```python
from datetime import datetime, timezone
from db import init_db, record_session
from config import SUMMARY_MAX_LENGTH
```

### 5b. Update `RunResponse` to include `session_id`

```python
class RunResponse(BaseModel):
    output:     str | None
    error:      str | None = None
    session_id: int | None = None   # ← new: ID of the stored session record
```

### 5c. Call `init_db()` in the lifespan

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    global session_semaphore
    session_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
    init_db()                          # ← add this line
    print(f"✓ BU Local wrapper ready — concurrency cap: {MAX_CONCURRENT_SESSIONS}")
    yield
    print("BU Local wrapper shutting down.")
```

### 5d. Update the `/run` endpoint

Replace only the inner `try` block. The semaphore, `patchright`, and `finally`
stay exactly the same:

```python
@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest):
    model      = req.model or DEFAULT_MODEL
    started_at = datetime.now(timezone.utc).isoformat()

    async with session_semaphore:
        patchright = PatchrightBrowser(debug_port=BROWSER_DEBUG_PORT)
        try:
            cdp_url = await patchright.start()

            task_result = await asyncio.wait_for(
                run_task(req.task, cdp_url, model),
                timeout=TASK_TIMEOUT_SECONDS,
            )

            completed_at = datetime.now(timezone.utc).isoformat()
            summary      = req.task[:SUMMARY_MAX_LENGTH] + (
                "..." if len(req.task) > SUMMARY_MAX_LENGTH else ""
            )

            session_id = record_session(
                task           = req.task,
                summary        = summary,
                model          = model,
                steps_readable = task_result.steps_readable,
                steps_json     = task_result.steps_json,
                output         = task_result.output,
                status         = task_result.status,
                started_at     = started_at,
                completed_at   = completed_at,
                duration_ms    = task_result.duration_ms,
            )

            return RunResponse(
                output     = task_result.output,
                session_id = session_id,
            )

        except asyncio.TimeoutError:
            completed_at = datetime.now(timezone.utc).isoformat()
            summary      = req.task[:SUMMARY_MAX_LENGTH] + (
                "..." if len(req.task) > SUMMARY_MAX_LENGTH else ""
            )
            record_session(
                task           = req.task,
                summary        = summary,
                model          = model,
                steps_readable = "",
                steps_json     = "[]",
                output         = None,
                status         = "timeout",
                started_at     = started_at,
                completed_at   = completed_at,
                duration_ms    = TASK_TIMEOUT_SECONDS * 1000,
            )
            raise HTTPException(
                status_code=504,
                detail=f"Task timed out after {TASK_TIMEOUT_SECONDS}s",
            )

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        finally:
            await patchright.stop()
```

> **Note on error cases:** timeout sessions are recorded with `status="timeout"`
> and empty steps so the row exists for debugging. Generic exceptions (HTTP 500)
> are not recorded — the session never completed enough to produce step data.
> This can be changed later if needed.

---

## Step 6 — Verify and test

### 6a. Start the server

```bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Confirm the startup log shows both lines:

```
✓ Sessions DB ready at ./sessions.db
✓ BU Local wrapper ready — concurrency cap: 2
```

### 6b. Run a task and check the response

```bash
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"task": "Go to https://example.com and return the page title."}'
```

Expected response shape:

```json
{
  "output": "Example Domain",
  "error":  null,
  "session_id": 1
}
```

### 6c. Inspect the stored record

```bash
uv run python -c "
from db import get_session
import json
s = get_session(1)
print('Task:    ', s['task'])
print('Summary: ', s['summary'])
print('Model:   ', s['model'])
print('Status:  ', s['status'])
print('Duration:', s['duration_ms'], 'ms')
print()
print('--- Steps (readable) ---')
print(s['steps_readable'])
print()
print('--- Steps (JSON, first step) ---')
print(json.dumps(s['steps_json'][0], indent=2))
"
```

### 6d. Success criteria

- `sessions.db` is created in `bu-local/` on first startup
- Every successful `/run` call returns a `session_id`
- `get_session(id)` returns the full record including parsed `steps_json`
- Timeout runs are recorded with `status="timeout"` and `steps_json="[]"`
- No changes to JAT — it reads `session_id` from the response and can store
  or ignore it as needed

---

## Future extension: GET `/session/{id}` endpoint

Once the DB is in place, exposing stored sessions via the API is trivial — add
one read-only route to `main.py`:

```python
@app.get("/session/{session_id}")
async def get_session_endpoint(session_id: int):
    from db import get_session
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session
```