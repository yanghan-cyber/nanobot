"""Tests for event type guard functions."""

import pytest

from nanobot.agent.hooks.events import InternalHookEvent
from nanobot.agent.hooks.guards import (
    is_agent_bootstrap_event,
    is_message_received_event,
    is_message_sent_event,
    is_tool_before_call_event,
    is_tool_after_call_event,
)


def _make_event(type_: str, action: str, context: dict | None = None) -> InternalHookEvent:
    return InternalHookEvent.create(type_, action, "s1", context)


# --- is_agent_bootstrap_event ---


def test_is_agent_bootstrap_true():
    event = _make_event("agent", "bootstrap", {"workspace_dir": "/ws", "bootstrap_files": []})
    assert is_agent_bootstrap_event(event) is True


def test_is_agent_bootstrap_wrong_type():
    event = _make_event("message", "bootstrap")
    assert is_agent_bootstrap_event(event) is False


def test_is_agent_bootstrap_wrong_action():
    event = _make_event("agent", "shutdown")
    assert is_agent_bootstrap_event(event) is False


def test_is_agent_bootstrap_missing_files():
    event = _make_event("agent", "bootstrap", {"workspace_dir": "/ws"})
    # bootstrap_files is missing — not a valid bootstrap event
    assert is_agent_bootstrap_event(event) is False


# --- is_message_received_event ---


def test_is_message_received_true():
    event = _make_event("message", "received", {
        "from_": "user1", "content": "hi", "channel_id": "tg",
    })
    assert is_message_received_event(event) is True


def test_is_message_received_wrong_action():
    event = _make_event("message", "sent", {
        "from_": "user1", "content": "hi", "channel_id": "tg",
    })
    assert is_message_received_event(event) is False


def test_is_message_received_missing_fields():
    event = _make_event("message", "received", {"content": "hi"})
    assert is_message_received_event(event) is False


# --- is_message_sent_event ---


def test_is_message_sent_true():
    event = _make_event("message", "sent", {
        "to": "chat1", "content": "ok", "success": True, "channel_id": "tg",
    })
    assert is_message_sent_event(event) is True


def test_is_message_sent_wrong_type():
    event = _make_event("agent", "sent", {
        "to": "chat1", "content": "ok", "success": True, "channel_id": "tg",
    })
    assert is_message_sent_event(event) is False


# --- is_tool_before_call_event / is_tool_after_call_event ---


def test_is_tool_before_call_true():
    event = _make_event("tool", "before_call", {"tool_name": "bash"})
    assert is_tool_before_call_event(event) is True


def test_is_tool_after_call_true():
    event = _make_event("tool", "after_call", {"tool_name": "bash", "result": "ok"})
    assert is_tool_after_call_event(event) is True


def test_is_tool_before_call_wrong_action():
    event = _make_event("tool", "after_call", {"tool_name": "bash"})
    assert is_tool_before_call_event(event) is False
