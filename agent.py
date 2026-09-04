# agent.py
import json
import os
import time
from dataclasses import dataclass
from browser_use import Agent, BrowserSession, BrowserProfile, ChatGoogle, ChatOpenAI
from browser_use.llm.deepseek.chat import ChatDeepSeek
from browser_use.llm.anthropic.chat import ChatAnthropic
import asyncio
from config import GOOGLE_API_KEY, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY

# TEMP
import google.auth
import google.auth.transport.requests

# ── Per-provider factory functions ─────────────────────────────────────────────

def _make_google(model: str) -> ChatGoogle:
    """Handles all gemini-* model strings."""
    return ChatGoogle(
        model=model,
        api_key=GOOGLE_API_KEY,
        vertexai=True,
        project=os.getenv("GOOGLE_CLOUD_PROJECT"),
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
    )

def _make_deepseek(model: str) -> ChatDeepSeek:
    """Handles all deepseek-* model strings."""
    return ChatDeepSeek(
        model=model,
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
        max_tokens=4096,
    )

def _make_anthropic(model: str) -> ChatAnthropic:
    """Handles all claude-* model strings."""
    return ChatAnthropic(
        model=model,
        api_key=ANTHROPIC_API_KEY,
        max_tokens=4096,
    )

def _make_glm(model: str):
    return ChatOpenAI(
        model=model,
        api_key=os.getenv("ZAI_API_KEY"),
        base_url="https://api.z.ai/api/paas/v4/",
    )

def _make_glm_vertex(model: str):
    credentials, detected_project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    auth_req = google.auth.transport.requests.Request()
    credentials.refresh(auth_req)   # fetch fresh bearer token

    project = os.getenv("GOOGLE_CLOUD_PROJECT", detected_project)

    return ChatOpenAI(
        model=f"zai-org/{model}-maas",   # e.g. "zai-org/glm-4.7-maas"
        api_key=credentials.token,        # ADC bearer token
        base_url = (
            f"https://aiplatform.googleapis.com/v1beta1"
            f"/projects/{project}/locations/global/endpoints/openapi"
        ),
    )


# ── Provider map ───────────────────────────────────────────────────────────────

PROVIDER_MAP = {
    "gemini":   _make_google,
    "deepseek": _make_deepseek,
    "claude":   _make_anthropic,
    "glm":      _make_glm_vertex,
}

# ── Router ─────────────────────────────────────────────────────────────────────

def make_llm(model: str):
    """
    Splits model string on '-' and uses the first segment as the provider key.

    Examples:
        "gemini-3.6-flash"  -> provider "gemini"   -> _make_google("gemini-3.6-flash")
        "deepseek-chat"     -> provider "deepseek"  -> _make_deepseek("deepseek-chat")
        "claude-haiku-4-5"  -> provider "claude"    -> _make_anthropic("claude-haiku-4-5")

    Raises ValueError if the prefix doesn't match any registered provider.
    The ValueError is caught in main.py and returned as HTTP 400.
    """
    provider = model.split("-")[0].lower()
    factory = PROVIDER_MAP.get(provider)

    if not factory:
        raise ValueError(
            f"Unsupported provider prefix '{provider}' in model string '{model}'. "
            f"Supported prefixes: {list(PROVIDER_MAP.keys())}"
        )

    return factory(model)

# ── Result container ───────────────────────────────────────────────────────────

@dataclass
class TaskResult:
    output:         str | None
    steps_readable: str
    steps_json:     str
    status:         str       # "success" | "incomplete" | "failed"
    duration_ms:    int
    total_tokens:   int
    total_cost:     float

# ── Step extraction ────────────────────────────────────────────────────────────

def extract_steps(history) -> tuple[str, str]:
    """
    Reads the AgentHistoryList returned by agent.run() and produces:
    - steps_readable: human-readable multi-line string, one paragraph per step
    - steps_json:     JSON string of a list of step dicts

    Both are built from the same loop to avoid iterating history twice.
    Steps where model_output is None (initial navigation before first LLM call)
    are included with empty brain fields so the step count stays accurate.

    Note: per-step token counts are not available from BU's StepMetadata in
    the installed browser-use version — only an aggregate total (from
    history.usage) is available, and is stored on the session row instead
    of per step.
    """
    steps_list      = []
    readable_parts  = []

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

        # ── Timing ─────────────────────────────────────────────────────────────
        duration_s = item.metadata.duration_seconds if item.metadata else 0.0

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
            f"  Duration:  {duration_s:.1f}s"
        )

        # ── JSON step ──────────────────────────────────────────────────────────
        steps_list.append({
            "step":       step_num,
            "eval":       eval_text,
            "memory":     memory_text,
            "next_goal":  next_goal,
            "actions":    actions,
            "results":    results,
            "duration_s": round(duration_s, 2),
        })

    steps_readable = "\n\n".join(readable_parts)
    steps_json     = json.dumps(steps_list, indent=2, ensure_ascii=False)

    return steps_readable, steps_json

# ── Task runner ────────────────────────────────────────────────────────────────

@dataclass
class AgentOptions:
    """
    Per-request overrides for the BU Agent constructor and run() call.
    Defaults mirror the previously hardcoded values so existing behaviour
    is preserved when no options are supplied.
    """
    max_steps:    int  = 10     # passed to agent.run(max_steps=...)
    step_timeout: int  = 60     # passed to Agent(step_timeout=...)
    max_failures: int  = 3      # passed to Agent(max_failures=...)
    use_vision:   bool = False  # passed to Agent(use_vision=...)
    max_history_items: int = 6  # passed to Agent(max_history_items=...)

async def run_task(task: str, cdp_url: str, model: str, options: AgentOptions | None = None) -> TaskResult:
    """
    Runs the BU agent and returns a TaskResult with the output,
    step breakdown (readable + JSON), status, wall-clock duration,
    and aggregate token/cost usage for the whole task.
    """
    opts = options or AgentOptions()

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
        use_vision=opts.use_vision,       # ← from options, default False
        max_failures=opts.max_failures,   # ← from options, default 3
        step_timeout=opts.step_timeout,   # ← from options, default 60
        max_history_items=opts.max_history_items # ← from options, default 5
    )
    try:
        history = await agent.run(max_steps=opts.max_steps)
    except asyncio.CancelledError:
        raise  # let it propagate — asyncio.wait_for in main.py converts it to TimeoutError
    except Exception as e:
        raise RuntimeError(f"BU agent failed: {e}") from e

    duration_ms = int(time.time() * 1000) - started_ms
    output      = history.final_result() or None
    steps_readable, steps_json = extract_steps(history)

    # is_successful() is True/False/None — None means the agent never called
    # "done" (e.g. it got stuck or hit the step limit), distinct from an
    # explicit failure.
    successful = history.is_successful()
    if successful is True:
        status = "success"
    elif successful is None:
        status = "incomplete"
    else:
        status = "failed"

    usage        = history.usage
    total_tokens = usage.total_tokens if usage else 0
    total_cost   = usage.total_cost if usage else 0.0

    return TaskResult(
        output=output,
        steps_readable=steps_readable,
        steps_json=steps_json,
        status=status,
        duration_ms=duration_ms,
        total_tokens=total_tokens,
        total_cost=total_cost,
    )