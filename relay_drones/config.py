"""Configuration — env-var driven, with sensible defaults.

The whole config surface lives here so deployment is a matter of setting
environment variables (or a .env file you source) rather than editing code.

Required:
    OPENROUTER_API_KEY    — auth for the cheap advisor pool.

Optional, with defaults:
    RELAY_DRONES_DB                       Local SQLite queue path
                                          (~/.local/share/relay-drones/queue.db)
    RELAY_DRONES_ARTIFACTS                Where openrouter/claude trace markdown
                                          gets written (<repo>/artifacts)
    RELAY_DRONES_LOGS                     Where launchagent logs land
                                          (<repo>/logs)
    RELAY_DRONES_MAX_CLAUDE_RUNS_PER_DAY  Daily handoff budget (40)
    RELAY_DRONES_MAX_HANDOFF_DEPTH        Max chain depth (2)
    RELAY_DRONES_HANDOFF_MODEL            Model for the executor (claude-sonnet-4-6)
    RELAY_DRONES_HANDOFF_TIMEOUT          Seconds before `claude -p` is killed (600)
    RELAY_DRONES_OPENROUTER_PRIMARY       Primary advisor model
                                          (openai/gpt-4o-mini)
    RELAY_DRONES_OPENROUTER_FALLBACKS     Comma-separated fallback chain
                                          (4 free models by default)
    WORKER_POLL_INTERVAL                  Seconds between empty polls (30)
    WORKER_MAX_PROMPT                     Prompt body cap (12000 chars)
    WORKER_ASK_TIMEOUT                    OpenRouter timeout (600)
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ─── Paths ────────────────────────────────────────────────────────────────
INBOX = ROOT / "inbox"
INBOX_PROCESSED = INBOX / "processed"
RESULTS = ROOT / "results"


def _env_path(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return Path(raw).expanduser() if raw else default


QUEUE_DB = _env_path(
    "RELAY_DRONES_DB",
    Path.home() / ".local" / "share" / "relay-drones" / "queue.db",
)
ARTIFACTS = _env_path("RELAY_DRONES_ARTIFACTS", ROOT / "artifacts")
LOG_DIR = _env_path("RELAY_DRONES_LOGS", ROOT / "logs")
HANDOFF_STATE = _env_path(
    "RELAY_DRONES_STATE",
    ROOT / ".state",
) / "claude_budget.json"

# ─── OpenRouter advisor pool ──────────────────────────────────────────────
OPENROUTER_DEFAULT_MODEL = os.environ.get(
    "RELAY_DRONES_OPENROUTER_PRIMARY",
    "openai/gpt-4o-mini",
)
_DEFAULT_FALLBACKS = [
    "qwen/qwen3-coder:free",
    "deepseek/deepseek-v4-flash:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]
OPENROUTER_FALLBACKS = [
    s.strip()
    for s in os.environ.get(
        "RELAY_DRONES_OPENROUTER_FALLBACKS",
        ",".join(_DEFAULT_FALLBACKS),
    ).split(",")
    if s.strip()
]

# ─── Role registry ────────────────────────────────────────────────────────
# Single-role design: every inbox note routes to one generic worker pool.
# Hot-swap models by editing OPENROUTER_DEFAULT_MODEL — no code changes
# elsewhere. Adding new roles is a 5-line edit to ROLES.
TRIAGE_WORKERS_ROLE = "triage_workers"

ROLES = {
    TRIAGE_WORKERS_ROLE: {
        "provider": "openrouter",
        "model": [OPENROUTER_DEFAULT_MODEL, *OPENROUTER_FALLBACKS],
        "label": "relay-drones-triage-workers",
        "description": (
            "General-purpose autonomous advisor: research, code, proposals, "
            "analysis, drafts. One worker pool, one bill."
        ),
    },
}
VALID_ROLES = set(ROLES.keys())

# ─── Headless-Claude action handoff ───────────────────────────────────────
# When the cheap advisor flags a task as actionable, the worker shells out
# to `claude -p` so the action runs against the user's Claude Max
# subscription, not the API. ANTHROPIC_API_KEY is stripped from the child
# env in lib/claude_handoff.py — see docs/oauth-trick.md.
MAX_CLAUDE_RUNS_PER_DAY = int(
    os.environ.get("RELAY_DRONES_MAX_CLAUDE_RUNS_PER_DAY", "40")
)
MAX_HANDOFF_DEPTH = int(os.environ.get("RELAY_DRONES_MAX_HANDOFF_DEPTH", "2"))
CLAUDE_HANDOFF_MODEL = os.environ.get(
    "RELAY_DRONES_HANDOFF_MODEL", "claude-sonnet-4-6"
)
CLAUDE_HANDOFF_TIMEOUT = int(
    os.environ.get("RELAY_DRONES_HANDOFF_TIMEOUT", "600")
)

# Allowlist of action_kind values the cheap advisor is allowed to escalate.
# Novel kinds stay advisory. Keep this surface small and predictable —
# this is the main brake on prompt-injection escalations.
#
# Override with RELAY_DRONES_ACTION_ALLOWLIST=comma,separated,list
_DEFAULT_ALLOWLIST = (
    "restart_launchagent,"
    "tail_log,"
    "dns_repair,"
    "cron_disable,"
    "config_inspect,"
    "service_health"
)
CLAUDE_ACTION_ALLOWLIST = {
    s.strip()
    for s in os.environ.get(
        "RELAY_DRONES_ACTION_ALLOWLIST", _DEFAULT_ALLOWLIST
    ).split(",")
    if s.strip()
}

# ─── Worker tuning ────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.environ.get("WORKER_POLL_INTERVAL", "30"))
MAX_PROMPT = int(os.environ.get("WORKER_MAX_PROMPT", "12000"))
ASK_TIMEOUT = int(os.environ.get("WORKER_ASK_TIMEOUT", "600"))
