"""Smoke tests for the three handoff guards.

These run without invoking Claude — they only exercise the gating logic,
which is the load-bearing safety surface.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def tmp_state(monkeypatch, tmp_path: Path):
    """Redirect the budget-state file into a temp dir so tests are hermetic."""
    state_path = tmp_path / "claude_budget.json"
    monkeypatch.setenv("RELAY_DRONES_STATE", str(tmp_path))
    # Re-import config so the env var takes effect.
    import importlib

    from relay_drones import config as config_mod
    importlib.reload(config_mod)
    from relay_drones.lib import claude_handoff
    importlib.reload(claude_handoff)
    return state_path, claude_handoff


def test_can_run_default_passes(tmp_state):
    _, claude_handoff = tmp_state
    assert claude_handoff.can_run("dns_repair", 0) is None
    assert claude_handoff.can_run("tail_log", 0) is None
    assert claude_handoff.can_run("config_inspect", 1) is None


def test_can_run_rejects_unknown_action_kind(tmp_state):
    _, claude_handoff = tmp_state
    reason = claude_handoff.can_run("delete_everything", 0)
    assert reason is not None and "allowlist" in reason


def test_can_run_rejects_depth_at_cap(tmp_state):
    _, claude_handoff = tmp_state
    reason = claude_handoff.can_run("dns_repair", 2)
    assert reason is not None and "depth" in reason


def test_can_run_rejects_depth_above_cap(tmp_state):
    _, claude_handoff = tmp_state
    reason = claude_handoff.can_run("dns_repair", 3)
    assert reason is not None and "depth" in reason


def test_budget_exhausted_blocks(tmp_state, monkeypatch):
    state_path, claude_handoff = tmp_state
    # Lower the cap so we don't have to write 40 records
    monkeypatch.setenv("RELAY_DRONES_MAX_CLAUDE_RUNS_PER_DAY", "2")
    import importlib
    from relay_drones import config as config_mod
    importlib.reload(config_mod)
    importlib.reload(claude_handoff)

    # Manually mark budget exhausted
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"day": today, "count": 2}))

    reason = claude_handoff.can_run("dns_repair", 0)
    assert reason is not None and "budget" in reason


def test_budget_rolls_over_new_day(tmp_state):
    state_path, claude_handoff = tmp_state
    # Write yesterday's exhausted budget; today should reset
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"day": "1999-01-01", "count": 9999}))

    used, _ = claude_handoff.budget_status()
    assert used == 0, "stale day should reset to 0"
