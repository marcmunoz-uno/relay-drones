# relay-drones

A swarm of cheap LLM advisors that escalate to a Claude executor — running on your **Claude Max subscription**, not the API.

```
     ingestors                  inbox            queue              advisors          escalator
  ┌──────────────┐         ┌──────────┐     ┌──────────┐         ┌──────────┐      ┌──────────┐
  │ system_monitor│─┐      │ *.md     │     │ tasks    │         │ worker   │      │ claude   │
  │ self_prompt   │─┼──►   │ frontmtr │─►   │ sqlite   │─claim─► │ pool     │─tail►│  -p      │
  │ daily_prompt  │─┘      │ priority │     │ tags     │         │ (N copies)│      │ Max plan │
  │ + your own    │        │ depth    │     │          │         │ gpt-4o-mini│     │ OAuth    │
  └──────────────┘         └──────────┘     └──────────┘         └──────────┘      └──────────┘
                                                                       │                ▲
                                                                       └─actionable JSON┘
                                                                         tail triggers
                                                                         the handoff
                                                                         (gated)
```

The cheap workers (`gpt-4o-mini` through OpenRouter, ~$0.0014/task) handle diagnosis and prose. When they spot something that needs *doing* — restart a stuck service, flush DNS, edit a config — they tag the response `{"actionable": true, ...}` and the worker shells out to `claude -p` to do it. The Claude call goes through your Max subscription instead of the API key because we strip `ANTHROPIC_API_KEY` from the subprocess env. **No per-token billing.**

## Why this exists

Cheap LLMs are great at reading logs and writing memos. They're bad at fixing things — no tools, no shell, no filesystem. Capable LLMs (Claude with tool access) are great at fixing things but expensive to run continuously. relay-drones runs the cheap layer continuously and *escalates* to the capable layer only when an action is needed and a chain of safety checks pass.

- **Continuous cheap reasoning.** 4 parallel workers, ~$2/month at typical volume.
- **Bursts of expensive action.** Claude only spins up when a problem is actually fixable. On the Max plan there's no marginal cost — just session-rate-limit consumption.
- **Three safety brakes.** Action allowlist, daily budget, depth cap. Prevent prompt injection from log content. Prevent runaway recursion. Prevent burning through the Max plan in an afternoon.

## Quickstart

Requires Python ≥ 3.9, macOS or Linux, an OpenRouter API key, and Claude Code installed + logged in via `claude` (OAuth).

```bash
git clone https://github.com/marcmunoz-uno/relay-drones.git
cd relay-drones
pip install -e .

cp .env.example .env
$EDITOR .env                # paste your OPENROUTER_API_KEY

# One-time: log into Claude Code so OAuth creds get written to disk
claude                       # opens REPL
/login                       # browser flow with your Max account
/exit

# Smoke test: write a note, run triage, run a worker once
relay-drones drop "test" "say hello to the loop"
relay-drones triage
relay-drones worker triage_workers --once

relay-drones status
```

For continuous operation, install the LaunchAgents (macOS) or systemd units (Linux) — see [`launchagents/README.md`](launchagents/README.md) and [`systemd/README.md`](systemd/README.md).

## How the handoff works

The cheap worker's response ends with a single-line JSON object:

```json
{"actionable": true, "action_kind": "dns_repair", "action_brief": "flush DNS cache then HUP mDNSResponder", "handoff_reason": "openrouter unreachable, DNS resolution failing"}
```

`worker.py` parses the tail. If `actionable: true` AND three gates pass:

1. **`action_kind`** is in the allowlist (`restart_launchagent`, `tail_log`, `dns_repair`, `cron_disable`, `config_inspect`, `service_health` by default — override via `RELAY_DRONES_ACTION_ALLOWLIST`)
2. **Daily budget** under the cap (`40` by default, configurable)
3. **Handoff depth** under the cap (`2` by default — see [docs/safety-rails.md](docs/safety-rails.md))

Eight `action_kind` values ship in the default allowlist, ordered by what they enable:

| Kind | What it does | Path |
|---|---|---|
| `notify_human` | Send a Telegram / ntfy / webhook ping. No Claude spawned. | Worker-direct |
| `tail_log` | Read-only `tail`/`head`/`grep` against a log file. | Claude, read-only tools |
| `config_inspect` | Read-only `cat`/`grep` against config files. | Claude, read-only tools |
| `service_health` | `curl`/`ping`/`systemctl status`/`launchctl list`. | Claude, status tools |
| `dns_repair` | `dscacheutil -flushcache`, restart resolver. | Claude, dns-tools |
| `restart_launchagent` | `launchctl bootout`/`bootstrap`. | Claude, launchctl |
| `cron_disable` | Mark a broken cron disabled. | Claude, edit + launchctl |
| `config_edit_proposed` | Writes a fix to `<path>.proposed` (NOT the real file). | Claude, `Write(**/*.proposed)` only |
| `pr_open` | Opens a draft PR in a git repo via `gh pr create`. | Claude with cwd=repo, git+gh tools |

