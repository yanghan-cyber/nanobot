"""Tests for InternalHookEvent."""

from datetime import datetime

from nanobot.agent.hooks.events import InternalHookEvent


def test_create_event():
    event = InternalHookEvent.create("agent", "bootstrap", "cli:direct")
    assert event.type == "agent"
    assert event.action == "bootstrap"
    assert event.session_key == "cli:direct"
    assert isinstance(event.timestamp, datetime)
    assert event.context == {}
    assert event.messages == []


def test_create_event_with_context():
    event = InternalHookEvent.create(
        "message", "received", "telegram:123",
        {"from": "user1", "content": "hello", "channel": "telegram", "chat_id": "123"},
    )
    assert event.context["from"] == "user1"
    assert event.context["content"] == "hello"


def test_event_messages_mutable():
    event = InternalHookEvent.create("agent", "bootstrap", "s1")
    event.messages.append("hello")
    assert event.messages == ["hello"]


def test_event_context_mutable():
    event = InternalHookEvent.create("agent", "bootstrap", "s1", {"files": []})
    event.context["files"].append({"path": "test.md", "content": "x"})
    assert len(event.context["files"]) == 1
