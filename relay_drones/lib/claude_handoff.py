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

# Per-action-kind tool scoping. The whole safety story of config_edit_proposed
# rests on `--allowed-tools Write(*.proposed)`: if Claude is technically
# capable of writing arbitrary files (which `--permission-mode bypassPermissions`
# allows), then "only write to .proposed siblings" is just prompt discipline —
# not enforcement. Locking the tool surface per kind makes the rule a real
# property of the system.
#
# Pattern grammar comes from Claude Code's --allowed-tools:
#   Tool                                — bare tool name allows everything
#   Tool(pattern)                       — pattern-restricted use
#   Bash(<cmd>:*)                       — only `<cmd>` invocations
#
# Notes:
# - `notify_human` is handled in worker.py directly (no Claude subprocess).
#   It is NOT in this map.
# - Keep these conservative. Adding capabilities is cheap; revoking them
#   after the cheap LLM gets used to having them is harder.
ACTION_KIND_TOOLS: dict[str, str] = {
    "tail_log": "Read Glob Bash(tail:*) Bash(head:*) Bash(wc:*) Bash(grep:*)",
    "config_inspect": "Read Glob Grep Bash(cat:*) Bash(grep:*)",
    "dns_repair": "Bash(dscacheutil:*) Bash(killall:*) Bash(scutil:*) Bash(ipconfig:*)",
    "service_health": "Bash(curl:*) Bash(ping:*) Bash(systemctl:status*) Bash(launchctl:list*) Bash(launchctl:print*)",
    "cron_disable": "Read Edit Bash(launchctl:bootout*) Bash(launchctl:disable*)",
    "restart_launchagent": "Bash(launchctl:*)",
    "config_edit_proposed": "Read Glob Grep Write(**/*.proposed)",
    # pr_open is the broadest by design — Claude needs to edit, commit, push.
    # The approval gate is the draft-PR review flow, not the tool surface.
    "pr_open": "Read Edit Write Glob Grep Bash(git:*) Bash(gh:pr*) Bash(gh:repo*) Bash(gh:auth*)",
}


def _tools_for(action_kind: str) -> Optional[str]:
    """Return the --allowed-tools spec for a kind, or None to omit the flag.

    Omission means "use the default tool set" (whatever the user's Claude
    Code config allows). That's the right behavior for action kinds we
    haven't explicitly scoped — falls back to the existing safety
    boundaries.
    """
    return ACTION_KIND_TOOLS.get(action_kind)


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
    repo_root: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
) -> dict:
    """Invoke headless Claude with the advisory + action brief.

    Caller is the worker. Guards are checked first; if any fail we return
    a skipped envelope (NOT an exception) so the worker can still log an
    advisory-only result.

    `repo_root` only matters for `action_kind="pr_open"` — sets the subprocess
    cwd so `git` and `gh` operate on the right repo. Other kinds ignore it.
    """
    skip = can_run(action_kind, depth)
    if skip:
        return _skipped(skip)

    model = model or CLAUDE_HANDOFF_MODEL
    timeout = timeout or CLAUDE_HANDOFF_TIMEOUT
    allowed_tools = _tools_for(action_kind)

    # Pull the scope hint into the prompt so the model can see the limits
    # it's been given — helps it write better refusals when an action would
    # exceed scope.
    scope_note = (
        f"Tools available to you for this action: {allowed_tools}.\n"
        if allowed_tools else ""
    )
    cwd_note = f"Working directory: {repo_root}\n" if repo_root else ""

    prompt = (
        f"You are executing an automated action proposed by an upstream "
        f"diagnostic worker in the relay-drones pipeline.\n"
        f"\n"
        f"Task ID: {task_id}\n"
        f"Action kind: {action_kind}\n"
        f"Handoff reason: {handoff_reason}\n"
        f"Handoff depth: {depth}\n"
        f"{cwd_note}"
        f"{scope_note}"
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
    if allowed_tools:
        cmd.extend(["--allowed-tools", allowed_tools])

    started = time.time()
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=timeout,
            cwd=repo_root,  # None falls back to caller's cwd
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
        "allowed_tools": allowed_tools,
        "repo_root": repo_root,
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
