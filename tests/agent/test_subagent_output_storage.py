"""Tests for subagent output storage (file-write + tail logic)."""

import pytest
from unittest.mock import MagicMock

from nanobot.bus.queue import MessageBus

_MAX_TOOL_RESULT_CHARS = 100_000


def _make_mgr():
    from nanobot.agent.subagent import SubagentManager

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider,
        workspace=MagicMock(),
        bus=bus,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    )
    return mgr, bus


@pytest.mark.asyncio
async def test_short_output_returned_in_full():
    """Output <= 50 lines is returned verbatim (file still written)."""
    from nanobot.agent.subagent import _subagent_output_path

    mgr, bus = _make_mgr()
    task_id = "short-test-1"
    result = "line1\nline2\nline3"

    await mgr._announce_result(task_id, "test", result, {"channel": "cli", "chat_id": "d"}, "ok")

    msg = await bus.consume_inbound()
    assert result in msg.content
    assert "full output saved to" not in msg.content

    path = _subagent_output_path(task_id)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == result
    path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_long_output_returns_tail_with_file_hint():
    """Output > 50 lines returns tail + file path hint."""
    from nanobot.agent.subagent import _subagent_output_path, _TAIL_LINES

    mgr, bus = _make_mgr()
    task_id = "long-test-1"
    lines = [f"line {i}" for i in range(100)]
    result = "\n".join(lines)

    await mgr._announce_result(task_id, "test", result, {"channel": "cli", "chat_id": "d"}, "ok")

    msg = await bus.consume_inbound()
    content = msg.content
    # Last 50 lines should be present
    assert f"line 99" in content
    assert f"line 50" in content
    # First lines should not be in the displayed content
    assert f"line 0\n" not in content
    # File path hint present
    assert "full output saved to:" in content
    assert f"last {_TAIL_LINES} of 100 lines" in content

    path = _subagent_output_path(task_id)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == result
    path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_write_failure_graceful_fallback():
    """If file write fails, announcement still proceeds without file hint."""
    mgr, bus = _make_mgr()
    task_id = "fail-write-1"
    lines = [f"line {i}" for i in range(60)]
    result = "\n".join(lines)

    from unittest.mock import patch
    with patch("nanobot.agent.subagent.Path.write_text", side_effect=OSError("disk full")):
        await mgr._announce_result(task_id, "test", result, {"channel": "cli", "chat_id": "d"}, "ok")

    msg = await bus.consume_inbound()
    assert msg is not None
    # Tail should still be present
    assert "line 59" in msg.content
    # No file hint (write failed)
    assert "full output saved to:" not in msg.content
    # Still indicates truncation
    assert "last 50 of 60 lines" in msg.content


@pytest.mark.asyncio
async def test_empty_result_handled():
    """Empty result writes an empty file and announces normally."""
    from nanobot.agent.subagent import _subagent_output_path

    mgr, bus = _make_mgr()
    task_id = "empty-test-1"

    await mgr._announce_result(task_id, "test", "", {"channel": "cli", "chat_id": "d"}, "ok")

    msg = await bus.consume_inbound()
    assert msg is not None
    assert "full output saved to" not in msg.content

    path = _subagent_output_path(task_id)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == ""
    path.unlink(missing_ok=True)
