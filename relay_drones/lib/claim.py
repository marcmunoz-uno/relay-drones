"""Race-safe task claim for parallel workers.

Why this exists:
    A naive SELECT-then-UPDATE has a window where N parallel workers can
    each read the same row, each think they've "claimed" it, and each
    call the model — wasted spend and duplicate work.

    One atomic statement closes that window:

        UPDATE tasks
           SET status='in_progress', started_at=?, updated_at=?
         WHERE id = (SELECT id FROM tasks
                      WHERE agent=? AND status IN ('submitted','assigned')
                      ORDER BY priority DESC, created_at ASC
                      LIMIT 1)
        RETURNING *

    SQLite serializes writes (one WAL writer at a time), so exactly one
    worker wins the row. The losing worker gets None and goes back to
    polling.

    Requires SQLite ≥ 3.35 for `RETURNING`. macOS Python 3.9+ ships 3.43.
    Linux distros vary — check `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from relay_drones.config import QUEUE_DB


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(QUEUE_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def claim_next(role: str) -> Optional[dict]:
    """Atomically claim the highest-priority pending task for `role`.

    Returns the claimed task as a dict, or None if no work was available.
    Side effect: row's status → 'in_progress', started_at set.
    """
    now = _now()
    conn = _connect()
    try:
        row = conn.execute(
            """
            UPDATE tasks
               SET status = 'in_progress',
                   started_at = ?,
                   updated_at = ?
             WHERE id = (
                 SELECT id FROM tasks
                  WHERE agent = ?
                    AND status IN ('submitted', 'assigned')
                  ORDER BY priority DESC, created_at ASC
                  LIMIT 1
             )
            RETURNING *
            """,
            (now, now, role),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    if row is None:
        return None
    task = dict(row)
    if task.get("tags"):
        try:
            task["tags"] = json.loads(task["tags"])
        except (TypeError, json.JSONDecodeError):
            pass
    return task
