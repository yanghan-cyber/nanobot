"""Tests verifying message context field names match OpenClaw conventions."""

import pytest

from nanobot.agent.hooks.events import InternalHookEvent, MessageReceivedContext, MessageSentContext


def test_message_received_context_field_names():
    """MessageReceivedContext uses OpenClaw-aligned field names."""
    ctx = MessageReceivedContext(
        from_="user1",
        content="hello",
        channel_id="telegram",
        conversation_id="chat123",
    )
    # Should have channel_id (not channel), conversation_id (not chat_id)
    assert "channel_id" in ctx.__dataclass_fields__
    assert "conversation_id" in ctx.__dataclass_fields__
    assert "from_" in ctx.__dataclass_fields__


def test_message_sent_context_field_names():
    """MessageSentContext uses OpenClaw-aligned field names."""
    ctx = MessageSentContext(
        to="chat123",
        content="response",
        success=True,
        channel_id="telegram",
        conversation_id="chat123",
    )
    assert "channel_id" in ctx.__dataclass_fields__
    assert "conversation_id" in ctx.__dataclass_fields__


def test_event_context_uses_aligned_names_received():
    """When building a received event context dict, use OpenClaw names."""
    ctx = {
        "from_": "user1",
        "content": "hello",
        "channel_id": "telegram",
        "conversation_id": "chat123",
    }
    event = InternalHookEvent.create("message", "received", "s1", ctx)
    assert event.context["channel_id"] == "telegram"
    assert event.context["conversation_id"] == "chat123"
    assert event.context["from_"] == "user1"


def test_event_context_uses_aligned_names_sent():
    """When building a sent event context dict, use OpenClaw names."""
    ctx = {
        "to": "chat123",
        "content": "response",
        "success": True,
        "channel_id": "telegram",
        "conversation_id": "chat123",
    }
    event = InternalHookEvent.create("message", "sent", "s1", ctx)
    assert event.context["channel_id"] == "telegram"
    assert event.context["conversation_id"] == "chat123"
