# db.py
import os
import sqlite3
import json
from contextlib import contextmanager
from config import DB_PATH

# ── Connection helper ──────────────────────────────────────────────────────────

@contextmanager
def _get_conn():
    # sqlite3 does not create parent directories itself — ensure DB_PATH's
    # directory exists before every connection attempt.
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

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
                error_message   TEXT,
                total_tokens    INTEGER NOT NULL DEFAULT 0,
                total_cost      REAL    NOT NULL DEFAULT 0,
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
    error_message:  str | None = None,
    total_tokens:   int = 0,
    total_cost:     float = 0.0,
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
                output, status, error_message,
                total_tokens, total_cost,
                started_at, completed_at, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task, summary, model,
            steps_readable, steps_json,
            output, status, error_message,
            total_tokens, total_cost,
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
