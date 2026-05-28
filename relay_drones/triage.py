"""Triage — drain inbox/*.md into the task queue.

Front-matter is parsed for `priority`, `task_type`, and the reserved
`handoff_depth` key (propagated from ingestor-generated follow-ups).
Notes are moved to inbox/processed/ after submission so the next run
doesn't re-queue them.

    ---
    priority: 2
    task_type: research
    handoff_depth: 1
    ---
    Body text of the note...
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from relay_drones import queue
from relay_drones.config import INBOX, INBOX_PROCESSED, TRIAGE_WORKERS_ROLE
from relay_drones.lib import bb

AGENT_NAME = "relay-drones-triage"

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
KV = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$", re.M)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta = {k.lower(): v for k, v in KV.findall(m.group(1))}
    return meta, text[m.end():]


def _coerce_priority(raw: str | None) -> int:
    try:
        n = int(raw) if raw else 3
    except (ValueError, TypeError):
        n = 3
    return max(1, min(5, n))


def process(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)

    title = meta.get("title")
    if not title:
        h1 = re.search(r"^#\s+(.+)$", body, re.M)
        title = (
            h1.group(1).strip()
            if h1
            else path.stem.replace("_", " ").replace("-", " ")
        )
    title = title[:100]

    description = body.strip()
    priority = _coerce_priority(meta.get("priority"))
    task_type = meta.get("task_type") or meta.get("type")

    # Lift reserved frontmatter keys into the task's tags dict so
    # downstream code (worker, attempted_fixes) can read them back.
    #   - handoff_depth: enforces MAX_HANDOFF_DEPTH (worker._read_depth)
    #   - target:        attempted_fixes dedup key (worker writes outcome
    #                    rows; ingestors check before re-queuing)
    tags: dict = {}
    raw_depth = meta.get("handoff_depth")
    if raw_depth is not None:
        try:
            tags["handoff_depth"] = int(raw_depth)
        except (TypeError, ValueError):
            pass
    raw_target = meta.get("target")
    if raw_target:
        tags["target"] = str(raw_target).strip()

    task_id = queue.create_task(
        title=title,
        description=description,
        source="relay-drones:inbox",
        task_type=task_type,
        priority=priority,
        agent=TRIAGE_WORKERS_ROLE,
        tags=(tags or None),
    )
    return {
        "task_id": task_id,
        "agent": TRIAGE_WORKERS_ROLE,
        "title": title,
        "file": path.name,
        "tags": tags,
    }


def main() -> int:
    INBOX_PROCESSED.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    bb.set_presence(AGENT_NAME, "online", "scanning inbox")

    queued = []
    skipped = []
    for p in sorted(INBOX.glob("*.md")):
        if p.parent != INBOX:
            continue
        try:
            info = process(p)
            queued.append(info)
            shutil.move(str(p), str(INBOX_PROCESSED / p.name))
            print(f"queued {info['task_id']} [{info['agent']}] {info['title']}")
        except Exception as e:
            skipped.append((p.name, str(e)))
            print(f"skip {p.name}: {e}", file=sys.stderr)

    if queued:
        bb.post_message(
            AGENT_NAME,
            f"triage queued {len(queued)} task(s): "
            + ", ".join(f"{q['task_id']}[{q['agent']}]" for q in queued),
        )
    bb.set_presence(
        AGENT_NAME,
        "idle",
        f"last run queued={len(queued)} skipped={len(skipped)}",
    )
    return 0 if not skipped else 1


if __name__ == "__main__":
    sys.exit(main())
