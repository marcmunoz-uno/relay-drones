"""Smoke tests for the notifier.

We don't actually hit Telegram/ntfy/webhooks — we monkeypatch urllib.request.urlopen
to record calls. The point is to verify routing, env-var gating, and ordering.
"""
from __future__ import annotations

from io import BytesIO

import pytest


@pytest.fixture
def fake_urlopen(monkeypatch):
    calls: list[tuple[str, dict, bytes | None]] = []

    class _Resp:
        def __init__(self, body=b'{"ok": true}'):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        calls.append((
            req.full_url,
            dict(req.headers),
            req.data,
        ))
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return calls


def test_no_backends_returns_not_ok(monkeypatch, fake_urlopen):
    for k in (
        "RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN",
        "RELAY_DRONES_NOTIFY_TELEGRAM_CHAT_ID",
        "RELAY_DRONES_NOTIFY_NTFY_TOPIC",
        "RELAY_DRONES_NOTIFY_WEBHOOK_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    from relay_drones.lib import notify
    result = notify.send("t", "b")
    assert not result.ok
    assert result.tried == []
    assert fake_urlopen == []


def test_telegram_routes_correctly(monkeypatch, fake_urlopen):
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN", "ABC")
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_TELEGRAM_CHAT_ID", "999")
    for k in ("RELAY_DRONES_NOTIFY_NTFY_TOPIC", "RELAY_DRONES_NOTIFY_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    from relay_drones.lib import notify
    result = notify.send("hi", "body")
    assert result.ok
    assert len(fake_urlopen) == 1
    url, _, _ = fake_urlopen[0]
    assert "api.telegram.org" in url
    assert "ABC" in url


def test_ntfy_uses_default_server(monkeypatch, fake_urlopen):
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_NTFY_TOPIC", "my-topic")
    for k in (
        "RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN",
        "RELAY_DRONES_NOTIFY_WEBHOOK_URL",
        "RELAY_DRONES_NOTIFY_NTFY_SERVER",
    ):
        monkeypatch.delenv(k, raising=False)
    from relay_drones.lib import notify
    result = notify.send("hi", "body")
    assert result.ok
    url, headers, _ = fake_urlopen[0]
    assert url == "https://ntfy.sh/my-topic"
    assert headers.get("Title", "").startswith("hi")


def test_ntfy_custom_server(monkeypatch, fake_urlopen):
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_NTFY_TOPIC", "t")
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_NTFY_SERVER", "https://my-ntfy.example.com/")
    for k in (
        "RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN",
        "RELAY_DRONES_NOTIFY_WEBHOOK_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    from relay_drones.lib import notify
    notify.send("t", "b")
    url, _, _ = fake_urlopen[0]
    assert url == "https://my-ntfy.example.com/t"


def test_all_three_fire_when_all_configured(monkeypatch, fake_urlopen):
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN", "TOK")
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_TELEGRAM_CHAT_ID", "999")
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_NTFY_TOPIC", "topic")
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_WEBHOOK_URL", "https://hook.example/x")
    from relay_drones.lib import notify
    result = notify.send("t", "b")
    assert result.ok
    assert len(fake_urlopen) == 3
    assert {url for url, _, _ in fake_urlopen} == {
        "https://api.telegram.org/botTOK/sendMessage",
        "https://ntfy.sh/topic",
        "https://hook.example/x",
    }


def test_only_filter(monkeypatch, fake_urlopen):
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN", "TOK")
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_TELEGRAM_CHAT_ID", "999")
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_NTFY_TOPIC", "topic")
    for k in ("RELAY_DRONES_NOTIFY_WEBHOOK_URL",):
        monkeypatch.delenv(k, raising=False)
    from relay_drones.lib import notify
    notify.send("t", "b", only=["telegram"])
    assert len(fake_urlopen) == 1
    assert "telegram.org" in fake_urlopen[0][0]


def test_configured_backends(monkeypatch):
    monkeypatch.setenv("RELAY_DRONES_NOTIFY_NTFY_TOPIC", "t")
    for k in (
        "RELAY_DRONES_NOTIFY_TELEGRAM_BOT_TOKEN",
        "RELAY_DRONES_NOTIFY_WEBHOOK_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    from relay_drones.lib import notify
    assert notify.configured_backends() == ["ntfy"]
