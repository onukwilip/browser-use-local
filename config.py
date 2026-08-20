# config.py
import os
from dotenv import load_dotenv

load_dotenv()

BROWSER_HEADLESS: bool     = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
BROWSER_DEBUG_PORT: int    = int(os.getenv("BROWSER_DEBUG_PORT", "9222"))
BROWSER_USER_DATA_DIR: str = os.getenv("BROWSER_USER_DATA_DIR", "./chrome-profile")

# ── Server ─────────────────────────────────────────────────────────────────────
PORT: int = int(os.getenv("PORT", "8000"))
HOST: str = os.getenv("HOST", "0.0.0.0")

# ── Concurrency ────────────────────────────────────────────────────────────────
MAX_CONCURRENT_SESSIONS: int = int(os.getenv("MAX_CONCURRENT_SESSIONS", "2"))
TASK_TIMEOUT_SECONDS: int    = int(os.getenv("TASK_TIMEOUT_SECONDS", "300"))

# ── LLM providers ──────────────────────────────────────────────────────────────
DEFAULT_MODEL: str       = os.getenv("DEFAULT_MODEL", "gemini-3.6-flash")
GOOGLE_API_KEY: str      = os.getenv("GOOGLE_API_KEY", "")
DEEPSEEK_API_KEY: str    = os.getenv("DEEPSEEK_API_KEY", "")
ANTHROPIC_API_KEY: str   = os.getenv("ANTHROPIC_API_KEY", "")

# ── Database ───────────────────────────────────────────────────────────────────
DB_PATH: str             = os.getenv("DB_PATH", "./data/sessions.db")
SUMMARY_MAX_LENGTH: int  = int(os.getenv("SUMMARY_MAX_LENGTH", "150"))