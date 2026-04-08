"""Tests for typed context dataclasses."""

import pytest

from nanobot.agent.hooks.events import (
    AgentBootstrapContext,
    InternalHookEvent,
    MessageReceivedContext,
    MessageSentContext,
    ToolCallContext,
)


def test_agent_bootstrap_context():
    ctx = AgentBootstrapContext(
        workspace_dir="/tmp/ws",
        bootstrap_files=[{"path": "AGENTS.md", "content": "hi"}],
        session_key="s1",
    )
    assert ctx.workspace_dir == "/tmp/ws"
    assert len(ctx.bootstrap_files) == 1
    assert ctx.session_id is None
    assert ctx.agent_id is None


def test_message_received_context():
    ctx = MessageReceivedContext(
        from_="user1",
        content="hello",
        channel_id="telegram",
        conversation_id="c1",
    )
    assert ctx.from_ == "user1"
    assert ctx.content == "hello"
    assert ctx.channel_id == "telegram"
    assert ctx.message_id is None
    assert ctx.metadata is None


def test_message_sent_context():
    ctx = MessageSentContext(
        to="chat1",
        content="response",
        success=True,
        channel_id="telegram",
        conversation_id="c1",
    )
    assert ctx.to == "chat1"
    assert ctx.success is True
    assert ctx.error is None


def test_tool_call_context():
    ctx = ToolCallContext(
        tool_name="bash",
        arguments={"command": "ls"},
    )
    assert ctx.tool_name == "bash"
    assert ctx.arguments["command"] == "ls"
    assert ctx.result is None
    assert ctx.error is None


def test_context_in_event():
    """Context dataclass values should be usable as event context."""
    bootstrap_ctx = AgentBootstrapContext(
        workspace_dir="/tmp/ws",
        bootstrap_files=[],
    )
    event = InternalHookEvent.create(
        "agent", "bootstrap", "s1",
        context=bootstrap_ctx.__dict__,
    )
    assert event.context["workspace_dir"] == "/tmp/ws"
