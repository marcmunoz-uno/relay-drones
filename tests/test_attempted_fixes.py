"""Smoke tests for the attempted_fixes table + should_skip logic."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def af(monkeypatch, tmp_path: Path):
    """Redirect QUEUE_DB to a tmp file and return a fresh attempted_fixes module."""
    monkeypatch.setenv("RELAY_DRONES_DB", str(tmp_path / "queue.db"))
    import importlib
    from relay_drones import config as config_mod
    importlib.reload(config_mod)
    from relay_drones import attempted_fixes as af_mod
    importlib.reload(af_mod)
    return af_mod


def test_no_attempts_does_not_skip(af):
    skip, reason = af.should_skip("cron:foo")
    assert not skip
    assert "no prior attempts" in reason


def test_one_failure_allows_retry(af):
    af.record(target="cron:foo", action_kind="config_inspect", outcome="failed")
    skip, reason = af.should_skip("cron:foo")
    assert not skip
    assert "retry allowed" in reason


def test_two_failures_skip(af):
    af.record(target="cron:foo", action_kind="config_inspect", outcome="failed")
    af.record(target="cron:foo", action_kind="cron_disable", outcome="failed")
    skip, reason = af.should_skip("cron:foo")
    assert skip
    assert "2 unsuccessful" in reason


def test_success_short_circuits(af):
    af.record(target="cron:foo", action_kind="cron_disable", outcome="success")
    skip, reason = af.should_skip("cron:foo")
    assert skip
    assert "already fixed" in reason


def test_notified_counts_as_unsuccessful(af):
    # Two human notifications without an actual fix → still escalate
    af.record(target="cron:foo", action_kind="notify_human", outcome="notified")
    af.record(target="cron:foo", action_kind="notify_human", outcome="notified")
    skip, _ = af.should_skip("cron:foo")
    assert skip


def test_targets_isolated(af):
    af.record(target="cron:a", action_kind="x", outcome="failed")
    af.record(target="cron:a", action_kind="x", outcome="failed")
    skip_a, _ = af.should_skip("cron:a")
    skip_b, _ = af.should_skip("cron:b")
    assert skip_a is True
    assert skip_b is False


def test_stats(af):
    af.record(target="t1", action_kind="x", outcome="success")
    af.record(target="t2", action_kind="x", outcome="failed")
    af.record(target="t3", action_kind="x", outcome="failed")
    s = af.stats()
    assert s == {"success": 1, "failed": 2}


def test_record_returns_row_id(af):
    rid = af.record(target="t", action_kind="x", outcome="success")
    assert isinstance(rid, int) and rid >= 1
