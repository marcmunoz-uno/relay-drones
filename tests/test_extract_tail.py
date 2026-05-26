"""Smoke tests for the JSON-tail extractor in worker.py.

Free-fallback models produce inconsistent JSON output (extra prose, code
fences, trailing whitespace). The extractor handles both strict trailing
JSON and a relaxed scan-from-end.
"""
from __future__ import annotations

import pytest

from relay_drones.worker import _extract_handoff


def test_strict_trailing_json():
    raw = (
        "advisory text here\n"
        '{"actionable": true, "action_kind": "dns_repair", '
        '"action_brief": "flushcache", "handoff_reason": "test"}'
    )
    body, tail = _extract_handoff(raw)
    assert tail == {
        "actionable": True,
        "action_kind": "dns_repair",
        "action_brief": "flushcache",
        "handoff_reason": "test",
    }
    assert body == "advisory text here"


def test_no_tail_returns_none():
    body, tail = _extract_handoff("advisory only, no JSON")
    assert tail is None
    assert body == "advisory only, no JSON"


def test_relaxed_scan_picks_last_balanced_object():
    raw = (
        "prose\n\n"
        'random prose with {"actionable": false} embedded\n\n'
        'final {"actionable": true, "action_kind": "tail_log"}'
    )
    body, tail = _extract_handoff(raw)
    assert tail is not None
    assert tail["actionable"] is True
    assert tail["action_kind"] == "tail_log"


def test_empty_input():
    body, tail = _extract_handoff("")
    assert tail is None
    assert body == ""


def test_handles_trailing_whitespace():
    raw = 'body\n{"actionable": true, "action_kind": "tail_log"}\n\n   '
    body, tail = _extract_handoff(raw)
    assert tail == {"actionable": True, "action_kind": "tail_log"}
    assert body == "body"


def test_actionable_false_still_parses():
    raw = 'body\n{"actionable": false}'
    body, tail = _extract_handoff(raw)
    assert tail == {"actionable": False}
