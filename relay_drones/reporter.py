"""Reporter — on-demand digest of what the loop produced.

Reads completed + failed tasks since `--since` (default 24h), pairs each
with its results/*.json artifact, and emits markdown on stdout.

    relay-drones report                       # last 24h
    relay-drones report --since 6h            # last 6h
    relay-drones report --since 2026-05-22    # since that date
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from relay_drones.config import QUEUE_DB, RESULTS


def _parse_since(s: str) -> datetime:
    s = s.strip()
    if s.endswith("h") and s[:-1].isdigit():
        return datetime.now(timezone.utc) - timedelta(hours=int(s[:-1]))
    if s.endswith("d") and s[:-1].isdigit():
        return datetime.now(timezone.utc) - timedelta(days=int(s[:-1]))
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"unrecognized --since value: {s!r}")


def _fetch(since: datetime) -> list[dict]:
    conn = sqlite3.connect(str(QUEUE_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, title, agent, status, priority, source, created_at,
                  completed_at, result, error
             FROM tasks
            WHERE source = 'relay-drones:inbox'
              AND updated_at >= ?
            ORDER BY updated_at DESC""",
        (since.isoformat(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _attach_artifact(task: dict) -> Optional[dict]:
    candidate = RESULTS / f"{task.get('agent')}__{task['id']}.json"
    if candidate.exists():
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    return None


def render(tasks: list[dict]) -> str:
    if not tasks:
        return "# relay-drones digest\n\n_no tasks in window._\n"

    by_status: dict[str, list[dict]] = {}
    for t in tasks:
        by_status.setdefault(t["status"], []).append(t)

    out = ["# relay-drones digest", ""]
    out.append(
        f"window: {len(tasks)} task(s)  "
        f"({', '.join(f'{k}={len(v)}' for k, v in sorted(by_status.items()))})"
    )
    out.append("")

    for status in ("completed", "failed", "in_progress", "submitted", "assigned"):
        items = by_status.get(status, [])
        if not items:
            continue
        out.append(f"## {status} ({len(items)})")
        out.append("")
        for t in items:
            out.append(f"### [{t['agent'] or '?'}] {t['title']}  `{t['id']}`")
            res = None
            if t.get("result"):
                try:
                    res = json.loads(t["result"])
                except (json.JSONDecodeError, TypeError):
                    res = {"snippet": t["result"]}
            if res:
                snippet = (res.get("snippet") or "").strip()
                if snippet:
                    out.append("")
                    for line in snippet.splitlines()[:20]:
                        out.append(f"> {line}")
                if res.get("handoff_provider"):
                    out.append("")
                    out.append(
                        f"_handoff: {res['handoff_provider']} "
                        f"(skipped={res.get('handoff_skipped', False)})_"
                    )
            if t.get("error"):
                out.append("")
                out.append(f"_error: {t['error'][:300]}_")
            artifact = _attach_artifact(t)
            if artifact and artifact.get("artifact_path"):
                out.append("")
                out.append(f"_artifact: {artifact['artifact_path']}_")
            out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since",
        default="24h",
        help="window: 6h, 2d, or ISO date (default 24h)",
    )
    args = parser.parse_args(argv)
    since = _parse_since(args.since)
    tasks = _fetch(since)
    print(render(tasks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
