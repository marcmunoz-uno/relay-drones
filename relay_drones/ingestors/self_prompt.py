"""self_prompt — reflect on recent completions, propose follow-ups.

Closes the loop on itself: the system reflects on its own output and
schedules its own next work. Runs every 30 min (or whatever interval your
LaunchAgent / systemd timer / cron uses).

Limits keep cost + recursion bounded:
- Looks at completed tasks from the last `--since` window (default 6h).
- Skips tasks we've already reflected on (state file).
- Caps generated notes per run at 3.
- Skips entirely if the inbox already has 5+ pending notes.

If a source task triggered a Claude handoff, follow-ups inherit
`handoff_depth = source_depth + 1` so the chain bounds at MAX_HANDOFF_DEPTH.
See docs/safety-rails.md.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from relay_drones.config import INBOX, QUEUE_DB
from relay_drones.lib.openrouter import ask_with_fallback, OpenRouterError

from relay_drones.ingestors._lib import load_state, save_state, write_note

INGESTOR = "self_prompt"
MAX_NEW_NOTES = 3
INBOX_BACKPRESSURE = 5

# Cheap-first model chain; tune via env vars if you want different defaults.
DEFAULT_MODELS = [
    "openai/gpt-4o-mini",
    "qwen/qwen3-coder:free",
    "deepseek/deepseek-v4-flash:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]


def _pending_inbox_count() -> int:
    return sum(1 for p in INBOX.glob("*.md") if p.parent == INBOX)


def _recent_completions(since_hours: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    conn = sqlite3.connect(str(QUEUE_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, title, agent, status, completed_at, result, tags
             FROM tasks
            WHERE source = 'relay-drones:inbox'
              AND status = 'completed'
              AND completed_at >= ?
            ORDER BY completed_at DESC
            LIMIT 25""",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _followup_depth(completions: list[dict]) -> int:
    """Worst-case depth across source completions, +1 if any handed off.

    The model proposes follow-ups across multiple sources at once with no
    1:1 mapping, so the safe assumption is: if ANY source triggered a
    handoff, the chain has advanced one level.
    """
    worst = 0
    for r in completions:
        src_depth = 0
        if r.get("tags"):
            try:
                tags = json.loads(r["tags"]) if isinstance(r["tags"], str) else r["tags"]
                src_depth = int(tags.get("handoff_depth", 0) or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        handoff_fired = False
        if r.get("result"):
            try:
                res = json.loads(r["result"])
                handoff_fired = (
                    bool(res.get("handoff_provider"))
                    and not res.get("handoff_skipped")
                )
            except (TypeError, json.JSONDecodeError):
                pass
        worst = max(worst, src_depth + (1 if handoff_fired else 0))
    return worst


def _summarize_result(r: dict) -> str:
    try:
        parsed = json.loads(r["result"] or "")
        snippet = parsed.get("snippet") or r["result"]
    except (json.JSONDecodeError, TypeError):
        snippet = r["result"] or ""
    return snippet[:600]


def _build_prompt(completions: list[dict]) -> str:
    lines = [
        "You are the reflection layer of an autonomous agent loop. "
        "Below are recent tasks the worker pool completed. Identify follow-ups "
        "that *open up* from this work — not generic next steps, not "
        "platitudes. A good follow-up names a specific file, system, "
        "metric, or external source, and is small enough that a single "
        "worker can finish it in one shot.",
        "",
        "Return STRICT JSON only — no prose, no markdown fences. Schema:",
        '  {"follow_ups": [{"title": "...", "body": "...", "priority": 1-5}, ...]}',
        f"At most {MAX_NEW_NOTES} entries. If nothing is worth queuing, "
        'return {"follow_ups": []}.',
        "",
        "Recent completions:",
    ]
    for r in completions:
        lines.append(f"\n--- {r['id']} [{r['agent']}] {r['title']}")
        lines.append(_summarize_result(r))
    return "\n".join(lines)


def _parse_response(raw: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out = data.get("follow_ups") or []
    if not isinstance(out, list):
        return []
    return [x for x in out if isinstance(x, dict) and x.get("title")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-hours", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if _pending_inbox_count() >= INBOX_BACKPRESSURE:
        print(f"[self_prompt] inbox has {_pending_inbox_count()} pending — skip")
        return 0

    state = load_state(INGESTOR)
    seen: set[str] = set(state.get("reflected_task_ids") or [])

    completions = [
        r for r in _recent_completions(args.since_hours) if r["id"] not in seen
    ]
    if not completions:
        print("[self_prompt] no new completions to reflect on")
        return 0

    print(f"[self_prompt] reflecting on {len(completions)} completion(s)")
    prompt = _build_prompt(completions)
    try:
        resp = ask_with_fallback(DEFAULT_MODELS, prompt, timeout=180)
    except OpenRouterError as e:
        print(f"[self_prompt] openrouter failed: {e}", file=sys.stderr)
        return 1

    follow_ups = _parse_response(resp["raw"])[:MAX_NEW_NOTES]
    depth = _followup_depth(completions)
    print(
        f"[self_prompt] model proposed {len(follow_ups)} follow-up(s); "
        f"follow-up handoff_depth={depth}"
    )

    written = []
    for fu in follow_ups:
        title = str(fu["title"])[:100]
        body = str(fu.get("body") or "")
        try:
            priority = int(fu.get("priority") or 3)
        except (TypeError, ValueError):
            priority = 3
        if args.dry_run:
            print(f"  [dry-run] would write: {title} (depth={depth})")
            continue
        extra = {"handoff_depth": depth} if depth > 0 else None
        path = write_note(
            title, body, priority=priority, source="self_prompt",
            extra_frontmatter=extra,
        )
        written.append(path)
        print(f"  wrote {path.name}")

    if not args.dry_run:
        state["reflected_task_ids"] = list(
            seen | {r["id"] for r in completions}
        )[-200:]
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["last_proposed"] = len(follow_ups)
        save_state(INGESTOR, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
