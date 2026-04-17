"""Tests for grep/glob search tools using ripgrep backend."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.subagent import SubagentManager, SubagentStatus
from nanobot.agent.tools.search import GlobTool, GrepTool
from nanobot.bus.queue import MessageBus


@pytest.mark.asyncio
async def test_glob_matches_recursively(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "nested").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "nested" / "util.py").write_text("print('ok')\n", encoding="utf-8")

    tool = GlobTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(pattern="*.py", path=".")

    assert "src/app.py" in result
    assert "nested/util.py" in result


@pytest.mark.asyncio
async def test_grep_respects_glob_filter(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "alpha\nbeta\nmatch_here\ngamma\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("match_here\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="match_here", path=".", glob="*.py", output_mode="content",
    )

    assert "src/main.py" in result
    assert "README.md" not in result


@pytest.mark.asyncio
async def test_grep_defaults_to_files_with_matches(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("match_here\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(pattern="match_here", path="src")

    assert "src/main.py" in result


@pytest.mark.asyncio
async def test_grep_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "HISTORY.md").write_text(
        "[2026-04-02 10:00] OAuth token rotated\n", encoding="utf-8",
    )

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="oauth", path="memory/HISTORY.md",
        case_insensitive=True, output_mode="content",
    )

    assert "memory/HISTORY.md" in result
    assert "OAuth token rotated" in result


@pytest.mark.asyncio
async def test_grep_type_filter(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "src" / "b.md").write_text("needle\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(pattern="needle", path="src", type="py")

    assert "src/a.py" in result
    assert "src/b.md" not in result


@pytest.mark.asyncio
async def test_grep_fixed_strings(tmp_path: Path) -> None:
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "HISTORY.md").write_text(
        "[2026-04-02 10:00] OAuth token rotated\n", encoding="utf-8",
    )

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="[2026-04-02 10:00]", path="memory/HISTORY.md",
        fixed_strings=True, output_mode="content",
    )

    assert "memory/HISTORY.md" in result
    assert "[2026-04-02 10:00] OAuth token rotated" in result


@pytest.mark.asyncio
async def test_grep_files_with_matches_mode_returns_unique_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    a = tmp_path / "src" / "a.py"
    b = tmp_path / "src" / "b.py"
    a.write_text("needle\nneedle\n", encoding="utf-8")
    b.write_text("needle\n", encoding="utf-8")
    os.utime(a, (1, 1))
    os.utime(b, (2, 2))

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle",
        path="src",
        output_mode="files_with_matches",
    )

    assert result.splitlines() == ["src/b.py", "src/a.py"]


@pytest.mark.asyncio
async def test_grep_files_with_matches_supports_head_limit_and_offset(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / "src" / name).write_text("needle\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle",
        path="src",
        head_limit=1,
        offset=1,
    )

    # Filesystem order is not deterministic across platforms, so just verify:
    # 1. Only one file path is returned (head_limit=1 after offset=1)
    # 2. The pagination info is correct
    assert "pagination: limit=1, offset=1" in result
    # Count non-empty lines that start with src/ (file paths)
    file_lines = [l for l in result.splitlines() if l.startswith("src/")]
    assert len(file_lines) == 1


@pytest.mark.asyncio
async def test_grep_count_mode_reports_counts_per_file(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "one.log").write_text("warn\nok\nwarn\n", encoding="utf-8")
    (tmp_path / "logs" / "two.log").write_text("warn\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="warn", path="logs", output_mode="count",
    )

    assert "logs/one.log" in result
    assert "2" in result
    assert "logs/two.log" in result


@pytest.mark.asyncio
async def test_grep_context_lines(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "line1\nline2\nMATCH\nline4\nline5\n", encoding="utf-8"
    )

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="MATCH", path=".", output_mode="content",
        context_before=1, context_after=1,
    )

    assert "src/main.py" in result
    assert "MATCH" in result


@pytest.mark.asyncio
async def test_grep_multiline_matching(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "def hello():\n    return 'world'\n", encoding="utf-8"
    )

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern=r"def hello\(\):.*return",
        path=".", output_mode="content", multiline=True,
    )

    assert "src/main.py" in result


@pytest.mark.asyncio
async def test_grep_head_limit_and_offset(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / "src" / name).write_text("needle\n", encoding="utf-8")

    tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(
        pattern="needle", path="src",
        output_mode="files_with_matches", head_limit=1, offset=1,
    )

    assert "pagination" in result


@pytest.mark.asyncio
async def test_glob_head_limit(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / "src" / name).write_text("ok\n", encoding="utf-8")

    tool = GlobTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(pattern="*.py", path="src", head_limit=1)

    lines = result.splitlines()
    assert len(lines) >= 1
    assert any(".py" in l for l in lines)


@pytest.mark.asyncio
async def test_search_tools_reject_paths_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-search.txt"
    outside.write_text("secret\n", encoding="utf-8")

    grep_tool = GrepTool(workspace=tmp_path, allowed_dir=tmp_path)
    glob_tool = GlobTool(workspace=tmp_path, allowed_dir=tmp_path)

    grep_result = await grep_tool.execute(pattern="secret", path=str(outside))
    glob_result = await glob_tool.execute(pattern="*.txt", path=str(outside.parent))

    assert grep_result.startswith("Error:")
    assert glob_result.startswith("Error:")


def test_agent_loop_registers_grep_and_glob(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    assert "grep" in loop.tools.tool_names
    assert "glob" in loop.tools.tool_names


@pytest.mark.asyncio
async def test_subagent_registers_grep_and_glob(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    mgr = SubagentManager(
        provider=provider, workspace=tmp_path, bus=bus, max_tool_result_chars=4096,
    )
    captured: dict[str, list[str]] = {}

    async def fake_run(spec):
        captured["tool_names"] = spec.tools.tool_names
        return SimpleNamespace(
            stop_reason="ok", final_content="done", tool_events=[], error=None,
        )

    mgr.runner.run = fake_run
    mgr._announce_result = AsyncMock()

    status = SubagentStatus(task_id="sub-1", label="label", task_description="search task", started_at=time.monotonic())
    await mgr._run_subagent("sub-1", "search task", "label", {"channel": "cli", "chat_id": "direct"}, status)

    assert "grep" in captured["tool_names"]
    assert "glob" in captured["tool_names"]


def test_subagent_prompt_respects_disabled_skills(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    skills_dir = tmp_path / "skills"
    (skills_dir / "alpha").mkdir(parents=True)
    (skills_dir / "alpha" / "SKILL.md").write_text("# Alpha\n\nhidden\n", encoding="utf-8")
    (skills_dir / "beta").mkdir(parents=True)
    (skills_dir / "beta" / "SKILL.md").write_text("# Beta\n\nshown\n", encoding="utf-8")

    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        max_tool_result_chars=4096,
        disabled_skills=["alpha"],
    )

    prompt = mgr._build_subagent_prompt()

    assert "alpha" not in prompt
    assert "beta" in prompt