Each kind is scoped via Claude Code's `--allowed-tools` — `config_edit_proposed` can *only* write `.proposed` files, not the real config. This makes the safety property a real enforcement, not just prompt discipline.

…then the worker shells out:

```python
env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
subprocess.run(
    ["claude", "-p", prompt,
     "--model", "claude-sonnet-4-6",
     "--permission-mode", "bypassPermissions",
     "--output-format", "json",
     "--no-session-persistence"],
    env=env, timeout=600,
)
```

That stripped `ANTHROPIC_API_KEY` is the whole trick. With no API key visible, Claude Code falls back to the OAuth credentials in `~/.claude/.credentials.json` and the call runs against your Max subscription. See [docs/oauth-trick.md](docs/oauth-trick.md) for details + the failure modes.

If any gate fails or the tail is missing, the advisory is recorded and nothing is escalated. The cheap worker is still useful as a memo writer; the handoff is opt-in per-task.

## Memory of attempted fixes

Without memory, the system rediscovers the same broken cron every 15 minutes and produces the same advisory forever. With memory (the `attempted_fixes` table), the loop closes: ingestors that set a `target:` in their note's frontmatter get checked against recent attempts before re-queuing.

- 0 prior attempts in last 24h → queue normally.
- 1 prior unsuccessful attempt → retry allowed (one more try).
- 2+ prior unsuccessful attempts → suppress; the human got `notify_human` from the earlier attempts and the system should stop spamming itself.
- 1 prior successful attempt → suppress (problem fixed; nothing to re-investigate).

The worker writes a row to `attempted_fixes` after every handoff with the outcome (`success`, `skipped`, `failed`, or `notified`). The same SQLite DB as the task queue, so no extra service to run.

## Project layout

```
relay-drones/
├── relay_drones/
│   ├── config.py             # env-var-driven config surface
│   ├── queue.py              # 200-line SQLite task queue
│   ├── worker.py             # the advisor + handoff trigger
│   ├── triage.py             # inbox → queue
│   ├── reporter.py           # markdown digest
│   ├── cli.py                # `relay-drones` CLI
│   ├── lib/
│   │   ├── openrouter.py     # cheap-LLM transport with fallback chain
│   │   ├── claude_handoff.py # subprocess wrapper with the API-key strip
│   │   ├── claim.py          # race-safe task claim for N workers
│   │   └── bb.py             # observability hook (no-op by default)
│   └── ingestors/
│       ├── self_prompt.py    # reflects on completions → follow-ups
│       ├── system_monitor.py # macOS crash reports → inbox notes
│       └── daily_prompt.py   # one reflection note per day at 7AM
├── bin/relay-drones          # entry-point shim
├── launchagents/             # macOS plist templates
├── systemd/                  # Linux user-unit templates
├── docs/                     # architecture, oauth-trick, safety-rails
└── tests/                    # pytest smoke tests for the guards
```

## Configuration

Everything is environment variables. See [`.env.example`](.env.example) for the full surface; highlights:

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | _required_ | Auth for the cheap advisor pool |
| `RELAY_DRONES_DB` | `~/.local/share/relay-drones/queue.db` | Where the SQLite queue lives |
| `RELAY_DRONES_MAX_CLAUDE_RUNS_PER_DAY` | `40` | Handoff budget per UTC day |
| `RELAY_DRONES_MAX_HANDOFF_DEPTH` | `2` | Max chain depth before refusal |
| `RELAY_DRONES_HANDOFF_MODEL` | `claude-sonnet-4-6` | Which Claude model runs actions |
| `RELAY_DRONES_ACTION_ALLOWLIST` | _6 defaults_ | Comma-separated `action_kind` values allowed to escalate |
| `RELAY_DRONES_OPENROUTER_PRIMARY` | `openai/gpt-4o-mini` | Primary advisor model |
| `RELAY_DRONES_OPENROUTER_FALLBACKS` | _4 free models_ | Comma-separated fallback chain |

## Design constraints

- **Zero runtime dependencies** beyond the standard library. Auditable in an afternoon.
- **stdlib SQLite** for the queue. No Redis, no RabbitMQ, no Celery.
- **OpenRouter as the only outbound LLM provider for advisors.** One bill, one API key, dozens of models behind one endpoint.
- **Subprocess for the executor.** Reuses Claude Code's permission system instead of building our own.
- **Workers have no in-process tools.** The architectural split (advisor process is pure Python+HTTP, executor process is a separate sandboxed Claude) is the main defense against prompt-injection escalating to local code execution.

## Adding your own ingestor

Drop a Python file under `relay_drones/ingestors/` that calls `_lib.write_note()` when it has something to queue. Add a LaunchAgent / systemd timer that fires it on a schedule. Triage will pick up whatever lands in the inbox. Examples in `relay_drones/ingestors/`.

## Status

Alpha. The core architecture (advisor → gated handoff → executor) is solid and running in the author's setup. The package is published in the form an outside user can clone-and-run, but expect rough edges. Bug reports welcome.

## License

MIT — see [LICENSE](LICENSE).
