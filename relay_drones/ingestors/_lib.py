"""Shared helpers for ingestors — note writing, state, etc.

Every ingestor produces zero or more .md files in INBOX. Filenames have a
UTC timestamp prefix so they sort chronologically; the slug is built from
the title.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from relay_drones.config import INBOX, ROOT

STATE_DIR = ROOT / "relay_drones" / "ingestors" / ".state"


def _slug(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:max_len] or "untitled"


def write_note(
    title: str,
    body: str,
    *,
    priority: int = 3,
    task_type: str = "ingested",
    source: str = "ingestor",
    extra_frontmatter: dict | None = None,
) -> Path:
    """Drop a markdown note into INBOX. Returns the path written."""
    INBOX.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = INBOX / f"{stamp}__{source}__{_slug(title)}.md"
    fm = {"priority": priority, "task_type": task_type, "source": source}
    if extra_frontmatter:
        fm.update(extra_frontmatter)
    fm_lines = ["---"]
    for k, v in fm.items():
        fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    content = "\n".join(fm_lines) + f"\n\n# {title}\n\n{body.strip()}\n"
    path.write_text(content, encoding="utf-8")
    return path


def load_state(ingestor_name: str) -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{ingestor_name}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(ingestor_name: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{ingestor_name}.json"
    p.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
