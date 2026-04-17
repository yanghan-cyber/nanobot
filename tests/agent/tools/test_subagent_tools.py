"""Tests for subagent tool registration and wiring."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.config.schema import AgentDefaults

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


@pytest.mark.asyncio
async def test_subagent_bash_tool_receives_allowed_env_keys(tmp_path):
    """allowed_env_keys from BashToolConfig must be forwarded to the subagent's BashTool."""
    from nanobot.agent.subagent import SubagentManager, SubagentStatus
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import BashToolConfig

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        bash_config=BashToolConfig(allowed_env_keys=["GOPATH", "JAVA_HOME"]),
    )
    mgr._announce_result = AsyncMock()

    async def fake_run(spec):
        bash_tool = spec.tools.get("bash")
        assert bash_tool is not None
        assert bash_tool.allowed_env_keys == ["GOPATH", "JAVA_HOME"]
        return SimpleNamespace(
            stop_reason="done",
            final_content="done",
            error=None,
            tool_events=[],
        )

    mgr.runner.run = AsyncMock(side_effect=fake_run)

    status = SubagentStatus(
        task_id="sub-1", label="label", task_description="do task", started_at=time.monotonic()
    )
    await mgr._run_subagent(
        "sub-1", "do task", "label", {"channel": "test", "chat_id": "c1"}, status
    )

    mgr.runner.run.assert_awaited_once()
