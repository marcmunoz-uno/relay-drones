"""Verify per-action-kind tool scoping.

The whole safety case for config_edit_proposed rests on the worker passing
--allowed-tools "Read Write(*.proposed) Glob" to the Claude subprocess.
If this map ever loses an entry, the kind reverts to "default tool set"
which means arbitrary writes. Test it directly so the gap can't sneak in.
"""
from __future__ import annotations

import pytest


def test_known_kinds_have_tool_specs():
    from relay_drones.lib.claude_handoff import ACTION_KIND_TOOLS, _tools_for

    # These kinds must be scoped — they're write-capable or shell-capable.
    must_be_scoped = {
        "config_edit_proposed",
        "pr_open",
        "cron_disable",
        "restart_launchagent",
        "dns_repair",
    }
    for kind in must_be_scoped:
        assert kind in ACTION_KIND_TOOLS, f"{kind} missing scoping"
        assert _tools_for(kind), f"{kind} has empty tool spec"


def test_config_edit_proposed_restricts_writes():
    from relay_drones.lib.claude_handoff import ACTION_KIND_TOOLS
    spec = ACTION_KIND_TOOLS["config_edit_proposed"]
    assert "Write" in spec
    assert ".proposed" in spec, "must scope writes to .proposed siblings only"


def test_pr_open_includes_git_and_gh():
    from relay_drones.lib.claude_handoff import ACTION_KIND_TOOLS
    spec = ACTION_KIND_TOOLS["pr_open"]
    assert "Bash(git" in spec
    assert "Bash(gh" in spec


def test_notify_human_NOT_in_map():
    """notify_human is handled in worker.py (no Claude subprocess).

    If it ever appears here, somebody mistakenly tried to route it through
    Claude — which would waste a budget slot on a webhook call.
    """
    from relay_drones.lib.claude_handoff import ACTION_KIND_TOOLS
    assert "notify_human" not in ACTION_KIND_TOOLS


def test_unknown_kind_returns_none(monkeypatch):
    from relay_drones.lib.claude_handoff import _tools_for
    assert _tools_for("frobnicate") is None
