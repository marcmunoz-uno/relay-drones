"""attempted_fixes — memory of what's been tried, so the loop doesn't re-do the same advice forever.

Without this, every cycle the system re-discovers the same broken cron /
crashed service / failing healthcheck and produces the same advisory. With
it, the loop closes:

    - Worker writes a row after every handoff (success, skipped, failed).
    - Ingestors consult `recent_attempts(target)` before generating a new
      note. If there's already an unsuccessful attempt in the last 24h,
      the ingestor either skips OR upgrades to `action_kind=notify_human`
      so a human gets pulled in instead of yet another Claude run.

Schema is a separate table in the same SQLite DB as the task queue so
WAL serialization handles concurrency for free. No new dependencies.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from relay_drones.config import QUEUE_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempted_fixes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    target            TEXT NOT NULL,        -- "cron:Memory Server Health" / "service:nginx" / "file:/etc/x.conf"
    action_kind       TEXT NOT NULL,
    outcome           TEXT NOT NULL,        -- "success" | "skipped" | "failed" | "notified"
    advisory_task_id  TEXT,                 -- task_id of the originating task
    handoff_artifact  TEXT,                 -- path to claude handoff artifact (if any)
    detail            TEXT,                 -- short freeform note (skip_reason, error text, etc.)
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempted_target
    ON attempted_fixes(target, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempted_outcome
    ON attempted_fixes(outcome, created_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    QUEUE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(QUEUE_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init() -> None:
    """Idempotent schema bootstrap. Called on import."""
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


init()


def record(
    *,
    target: str,
    action_kind: str,
    outcome: str,
    advisory_task_id: Optional[str] = None,
    handoff_artifact: Optional[str] = None,
    detail: Optional[str] = None,
) -> int:
    """Insert one attempt row. Returns the new row id."""
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO attempted_fixes
                (target, action_kind, outcome, advisory_task_id,
                 handoff_artifact, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target, action_kind, outcome, advisory_task_id,
                handoff_artifact, detail, _now(),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def recent_attempts(target: str, *, since_hours: int = 24) -> list[dict]:
    """All attempts against `target` in the last `since_hours`, newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, target, action_kind, outcome, advisory_task_id,
                   handoff_artifact, detail, created_at
              FROM attempted_fixes
             WHERE target = ?
               AND created_at >= ?
             ORDER BY created_at DESC
            """,
            (target, cutoff),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def should_skip(target: str, *, since_hours: int = 24) -> tuple[bool, str]:
    """Has this target been attempted recently with no success?

    Returns (skip, reason). Use in ingestors to suppress duplicate notes:

        skip, why = attempted_fixes.should_skip("cron:foo")
        if skip:
            print(f"[ingestor] {why} — not re-queuing")
            return

    Rule: skip if there's any "success" in window (problem solved) OR if
    there are >=2 unsuccessful attempts (we've already tried, stop spamming
    Claude; let a human take it). 0 or 1 unsuccessful attempts → re-queue.
    """
    attempts = recent_attempts(target, since_hours=since_hours)
    if not attempts:
        return False, "no prior attempts"
    successes = [a for a in attempts if a["outcome"] == "success"]
    if successes:
        latest = successes[0]["created_at"]
        return True, f"already fixed at {latest[:19]}"
    unsuccessful = [a for a in attempts if a["outcome"] in ("failed", "skipped", "notified")]
    if len(unsuccessful) >= 2:
        return True, (
            f"{len(unsuccessful)} unsuccessful attempts in last {since_hours}h "
            f"— stop re-queuing; escalate to human"
        )
    return False, f"{len(unsuccessful)} prior attempt(s), retry allowed"


def stats(*, since_hours: int = 24) -> dict:
    """Counts by outcome for the last window. For reporter / status commands."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT outcome, COUNT(*) c FROM attempted_fixes
                WHERE created_at >= ? GROUP BY outcome""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    return {r["outcome"]: r["c"] for r in rows}
