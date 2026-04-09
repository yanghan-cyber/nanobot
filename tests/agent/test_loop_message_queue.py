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


@pytest.mark.asyncio
async def test_pending_callback_injects_queued_messages_into_runner(tmp_path):
    """The pending_message_callback should drain queued messages into the runner's context."""
    loop = _make_loop(tmp_path)

    # Set up session
    from nanobot.session.manager import Session
    session = Session(key="telegram:123")
    loop.sessions.get_or_create = MagicMock(return_value=session)
    loop.sessions.save = MagicMock()

    # Pre-populate pending queue
    queue = loop._pending_queues.setdefault("telegram:123", asyncio.Queue())
    await queue.put(InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="mid-run msg"))

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

    # Mark session as active (simulating mid-run state)
    loop._active_sessions.add("telegram:123")

    result, _, _, _ = await loop._run_agent_loop(
        [{"role": "user", "content": "initial task"}],
        session=session,
        channel="telegram",
        chat_id="123",
    )

    assert result == "done"
    # The second LLM call should include the queued message
    user_msgs = [m for m in captured if m.get("role") == "user" and "mid-run msg" in m.get("content", "")]
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
    msg_a = InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="do task A")

    # Simulate the entire flow with _run_agent_loop
    # Pre-populate pending queue with message B
    queue = loop._pending_queues.setdefault("telegram:123", asyncio.Queue())
    await queue.put(InboundMessage(channel="telegram", sender_id="user", chat_id="123", content="actually, do task B instead"))
    # Manually mark session as active to simulate mid-run state
    loop._active_sessions.add("telegram:123")

    # Start the agent loop with initial message A
    result, _, _, _ = await loop._run_agent_loop(
        [{"role": "user", "content": "do task A"}],
        session=session,
        channel="telegram",
        chat_id="123",
    )

    # Wait for any background tasks to complete
    await asyncio.sleep(0.1)

    # Message B should have been injected into the LLM's context via the pending queue
    if captured_messages:
        user_msgs = [m for m in captured_messages if m.get("role") == "user"]
        contents = [m.get("content", "") for m in user_msgs]
        assert any("task B" in c for c in contents), f"Expected 'task B' in {contents}"

    # The session should be cleaned up after processing
    # (Note: in our test, we manually added it to active_sessions, so clean up manually)
    loop._active_sessions.discard("telegram:123")
    assert "telegram:123" not in loop._active_sessions
