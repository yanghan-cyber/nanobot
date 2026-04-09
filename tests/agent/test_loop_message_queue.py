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