"""Notification backends — Telegram, ntfy, generic webhook.

Used by the worker when an advisor flags `action_kind=notify_human` and
the action is too unclear / too dangerous / out-of-scope for either the
advisor or Claude to handle autonomously. Also used as a fallback when
handoffs fail after multiple attempts (see attempted_fixes).

Why this exists:
    The whole point of the agent loop is that things happen on their own.
    But "on their own" sometimes means "the system has decided it can't
    safely fix this." Silence in that case is worse than no system at
    all — the user assumes things are fine and finds out hours later they
    weren't. notify.send() is the loudness valve.

Backends configured via environment, tried in order until one succeeds:

    RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN  +
    RELAY_DRONES_NOTIFY_TELEGRAM_CHAT_ID    → Telegram (highest priority)

    RELAY_DRONES_NOTIFY_NTFY_TOPIC          → ntfy.sh (or custom server
                                              via RELAY_DRONES_NOTIFY_NTFY_SERVER)

    RELAY_DRONES_NOTIFY_WEBHOOK_URL         → POST JSON to arbitrary URL
                                              (Slack/Discord/n8n incoming)

If none configured, send() returns False and logs a warning. That's an
honest "I couldn't tell anyone" — caller decides whether to retry or
record-and-move-on.

stdlib only. No requests, no notify-py, no slack-sdk.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

TIMEOUT = 10  # seconds — notifications are fire-and-forget


class NotifyResult:
    """Result envelope. .ok says whether ANY backend succeeded."""

    def __init__(self, ok: bool, tried: list[tuple[str, bool, str]]):
        self.ok = ok
        self.tried = tried  # list of (backend_name, success, detail)

    def __repr__(self) -> str:
        return f"NotifyResult(ok={self.ok}, tried={len(self.tried)})"


def _telegram_configured() -> bool:
    return bool(
        os.environ.get("RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN")
        and os.environ.get("RELAY_DRONES_NOTIFY_TELEGRAM_CHAT_ID")
    )


def _send_telegram(title: str, body: str) -> tuple[bool, str]:
    token = os.environ["RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["RELAY_DRONES_NOTIFY_TELEGRAM_CHAT_ID"]
    text = f"*{title}*\n\n{body}" if title else body
    # Telegram caps messages at 4096 chars; truncate with marker.
    if len(text) > 4000:
        text = text[:3990] + "\n…[truncated]"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return True, "delivered"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"unreachable: {e}"


def _ntfy_configured() -> bool:
    return bool(os.environ.get("RELAY_DRONES_NOTIFY_NTFY_TOPIC"))


def _send_ntfy(title: str, body: str) -> tuple[bool, str]:
    topic = os.environ["RELAY_DRONES_NOTIFY_NTFY_TOPIC"]
    server = os.environ.get(
        "RELAY_DRONES_NOTIFY_NTFY_SERVER", "https://ntfy.sh"
    ).rstrip("/")
    url = f"{server}/{urllib.parse.quote(topic, safe='')}"
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={
            "Title": title[:200] if title else "relay-drones",
            "Priority": "default",
            "Tags": "robot",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return True, "delivered"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"unreachable: {e}"


def _webhook_configured() -> bool:
    return bool(os.environ.get("RELAY_DRONES_NOTIFY_WEBHOOK_URL"))


def _send_webhook(title: str, body: str) -> tuple[bool, str]:
    url = os.environ["RELAY_DRONES_NOTIFY_WEBHOOK_URL"]
    payload = {"title": title, "body": body, "source": "relay-drones"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return True, "delivered"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"unreachable: {e}"


# Order matters — Telegram first (interactive, can reply), then ntfy
# (push notifications to phone), then webhook (catch-all).
_BACKENDS = [
    ("telegram", _telegram_configured, _send_telegram),
    ("ntfy", _ntfy_configured, _send_ntfy),
    ("webhook", _webhook_configured, _send_webhook),
]


def configured_backends() -> list[str]:
    """Return names of backends that have env-var config present."""
    return [name for name, check, _ in _BACKENDS if check()]


def send(
    title: str,
    body: str,
    *,
    only: Optional[list[str]] = None,
) -> NotifyResult:
    """Send a notification through every configured backend.

    `only` restricts to a subset by name (useful in tests). Default: try all
    configured backends. Returns NotifyResult(ok=True) if AT LEAST ONE
    backend delivered; ok=False means everyone failed or no backend is
    configured.
    """
    tried: list[tuple[str, bool, str]] = []
    any_success = False
    for name, check, fn in _BACKENDS:
        if only is not None and name not in only:
            continue
        if not check():
            continue
        ok, detail = fn(title, body)
        tried.append((name, ok, detail))
        if ok:
            any_success = True

    if not tried:
        print(
            "[notify] no backends configured — set "
            "RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN + _CHAT_ID, "
            "RELAY_DRONES_NOTIFY_NTFY_TOPIC, or "
            "RELAY_DRONES_NOTIFY_WEBHOOK_URL to enable",
            file=sys.stderr,
        )

    return NotifyResult(ok=any_success, tried=tried)
