"""Blackboard / observability stub.

In the original deployment this talked to a shared SQLite blackboard so
multiple agents could see each other's presence and messages. For the
public package we ship no-op stubs — the architecture works fine without
them, and any real observability target (Prometheus, OpenTelemetry, your
own SQLite) plugs in here by overriding these two functions.

To wire up your own:
    from relay_drones.lib import bb
    bb.set_presence = my_presence_writer
    bb.post_message = my_message_writer
"""
from __future__ import annotations


def set_presence(agent: str, status: str, current_work: str = "") -> None:  # noqa: ARG001
    """Optional hook: record that `agent` is in `status` doing `current_work`.

    Default: no-op. Override to plug in your observability backend.
    """
    return


def post_message(from_agent: str, content: str, channel: str = "relay-drones") -> None:  # noqa: ARG001
    """Optional hook: record a free-text message from `from_agent`.

    Default: no-op. Override to plug in your observability backend.
    """
    return
