"""daily_prompt — one note per day forcing a focused planning reflection.

Idempotent: state file tracks the last UTC date a note was written, so
re-running on the same day is a no-op.

To customize the prompt, set RELAY_DRONES_DAILY_PROMPT_FILE to a path
holding your own prompt body. Otherwise the default below is used.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from relay_drones.ingestors._lib import load_state, save_state, write_note

INGESTOR = "daily_prompt"

DEFAULT_PROMPT_BODY = """\
Today's daily reflection. Propose the 3 highest-leverage things to work on
today, ranked. Each must be specific (names a file, system, metric, or
pipeline), small (fits in one worker task), and reasoned (one sentence on
*why* it's top of the list, not generic best-practice).

Return three numbered items in this shape:

  1. **<title>** — <why this beats the rest, one sentence>.
     - Concrete next step: <what to do first>.
     - Verification: <how you'd know it's done>.

If nothing stands out, say so honestly and return zero items rather than
padding.
"""


def _load_prompt() -> str:
    path = os.environ.get("RELAY_DRONES_DAILY_PROMPT_FILE")
    if path:
        p = Path(path).expanduser()
        if p.exists():
            return p.read_text(encoding="utf-8")
        print(
            f"[daily_prompt] RELAY_DRONES_DAILY_PROMPT_FILE={path} not found; "
            "falling back to default",
            file=sys.stderr,
        )
    return DEFAULT_PROMPT_BODY


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force", action="store_true",
        help="write the note even if today's already exists",
    )
    args = parser.parse_args(argv)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = load_state(INGESTOR)
    if state.get("last_date") == today and not args.force:
        print(f"[daily_prompt] already ran today ({today}) — skip")
        return 0

    title = f"Daily reflection: top 3 highest-leverage tasks for {today}"
    path = write_note(title, _load_prompt(), priority=2, source="daily_prompt")
    print(f"[daily_prompt] wrote {path.name}")

    state["last_date"] = today
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(INGESTOR, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
