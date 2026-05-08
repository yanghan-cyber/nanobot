"""Focused tests for MyTool runtime sync side effects."""

from unittest.mock import MagicMock

import pytest

from nanobot.agent.tools.self import MyTool


@pytest.mark.asyncio
async def test_my_tool_max_iterations_syncs_subagent_limit() -> None:
    loop = MagicMock()
    loop.max_iterations = 40
    loop._runtime_vars = {}
    loop.subagents = MagicMock()
    loop.subagents.max_iterations = loop.max_iterations

    def _sync_subagent_runtime_limits() -> None:
        loop.subagents.max_iterations = loop.max_iterations

    loop._sync_subagent_runtime_limits = _sync_subagent_runtime_limits

    tool = MyTool(loop=loop)

    result = await tool.execute(action="set", key="max_iterations", value=80)

    assert "Set max_iterations = 80" in result
    assert loop.max_iterations == 80
    assert loop.subagents.max_iterations == 80


@pytest.mark.asyncio
async def test_my_tool_model_change_syncs_heartbeat() -> None:
    """Setting model via MyTool must propagate to HeartbeatService."""
    loop = MagicMock()
    loop.model = "glm-5.1"
    loop._runtime_vars = {}

    heartbeat = MagicMock()
    loop._heartbeat = heartbeat

    tool = MyTool(loop=loop)

    result = await tool.execute(action="set", key="model", value="deepseek-v4-flash")

    assert "Set model = 'deepseek-v4-flash'" in result
    assert loop.model == "deepseek-v4-flash"
    heartbeat.set_model.assert_called_once_with("deepseek-v4-flash")


@pytest.mark.asyncio
async def test_my_tool_model_change_no_heartbeat() -> None:
    """Setting model when no heartbeat is attached must not crash."""
    loop = MagicMock()
    loop.model = "glm-5.1"
    loop._runtime_vars = {}
    loop._heartbeat = None

    tool = MyTool(loop=loop)

    result = await tool.execute(action="set", key="model", value="deepseek-v4-flash")

    assert "Set model = 'deepseek-v4-flash'" in result
    assert loop.model == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_my_tool_model_change_syncs_consolidator_and_dream() -> None:
    """Setting model via MyTool must propagate to Consolidator and Dream."""
    loop = MagicMock()
    loop.model = "glm-5.1"
    loop._runtime_vars = {}
    loop.provider = MagicMock()
    loop.context_window_tokens = 200_000
    loop._heartbeat = None

    consolidator = MagicMock()
    dream = MagicMock()
    loop.consolidator = consolidator
    loop.dream = dream

    tool = MyTool(loop=loop)

    result = await tool.execute(action="set", key="model", value="deepseek-v4-flash")

    assert "Set model = 'deepseek-v4-flash'" in result
    consolidator.set_provider.assert_called_once_with(
        loop.provider, "deepseek-v4-flash", 200_000,
    )
    dream.set_provider.assert_called_once_with(
        loop.provider, "deepseek-v4-flash",
    )


@pytest.mark.asyncio
async def test_my_tool_model_change_all_components_present() -> None:
    """Setting model with heartbeat, consolidator, dream, and subagent all present."""
    loop = MagicMock()
    loop.model = "glm-5.1"
    loop._runtime_vars = {}
    loop.provider = MagicMock()
    loop.context_window_tokens = 200_000

    heartbeat = MagicMock()
    consolidator = MagicMock()
    dream = MagicMock()
    subagents = MagicMock()
    loop._heartbeat = heartbeat
    loop.consolidator = consolidator
    loop.dream = dream
    loop.subagents = subagents

    tool = MyTool(loop=loop)

    result = await tool.execute(action="set", key="model", value="deepseek-v4-flash")

    assert "Set model = 'deepseek-v4-flash'" in result
    heartbeat.set_model.assert_called_once_with("deepseek-v4-flash")
    consolidator.set_provider.assert_called_once_with(
        loop.provider, "deepseek-v4-flash", 200_000,
    )
    dream.set_provider.assert_called_once_with(
        loop.provider, "deepseek-v4-flash",
    )
    subagents.set_provider.assert_called_once_with(
        loop.provider, "deepseek-v4-flash",
    )


@pytest.mark.asyncio
async def test_my_tool_model_change_syncs_subagent() -> None:
    """Setting model via MyTool must propagate to SubagentManager."""
    loop = MagicMock()
    loop.model = "glm-5.1"
    loop._runtime_vars = {}
    loop.provider = MagicMock()
    loop._heartbeat = None

    subagents = MagicMock()
    loop.subagents = subagents

    tool = MyTool(loop=loop)

    result = await tool.execute(action="set", key="model", value="deepseek-v4-flash")

    assert "Set model = 'deepseek-v4-flash'" in result
    subagents.set_provider.assert_called_once_with(
        loop.provider, "deepseek-v4-flash",
    )
