"""Worker — pulls tasks for one role from the queue and runs them.

One worker process serves one role. Several can run in parallel (each gets
its own process); claim.claim_next is race-safe so they won't double-process.

The structured-output tail on the model response (`{"actionable": true, ...}`)
drives the optional headless-Claude escalation path. See:
    - docs/architecture.md
    - relay_drones.lib.claude_handoff
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from relay_drones import queue
from relay_drones.config import (
    CLAUDE_ACTION_ALLOWLIST,
    POLL_INTERVAL,
    MAX_PROMPT,
    ASK_TIMEOUT,
    RESULTS,
    ROLES,
)
from relay_drones.lib import bb, claude_handoff
from relay_drones.lib.claim import claim_next
from relay_drones.lib.openrouter import (
    ask as or_ask,
    ask_with_fallback as or_ask_fallback,
    OpenRouterError,
)

_shutdown = False


def _on_signal(signum, _frame):
    global _shutdown
    _shutdown = True
    print(f"[worker] signal {signum} — finishing current task then exiting", flush=True)


def _build_prompt(task: dict, role_cfg: dict) -> str:
    """Compose a single prompt from a task row.

    The closing JSON-tail contract is what enables the headless-Claude
    handoff (see _extract_handoff + lib/claude_handoff): the cheap model
    proposes whether and how to escalate; the worker enforces the gates.
    """
    role_desc = role_cfg.get("description", "")
    body = task.get("description") or ""
    if len(body) > MAX_PROMPT:
        body = body[:MAX_PROMPT] + "\n\n[…truncated…]"
    allowlist = sorted(CLAUDE_ACTION_ALLOWLIST)
    return (
        f"You are an autonomous worker in a multi-agent loop.\n"
        f"Role: {role_desc}\n"
        f"\n"
        f"Task title: {task['title']}\n"
        f"Task body:\n{body}\n"
        f"\n"
        f"Produce the most useful response you can. Be concrete and decisive. "
        f"If the task is a proposal request, return a numbered list of "
        f"concrete next steps. If it's research, return a structured brief "
        f"with sources where possible. If it's code, return the code change "
        f"(diff or full file) plus a one-paragraph rationale. Keep it tight.\n"
        f"\n"
        f"---\n"
        f"\n"
        f"Then, on the FINAL line of your response, emit a JSON object on a "
        f"single line that decides whether this diagnosis should be escalated "
        f"to a Claude executor with tool access. Schema:\n"
        f"\n"
        f"  {{\"actionable\": bool, \"action_kind\": str|null, "
        f"\"action_brief\": str|null, \"handoff_reason\": str|null}}\n"
        f"\n"
        f"Set actionable=true ONLY when the fix needs filesystem/shell access "
        f"AND fits one of these action_kind values: {allowlist}. "
        f"Otherwise actionable=false. action_brief, when set, should be a "
        f"3-6 line concrete instruction Claude can execute (commands, file "
        f"paths, expected outcome). handoff_reason is one short sentence. "
        f"If unsure, default to actionable=false — the advisory still gets "
        f"recorded. Output exactly one JSON object on the final line, no "
        f"code fences."
    )


_JSON_TAIL_RE = re.compile(r"\{[^{}]*\"actionable\"[^{}]*\}\s*\Z", re.DOTALL)


def _extract_handoff(raw: str) -> tuple[str, dict | None]:
    """Split the JSON tail from the body.

    The worker prompt asks the model to put a single-line JSON object on the
    final line. Free-fallback models often don't obey perfectly (extra prose,
    wrapped fences). We try the strict trailing match first, then a relaxed
    scan-from-end. Returns (body_without_tail, parsed_tail_or_None).
    """
    if not raw:
        return raw, None
    body = raw.rstrip()
    m = _JSON_TAIL_RE.search(body)
    if m:
        try:
            parsed = json.loads(m.group(0))
            return body[: m.start()].rstrip(), parsed
        except json.JSONDecodeError:
            pass
    # Relaxed: walk back, find the last balanced {…}, try parsing it.
    depth = 0
    end = None
    for i in range(len(body) - 1, -1, -1):
        c = body[i]
        if c == "}":
            if end is None:
                end = i
            depth += 1
        elif c == "{":
            depth -= 1
            if depth == 0 and end is not None:
                candidate = body[i : end + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    end = None
                    continue
                if isinstance(parsed, dict) and "actionable" in parsed:
                    return body[:i].rstrip(), parsed
                end = None
    return body, None


def _read_depth(task: dict) -> int:
    """Pull handoff_depth from the task's tags (JSON-encoded), default 0."""
    tags = task.get("tags")
    if not tags:
        return 0
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            return 0
    if isinstance(tags, dict):
        try:
            return int(tags.get("handoff_depth", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def _dump_result(
    task_id: str,
    role: str,
    ask_result: dict,
    tail: dict | None,
    handoff_result: dict | None,
) -> Path:
    """Persist the full artifact reference + raw output for later digesting."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{role}__{task_id}.json"
    payload = {
        "task_id": task_id,
        "role": role,
        "provider": ask_result["provider"],
        "artifact_path": ask_result["artifact_path"],
        "raw": ask_result["raw"],
        "actionable_tail": tail,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if handoff_result is not None:
        payload["handoff"] = {
            "provider": handoff_result.get("provider"),
            "artifact_path": handoff_result.get("artifact_path"),
            "raw": handoff_result.get("raw"),
            "skipped": handoff_result.get("skipped", False),
            "skip_reason": handoff_result.get("skip_reason"),
            "meta": handoff_result.get("meta"),
        }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def run_one(role: str, role_cfg: dict) -> bool:
    """Pull one task for the role and process it. Returns True if one was run."""
    task = claim_next(role)
    if not task:
        return False

    task_id = task["id"]
    label = role_cfg.get("label", f"relay-drones-{role}")
    bb.set_presence(label, "busy", f"running task {task_id}: {task['title'][:80]}")

    try:
        prompt = _build_prompt(task, role_cfg)
        model = role_cfg.get("model")
        if not model:
            raise OpenRouterError(f"role {role!r} has no 'model' configured")
        if isinstance(model, (list, tuple)):
            ask_result = or_ask_fallback(list(model), prompt, timeout=ASK_TIMEOUT)
        else:
            ask_result = or_ask(model, prompt, timeout=ASK_TIMEOUT)

        body, tail = _extract_handoff(ask_result["raw"])
        ask_result["raw"] = body

        handoff_result = None
        if tail and tail.get("actionable") is True:
            depth = _read_depth(task)
            kind = (tail.get("action_kind") or "").strip()
            brief = (tail.get("action_brief") or "").strip()
            reason = (tail.get("handoff_reason") or "").strip()
            if not kind or not brief:
                handoff_result = {
                    "skipped": True,
                    "skip_reason": "tail missing action_kind or action_brief",
                    "provider": "claude-code:n/a",
                    "raw": "",
                    "artifact_path": "",
                }
                bb.post_message(
                    label,
                    f"handoff-skip {task_id} [{role}]: malformed tail "
                    f"(kind={kind!r}, brief_len={len(brief)})",
                )
            else:
                try:
                    handoff_result = claude_handoff.run(
                        advisory=body,
                        action_kind=kind,
                        action_brief=brief,
                        handoff_reason=reason or "(no reason given)",
                        depth=depth,
                        task_id=task_id,
                    )
                    if handoff_result.get("skipped"):
                        bb.post_message(
                            label,
                            f"handoff-skip {task_id} [{role}] kind={kind}: "
                            f"{handoff_result.get('skip_reason')}",
                        )
                    else:
                        bb.post_message(
                            label,
                            f"handoff-run {task_id} [{role}] kind={kind} "
                            f"depth={depth} (budget "
                            f"{handoff_result['meta']['budget_used_today']})",
                        )
                except claude_handoff.HandoffError as e:
                    handoff_result = {
                        "skipped": True,
                        "skip_reason": f"HandoffError: {e}",
                        "provider": f"claude-code:{kind}",
                        "raw": "",
                        "artifact_path": "",
                    }
                    bb.post_message(
                        label,
                        f"handoff-fail {task_id} [{role}] kind={kind}: {e}",
                    )

        result_path = _dump_result(task_id, role, ask_result, tail, handoff_result)
        snippet = ask_result["raw"][:1200]
        dispatcher_result = {
            "snippet": snippet,
            "artifact": ask_result["artifact_path"],
            "result_json": str(result_path),
            "provider": ask_result["provider"],
        }
        if handoff_result is not None:
            dispatcher_result["handoff_provider"] = handoff_result.get("provider")
            dispatcher_result["handoff_skipped"] = handoff_result.get("skipped", False)
            if handoff_result.get("artifact_path"):
                dispatcher_result["handoff_artifact"] = handoff_result["artifact_path"]
        queue.complete_task(task_id, result=json.dumps(dispatcher_result))
        bb.post_message(
            label,
            f"completed {task_id} [{role}] — {task['title'][:60]} "
            f"(via {ask_result['provider']})",
        )
        print(
            f"[worker:{role}] done {task_id}  artifact={ask_result['artifact_path']}",
            flush=True,
        )
    except OpenRouterError as e:
        queue.fail_task(task_id, error=f"OpenRouterError: {e}")
        bb.post_message(label, f"failed {task_id} [{role}]: {e}")
        print(f"[worker:{role}] FAIL {task_id}: {e}", flush=True)
    except Exception as e:
        queue.fail_task(
            task_id, error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[:1500]}"
        )
        bb.post_message(label, f"crashed {task_id} [{role}]: {type(e).__name__}: {e}")
        print(f"[worker:{role}] CRASH {task_id}: {e}", flush=True)
        traceback.print_exc()
    finally:
        bb.set_presence(label, "online", "idle")

    return True


def drain(role: str, role_cfg: dict) -> int:
    n = 0
    while run_one(role, role_cfg):
        n += 1
        if _shutdown:
            break
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="relay-drones worker")
    parser.add_argument("role", help="role to claim tasks for (see config.ROLES)")
    parser.add_argument("--once", action="store_true",
                        help="process at most one task, then exit")
    parser.add_argument("--no-loop", action="store_true",
                        help="drain the current queue once, then exit")
    args = parser.parse_args(argv)

    if args.role not in ROLES:
        print(f"unknown role {args.role!r}. valid: {sorted(ROLES)}", file=sys.stderr)
        return 2
    role_cfg = dict(ROLES[args.role])
    role_cfg["label"] = f"{role_cfg['label']}-{os.getpid()}"
    label = role_cfg["label"]

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    bb.set_presence(label, "online", "ready")
    print(
        f"[worker:{args.role}] up — provider={role_cfg['provider']} "
        f"poll={POLL_INTERVAL}s ask_timeout={ASK_TIMEOUT}s",
        flush=True,
    )

    if args.once:
        run_one(args.role, role_cfg)
        return 0
    if args.no_loop:
        drain(args.role, role_cfg)
        return 0

    try:
        while not _shutdown:
            processed = drain(args.role, role_cfg)
            if processed == 0:
                for _ in range(POLL_INTERVAL):
                    if _shutdown:
                        break
                    time.sleep(1)
    finally:
        bb.set_presence(label, "offline", "worker exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
