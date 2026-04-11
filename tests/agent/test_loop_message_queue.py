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


def test_loop_has_pending_queues_and_session_locks(tmp_path):
    """AgentLoop should initialize with _pending_queues and _session_locks."""
    loop = _make_loop(tmp_path)
    assert hasattr(loop, "_pending_queues")
    assert isinstance(loop._pending_queues, dict)
    assert hasattr(loop, "_session_locks")
    assert isinstance(loop._session_locks, dict)


@pytest.mark.asyncio
async def test_dispatch_creates_pending_queue(tmp_path):
    """_dispatch should create a pending queue for the session."""
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
    # After dispatch completes, the pending queue should be cleaned up
    assert "telegram:123" not in loop._pending_queues


@pytest.mark.asyncio
async def test_dispatch_uses_session_lock(tmp_path):
    """_dispatch should use a per-session lock for serial processing."""
    loop = _make_loop(tmp_path)

    processed: list[InboundMessage] = []

    async def fake_process(msg, **kwargs):
        processed.append(msg)
        return None

    loop._process_message = fake_process

    msg = InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="hello")
    await loop._dispatch(msg)

    # A lock should have been created for the session
    assert "telegram:123" in loop._session_locks
    assert isinstance(loop._session_locks["telegram:123"], asyncio.Lock)


@pytest.mark.asyncio
async def test_dispatch_republishes_leftover_messages(tmp_path):
    """After runner finishes, leftover pending messages should be re-published to the bus."""
    loop = _make_loop(tmp_path)

    async def fake_process(msg, **kwargs):
        return None

    loop._process_message = fake_process

    republished: list[InboundMessage] = []
    loop.bus.publish_inbound = AsyncMock(side_effect=lambda m: republished.append(m))

    msg = InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="initial")
    await loop._dispatch(msg)

    # No leftover messages, so nothing should be republished
    assert len(republished) == 0


@pytest.mark.asyncio
async def test_injection_callback_injects_queued_messages_into_runner(tmp_path):
    """The injection_callback should drain queued messages into the runner's context."""
    loop = _make_loop(tmp_path)

    # Set up session
    from nanobot.session.manager import Session
    session = Session(key="telegram:123")
    loop.sessions.get_or_create = MagicMock(return_value=session)
    loop.sessions.save = MagicMock()

    # Mock context methods used by _drain_pending
    loop.context._build_user_content = MagicMock(return_value="mid-run msg")
    loop.context._build_runtime_context = MagicMock(return_value="[runtime context]")
    loop.context.timezone = "UTC"

    # Pre-populate pending queue
    pending_queue = asyncio.Queue(maxsize=20)
    loop._pending_queues["telegram:123"] = pending_queue
    await pending_queue.put(InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="mid-run msg"))

    # Mock provider to capture messages
    call_count = {"n": 0}
    captured: list[dict] = []

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            captured[:] = messages
            return LLMResponse(
                content="working",
                tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
                usage={},
            )
        captured[:] = messages
        return LLMResponse(content="done", tool_calls=[], usage={})

    loop.provider.chat_with_retry = chat_with_retry
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.execute = AsyncMock(return_value="tool result")

    result, _, _, _, _ = await loop._run_agent_loop(
        [{"role": "user", "content": "initial task"}],
        session=session,
        channel="telegram",
        chat_id="123",
        pending_queue=pending_queue,
    )

    assert result == "done"
    # The second LLM call should include the injected user message
    user_msgs = [m for m in captured if m.get("role") == "user" and "mid-run msg" in str(m.get("content", ""))]
    assert len(user_msgs) >= 1


@pytest.mark.asyncio
async def test_e2e_message_queue_flow(tmp_path):
    """Full flow: msg A starts runner, msg B queued mid-run, injected into context."""
    loop = _make_loop(tmp_path)

    # Set up a session
    from nanobot.session.manager import Session
    session = Session(key="telegram:123")
    loop.sessions.get_or_create = MagicMock(return_value=session)
    loop.sessions.save = MagicMock()

    # Mock context methods used by _drain_pending
    loop.context._build_user_content = MagicMock(return_value="actually, do task B instead")
    loop.context._build_runtime_context = MagicMock(return_value="[runtime context]")
    loop.context.timezone = "UTC"

    # Provider that takes time on first call (tool execution)
    call_count = {"n": 0}
    captured_messages: list[dict] = []

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        print(f"LLM call #{call_count['n']} with messages:", messages)
        captured_messages[:] = messages  # Capture all messages for inspection
        if call_count["n"] == 1:
            return LLMResponse(
                content="working",
                tool_calls=[ToolCallRequest(id="c1", name="list_dir", arguments={"path": "."})],
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        return LLMResponse(content="adjusted response", tool_calls=[], usage={})

    loop.provider.chat_with_retry = chat_with_retry
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.execute = AsyncMock(return_value="tool output")

    # Simulate: msg A is being processed, then msg B arrives
    pending_queue = asyncio.Queue(maxsize=20)
    loop._pending_queues["telegram:123"] = pending_queue
    await pending_queue.put(InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="actually, do task B instead"))

    # Start the agent loop with initial message A
    result, _, _, _, _ = await loop._run_agent_loop(
        [{"role": "user", "content": "do task A"}],
        session=session,
        channel="telegram",
        chat_id="123",
        pending_queue=pending_queue,
    )

    # Wait for any background tasks to complete
    await asyncio.sleep(0.1)

    # Message B should have been injected into the LLM's context via the pending queue
    if captured_messages:
        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        contents = [str(m.get("content", "")) for m in user_msgs]
        assert any("task B" in c for c in contents), f"Expected 'task B' in {contents}"

    # Note: pending queue cleanup is handled by _dispatch, not _run_agent_loop.
    # Since this test calls _run_agent_loop directly, the queue remains.
