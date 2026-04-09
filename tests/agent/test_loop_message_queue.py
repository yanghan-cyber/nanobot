"""Tests for per-session pending message queue in AgentLoop."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest


def _make_loop(tmp_path):
    from nanobot.agent.loop import AgentLoop

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager") as MockSubMgr:
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop


def test_loop_has_pending_queues_and_active_sessions(tmp_path):
    """AgentLoop should initialize with _pending_queues and _active_sessions."""
    loop = _make_loop(tmp_path)
    assert hasattr(loop, "_pending_queues")
    assert isinstance(loop._pending_queues, dict)
    assert hasattr(loop, "_active_sessions")
    assert isinstance(loop._active_sessions, set)


@pytest.mark.asyncio
async def test_dispatch_queues_when_session_active(tmp_path):
    """If the session is already active, _dispatch should queue the message and return."""
    loop = _make_loop(tmp_path)
    loop._active_sessions.add("telegram:123")

    msg = InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="queued msg")
    await loop._dispatch(msg)

    queue = loop._pending_queues.get("telegram:123")
    assert queue is not None
    assert not queue.empty()
    queued_msg = queue.get_nowait()
    assert queued_msg.content == "queued msg"


@pytest.mark.asyncio
async def test_dispatch_processes_message_when_session_inactive(tmp_path):
    """If the session is not active, _dispatch should process the message normally."""
    loop = _make_loop(tmp_path)

    processed: list[InboundMessage] = []

    async def fake_process(msg, **kwargs):
        processed.append(msg)
        return None

    loop._process_message = fake_process

    msg = InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="hello")
    await loop._dispatch(msg)

    assert len(processed) == 1
    assert processed[0].content == "hello"
    assert "telegram:123" not in loop._active_sessions


@pytest.mark.asyncio
async def test_dispatch_drains_pending_after_runner(tmp_path):
    """After runner finishes, remaining pending messages should be processed."""
    loop = _make_loop(tmp_path)

    processed: list[str] = []

    async def fake_process(msg, **kwargs):
        processed.append(msg.content)
        return None

    loop._process_message = fake_process

    # Pre-populate the pending queue
    queue = loop._pending_queues.setdefault("telegram:123", asyncio.Queue())
    await queue.put(InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="pending msg"))

    msg = InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="initial msg")
    await loop._dispatch(msg)

    assert "initial msg" in processed
    assert "pending msg" in processed
    assert "telegram:123" not in loop._active_sessions


@pytest.mark.asyncio
async def test_dispatch_queues_during_active_run(tmp_path):
    """Messages arriving during an active run should be queued, not block."""
    loop = _make_loop(tmp_path)

    processing_started = asyncio.Event()
    processing_done = asyncio.Event()

    async def fake_process(msg, **kwargs):
        processing_started.set()
        await processing_done.wait()
        return None

    loop._process_message = fake_process

    msg1 = InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="first")
    task1 = asyncio.create_task(loop._dispatch(msg1))

    await asyncio.wait_for(processing_started.wait(), timeout=1.0)
    assert "telegram:123" in loop._active_sessions

    msg2 = InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="second")
    await loop._dispatch(msg2)

    queue = loop._pending_queues.get("telegram:123")
    assert queue is not None
    assert queue.get_nowait().content == "second"

    processing_done.set()
    await task1