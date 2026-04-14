"""Tests for nanobot.agent.tools.skill.LoadSkillTool."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.skill import LoadSkillTool


def _write_skill(
    base: Path,
    name: str,
    *,
    description: str = "Test skill",
    body: str = "# Skill\n",
) -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    lines = ["---", f"name: {name}", f"description: {description}", "---", "", body]
    path = skill_dir / "SKILL.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_load_skill_returns_path_and_body(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    skill_path = _write_skill(ws_skills, "alpha", body="# Alpha content\n")
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    tool = LoadSkillTool(skills_loader=loader)
    result = _run(tool.execute(skill_name="alpha"))
    assert f"File: {skill_path}" in result
    assert "# Alpha content" in result
    # Frontmatter should NOT appear
    assert "---" not in result


def test_load_skill_not_found(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    tool = LoadSkillTool(skills_loader=loader)
    result = _run(tool.execute(skill_name="missing"))
    assert "Error" in result
    assert "not found" in result.lower()


def test_load_skill_not_found_lists_available(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    ws_skills = workspace / "skills"
    ws_skills.mkdir(parents=True)
    _write_skill(ws_skills, "alpha")
    _write_skill(ws_skills, "beta")
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    tool = LoadSkillTool(skills_loader=loader)
    result = _run(tool.execute(skill_name="missing"))
    assert "alpha" in result
    assert "beta" in result


def test_load_skill_read_only(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    tool = LoadSkillTool(skills_loader=loader)
    assert tool.read_only is True


def test_load_skill_name_property(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    builtin = tmp_path / "builtin"
    builtin.mkdir()

    loader = SkillsLoader(workspace, builtin_skills_dir=builtin)
    tool = LoadSkillTool(skills_loader=loader)
    assert tool.name == "load_skill"
