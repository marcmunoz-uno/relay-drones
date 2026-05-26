"""Headless-Claude action handoff.

When an OpenRouter advisory is tagged `actionable`, the worker calls into
this module instead of just dumping the advisory to disk. We shell out to
`claude -p` so the action runs against the user's Claude Max subscription —
NOT the API. The key trick: ANTHROPIC_API_KEY must be stripped from the
child env, or Claude Code prefers the API key and bills per-token.

See docs/oauth-trick.md for the full story.

Three guardrails layered on top:

  1. **Daily budget counter** — persisted under config.HANDOFF_STATE. When
     the day's count hits MAX_CLAUDE_RUNS_PER_DAY we short-circuit back
     to advisory-only. Resets at UTC midnight.
  2. **Depth guard** — caller passes `depth` (read from the originating
     task's tags). Past MAX_HANDOFF_DEPTH we refuse, breaking any
     self_prompt → action → self_prompt runaway.
  3. **Action-kind allowlist** — the cheap LLM proposes an `action_kind`
     string; only values in CLAUDE_ACTION_ALLOWLIST escalate. Novel kinds
     stay advisory. Mitigates prompt injection from log content that
     ingestors might scrape.

Return envelope mirrors openrouter.ask() so the worker's result-dump path
stays uniform.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from relay_drones.config import (
    ARTIFACTS,
    CLAUDE_ACTION_ALLOWLIST,
    CLAUDE_HANDOFF_MODEL,
    CLAUDE_HANDOFF_TIMEOUT,
    HANDOFF_STATE,
    MAX_CLAUDE_RUNS_PER_DAY,
    MAX_HANDOFF_DEPTH,
)


class HandoffError(RuntimeError):
    """Raised when the handoff itself fails (subprocess crash, JSON parse, etc).

    Guard-rail refusals are NOT errors — they return a skipped=True envelope
    so the worker can still record an advisory.
    """


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_budget() -> dict:
    if not HANDOFF_STATE.exists():
        return {"day": _today_utc(), "count": 0}
    try:
        data = json.loads(HANDOFF_STATE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"day": _today_utc(), "count": 0}
    if data.get("day") != _today_utc():
        return {"day": _today_utc(), "count": 0}
    return data


def _save_budget(data: dict) -> None:
    HANDOFF_STATE.parent.mkdir(parents=True, exist_ok=True)
    HANDOFF_STATE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def budget_status() -> tuple[int, int]:
    """Return (used_today, cap). Useful for status commands and tests."""
    data = _load_budget()
    return data.get("count", 0), MAX_CLAUDE_RUNS_PER_DAY


def _persist_artifact(prompt: str, raw: str, model: str, meta: dict) -> Path:
    """Drop a markdown trace alongside the openrouter artifacts."""
    out_dir = ARTIFACTS / "claude-handoff"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")[:-4] + "Z"
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower())[:60].strip("-") or "untitled"
    path = out_dir / f"{model}-{slug}-{stamp}.md"
    body = [
        "# claude-code handoff artifact",
        "",
        "- Provider: claude-code (Max subscription)",
        f"- Model: {model}",
        f"- Created at: {datetime.now(timezone.utc).isoformat()}",
        f"- Action kind: {meta.get('action_kind', '?')}",
        f"- Depth: {meta.get('depth', 0)}",
        "",
        "## Original advisory + action brief",
        "",
        prompt,
        "",
        "## Claude's response",
        "",
        "```text",
        raw,
        "```",
        "",
        "## Subprocess meta",
        "",
        "```json",
        json.dumps(meta, indent=2),
        "```",
        "",
    ]
    path.write_text("\n".join(body), encoding="utf-8")
    return path


def _skipped(reason: str) -> dict:
    return {
        "raw": "",
        "artifact_path": "",
        "provider": f"claude-code:{CLAUDE_HANDOFF_MODEL}",
        "exit_code": 0,
        "skipped": True,
        "skip_reason": reason,
    }


def can_run(action_kind: str, depth: int) -> Optional[str]:
    """Return None if the handoff is allowed, else a short skip reason."""
    if action_kind not in CLAUDE_ACTION_ALLOWLIST:
        return (
            f"action_kind {action_kind!r} not in allowlist "
            f"({sorted(CLAUDE_ACTION_ALLOWLIST)})"
        )
    if depth >= MAX_HANDOFF_DEPTH:
        return f"handoff depth {depth} >= cap {MAX_HANDOFF_DEPTH}"
    used, cap = budget_status()
    if used >= cap:
        return f"daily budget exhausted ({used}/{cap})"
    return None


def run(
    *,
    advisory: str,
    action_kind: str,
    action_brief: str,
    handoff_reason: str,
    depth: int,
    task_id: str,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
) -> dict:
    """Invoke headless Claude with the advisory + action brief.

    Caller is the worker. Guards are checked first; if any fail we return
    a skipped envelope (NOT an exception) so the worker can still log an
    advisory-only result.
    """
    skip = can_run(action_kind, depth)
    if skip:
        return _skipped(skip)

    model = model or CLAUDE_HANDOFF_MODEL
    timeout = timeout or CLAUDE_HANDOFF_TIMEOUT

    prompt = (
        f"You are executing an automated action proposed by an upstream "
        f"diagnostic worker in the relay-drones pipeline.\n"
        f"\n"
        f"Task ID: {task_id}\n"
        f"Action kind: {action_kind}\n"
        f"Handoff reason: {handoff_reason}\n"
        f"Handoff depth: {depth}\n"
        f"\n"
        f"--- Upstream advisory ---\n"
        f"{advisory}\n"
        f"\n"
        f"--- Action brief from the diagnostic worker ---\n"
        f"{action_brief}\n"
        f"\n"
        f"Perform the smallest concrete action that resolves the diagnosis. "
        f"Stay within the action kind {action_kind!r}. If the brief proposes "
        f"a destructive change beyond that kind, refuse and explain. After "
        f"acting (or refusing), summarize what you did in 3-6 lines so the "
        f"loop has a record."
    )

    # CRITICAL: strip ANTHROPIC_API_KEY so Claude Code uses the OAuth
    # (Max subscription) creds in ~/.claude/.credentials.json instead of
    # the API key. This is the whole point — see docs/oauth-trick.md.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    cmd = [
        "claude",
        "-p", prompt,
        "--model", model,
        "--permission-mode", "bypassPermissions",
        "--output-format", "json",
        "--no-session-persistence",
    ]

    started = time.time()
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise HandoffError(
            f"claude -p timed out after {timeout}s (kind={action_kind})"
        ) from e
    except FileNotFoundError as e:
        raise HandoffError(
            f"claude CLI not found on PATH: {e}. "
            "Install Claude Code from https://github.com/anthropics/claude-code "
            "and run `claude` once interactively to set up OAuth."
        ) from e
    elapsed = time.time() - started

    if result.returncode != 0:
        raise HandoffError(
            f"claude -p exit={result.returncode} stderr={result.stderr[:500]}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise HandoffError(
            f"claude -p produced non-JSON stdout: {e}; "
            f"first 500 chars: {result.stdout[:500]}"
        ) from e

    if payload.get("is_error"):
        raise HandoffError(
            f"claude reported error: subtype={payload.get('subtype')} "
            f"status={payload.get('api_error_status')}"
        )

    raw = payload.get("result") or ""
    if not raw.strip():
        raise HandoffError("claude returned empty result")

    # Increment the daily budget AFTER a successful call so failed
    # invocations don't burn the cap.
    data = _load_budget()
    data["count"] = data.get("count", 0) + 1
    _save_budget(data)

    meta = {
        "action_kind": action_kind,
        "depth": depth,
        "elapsed_seconds": round(elapsed, 1),
        "session_id": payload.get("session_id"),
        "total_cost_usd": payload.get("total_cost_usd"),
        # ^ informational only. On the Max subscription you are not billed
        #   per-call; this field is the token cost the same prompt would
        #   have been at API list price.
        "usage": payload.get("usage"),
        "model_usage": payload.get("modelUsage", {}),
        "budget_used_today": data["count"],
    }
    artifact = _persist_artifact(prompt, raw, model, meta)

    return {
        "raw": raw,
        "artifact_path": str(artifact),
        "provider": f"claude-code:{model}",
        "exit_code": 0,
        "skipped": False,
        "skip_reason": None,
        "meta": meta,
    }
