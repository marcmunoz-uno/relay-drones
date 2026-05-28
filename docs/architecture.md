# Architecture

A two-tier agent system. Cheap LLMs reason continuously; an expensive LLM acts occasionally, gated by safety checks.

## The flow

```
                                            ┌──────────────────────┐
                                            │      ingestors       │
                                            │  - system_monitor    │
                                            │  - self_prompt       │
                                            │  - daily_prompt      │
                                            │  - (yours)           │
                                            └──────────┬───────────┘
                                                       │ writes
                                                       ▼
                                            ┌──────────────────────┐
                                            │  inbox/*.md          │
                                            │  (frontmatter +      │
                                            │  body, priority,     │
                                            │  handoff_depth)      │
                                            └──────────┬───────────┘
                                                       │ every 5 min
                                                       ▼
                                            ┌──────────────────────┐
                                            │  triage.py           │
                                            │  parses frontmatter, │
                                            │  lifts handoff_depth │
                                            │  into task tags      │
                                            └──────────┬───────────┘
                                                       │ create_task
                                                       ▼
                                            ┌──────────────────────┐
                                            │  queue (SQLite)      │
                                            │  status, priority,   │
                                            │  tags                │
                                            └──────────┬───────────┘
                                                       │ atomic claim
                                                       ▼
                              ┌─────────────────────────────────────────┐
                              │            worker (N parallel)          │
                              │                                         │
                              │   1. build prompt + tail contract       │
                              │   2. call OpenRouter (gpt-4o-mini       │
                              │       → 4 free fallbacks)               │
                              │   3. split body / JSON tail             │
                              │   4. if tail.actionable AND guards pass │
                              │      → claude_handoff.run(...)          │
                              │   5. persist advisory + handoff result  │
                              └──────────────────┬──────────────────────┘
                                                 │
                                                 │ when guards pass
                                                 ▼
                                  ┌──────────────────────────────┐
                                  │   claude -p (subprocess)     │
                                  │   --permission-mode          │
                                  │     bypassPermissions        │
                                  │   --model sonnet-4-6         │
                                  │                              │
                                  │   env stripped of            │
                                  │   ANTHROPIC_API_KEY          │
                                  │   → OAuth (Max plan)         │
                                  └──────────────────────────────┘
```

## Component responsibilities

### Ingestors

Each ingestor is an event source: it watches some signal (system crash, recent completions, daily clock) and writes a markdown note to `inbox/` when there's something worth queuing. Ingestors implement their own dedup (state file under `ingestors/.state/`) so flapping signals don't spam.

### Triage

Drains `inbox/*.md` into the SQLite queue. Parses frontmatter for `priority`, `task_type`, and `handoff_depth`. Lifts `handoff_depth` into the task row's `tags` column as JSON. Moves processed notes to `inbox/processed/`.

### Queue

A single SQLite table with WAL mode. Schema in `queue.py`. The `claim.py` helper does an atomic `UPDATE…RETURNING` so N workers polling the same role can't double-claim.

### Worker

Daemon process. Polls the queue (claim → process → ack). Per task:

1. Build a prompt that includes the role description, task body, and the contract for the JSON tail.
2. Call OpenRouter with the primary model; on transport error or empty response, fall back through the chain.
3. Split the response into `body` and `tail` (the `{"actionable": …}` object).
4. If `tail.actionable` is true: read `handoff_depth` from task tags, check `claude_handoff.can_run(kind, depth)`. If allowed, shell out to `claude -p`.
5. Persist both the advisory and any handoff result to `results/<role>__<task_id>.json` and update the queue row.

The worker process itself is pure Python + HTTP. It has no `Bash`, `Edit`, or `Write` tools. The handoff is a child process that runs Claude Code with its own permission system.

### Claude handoff

`lib/claude_handoff.py` wraps `subprocess.run(["claude", "-p", ...])` with:

- **Env scrubbing.** Removes `ANTHROPIC_API_KEY` from the child env. With no key visible, Claude Code uses OAuth → Max plan.
- **Daily budget.** Counts successful runs per UTC day under the state file. Past cap, returns `skipped=True`.
- **Depth guard.** Reads depth from caller. Past cap (default 2), returns `skipped=True`.
- **Allowlist.** Only specific `action_kind` strings are accepted. Anything else, `skipped=True`.
- **Per-kind tool scoping.** Each `action_kind` in `ACTION_KIND_TOOLS` maps to a `--allowed-tools` spec — `config_edit_proposed` only gets `Write(**/*.proposed)`, `pr_open` gets `Bash(git/gh:*)`, etc. The tool surface itself enforces the safety property.
- **Optional working directory.** `pr_open` passes `repo_root` as subprocess `cwd` so `git` and `gh` operate on the right repo.

A skipped handoff is not an error; the advisory is still persisted. Hard errors (subprocess failure, JSON parse failure) raise `HandoffError` and the worker logs them.

### notify_human (worker-direct, no Claude)

When `action_kind=notify_human`, the worker calls `lib.notify.send()` directly — no subprocess, no budget burn. The advisory body + brief are sent to whichever backends are configured (Telegram, ntfy, webhook). This is for cases where the cheap LLM has decided the right action is "tell the human" rather than "do something."

### attempted_fixes memory

A separate SQLite table `attempted_fixes` records every handoff with `(target, action_kind, outcome, created_at)`. Ingestors that emit `target:` in their frontmatter consult this memory before generating new notes — if a target has already been escalated unsuccessfully twice in the last 24h, the ingestor suppresses the note rather than spamming the same advisory.

## Why a subprocess instead of an SDK?

Two reasons:

1. **Permission boundary.** Claude Code already has a permission system (`--permission-mode`, allowed/disallowed tools, sandbox detection). Putting Claude into the worker process as a library would bypass that. The subprocess is its own security domain.
2. **OAuth flow.** Claude Code's OAuth login writes credentials to `~/.claude/.credentials.json` and refreshes them via the CLI. Recreating that flow in a library means owning the refresh token, the keychain integration, etc. — too much surface area for a small project.

## Why depth propagation matters

Without it, this loop is possible:

1. Worker handles task A, escalates to Claude (depth 0).
2. Claude fixes something, writes a result.
3. `self_prompt` reflects on the result an hour later, generates follow-up B.
4. Worker handles B, escalates again (depth still 0 — no propagation).
5. Repeat indefinitely.

With propagation:

1. Worker handles A at depth 0, escalates.
2. `self_prompt` notices A triggered a handoff, writes B with `handoff_depth: 1` in frontmatter.
3. Triage lifts that into B's `tags`.
4. Worker handles B at depth 1, escalation OK (1 < cap=2).
5. `self_prompt` writes C with `handoff_depth: 2`.
6. Worker handles C at depth 2; escalation refused. Chain breaks.

The daily budget (40/day) is the slow brake; the depth cap is the fast brake. Both fire.
