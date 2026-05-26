"""Tiny SQLite task queue.

Replaces the heavier task_dispatcher used in the author's private setup.
Public API matches what worker.py + triage.py need: create_task,
complete_task, fail_task, get_task, and the schema claim.py expects.

Design constraints:
- Standard library only. No deps.
- One file, one table, one writer at a time (SQLite serializes via WAL).
- Tags column is JSON text so downstream knobs (handoff_depth, custom
  routing keys) ride along without schema changes.
- IDs are 12-char hex slices of UUIDv4 — short enough to paste, long
  enough to avoid collisions at the volumes this is designed for.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from relay_drones.config import QUEUE_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT,
    source        TEXT,
    agent         TEXT,
    status        TEXT NOT NULL,
    priority      INTEGER DEFAULT 3,
    task_type     TEXT,
    parent_id     TEXT,
    tags          TEXT,
    result        TEXT,
    error         TEXT,
    created_at    TEXT,
    assigned_at   TEXT,
    started_at    TEXT,
    completed_at  TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_agent_status
    ON tasks(agent, status, priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_tasks_status_updated
    ON tasks(status, updated_at DESC);
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
    """Create the schema if it doesn't exist. Safe to call repeatedly."""
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# Initialize on import so callers don't need to remember.
init()


def create_task(
    title: str,
    description: Optional[str] = None,
    source: str = "cli",
    task_type: Optional[str] = None,
    priority: int = 3,
    parent_id: Optional[str] = None,
    tags: Optional[dict] = None,
    agent: Optional[str] = None,
) -> str:
    """Insert a task and return its 12-char hex id."""
    now = _now()
    task_id = uuid.uuid4().hex[:12]
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO tasks
                (id, title, description, source, agent, status, priority,
                 task_type, parent_id, tags,
                 created_at, assigned_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                title,
                description,
                source,
                agent,
                "assigned" if agent else "submitted",
                priority,
                task_type,
                parent_id,
                json.dumps(tags) if tags else None,
                now,
                now if agent else None,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return task_id


def complete_task(task_id: str, *, result: Optional[str] = None) -> None:
    now = _now()
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE tasks
               SET status='completed',
                   result=?,
                   completed_at=?,
                   updated_at=?
             WHERE id=?
            """,
            (result, now, now, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def fail_task(task_id: str, *, error: Optional[str] = None) -> None:
    now = _now()
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE tasks
               SET status='failed',
                   error=?,
                   completed_at=?,
                   updated_at=?
             WHERE id=?
            """,
            (error, now, now, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_task(task_id: str) -> Optional[dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    task = dict(row)
    if task.get("tags"):
        try:
            task["tags"] = json.loads(task["tags"])
        except (TypeError, json.JSONDecodeError):
            pass
    return task


def status_counts(agent: Optional[str] = None) -> dict[str, int]:
    """Return {status: count}. Useful for status/reporter commands."""
    conn = _connect()
    try:
        if agent:
            rows = conn.execute(
                "SELECT status, COUNT(*) c FROM tasks WHERE agent=? GROUP BY status",
                (agent,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT status, COUNT(*) c FROM tasks GROUP BY status"
            ).fetchall()
    finally:
        conn.close()
    return {r["status"]: r["c"] for r in rows}
