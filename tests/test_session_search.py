from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.tools.session_search import SessionSearchTool
from nanobot.session.db import SessionDB


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


@pytest.fixture
def tool(db: SessionDB) -> SessionSearchTool:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=MagicMock(content="Summary of docker deployment discussion")
    )
    return SessionSearchTool(db=db, provider=provider, model="test-model")


class TestSessionSearchToolSchema:
    def test_name(self, tool: SessionSearchTool) -> None:
        assert tool.name == "session_search"

    def test_schema_has_query_param(self, tool: SessionSearchTool) -> None:
        props = tool.schema["parameters"]["properties"]
        assert "query" in props

    def test_schema_has_limit_param(self, tool: SessionSearchTool) -> None:
        props = tool.schema["parameters"]["properties"]
        assert "limit" in props

    def test_schema_has_role_filter_param(self, tool: SessionSearchTool) -> None:
        props = tool.schema["parameters"]["properties"]
        assert "role_filter" in props

    def test_read_only(self, tool: SessionSearchTool) -> None:
        assert tool.read_only is True


class TestSessionSearchRecent:
    @pytest.mark.asyncio
    async def test_recent_sessions_no_query(
        self, tool: SessionSearchTool, db: SessionDB
    ) -> None:
        db.create_session("s1", session_key="cli:direct", source="agent", model="gpt-4")
        db.append_message("s1", role="user", content="Hello")
        db.update_session("s1", title="Test Chat")
        result = await tool.execute()
        assert "s1" in result
        assert "Test Chat" in result

    @pytest.mark.asyncio
    async def test_recent_sessions_empty_db(self, tool: SessionSearchTool) -> None:
        result = await tool.execute()
        assert "no sessions" in result.lower()


class TestSessionSearchKeyword:
    @pytest.mark.asyncio
    async def test_keyword_search_finds_match(
        self, tool: SessionSearchTool, db: SessionDB
    ) -> None:
        db.create_session("s1", session_key="cli:direct", source="agent", model="gpt-4")
        db.append_message("s1", role="user", content="How to deploy docker containers?")
        db.update_session("s1", title="Docker Deploy")
        result = await tool.execute(query="docker")
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed["count"] >= 1
        assert any(r["session_id"] == "s1" for r in parsed["results"])

    @pytest.mark.asyncio
    async def test_keyword_search_no_match(
        self, tool: SessionSearchTool, db: SessionDB
    ) -> None:
        db.create_session("s1", session_key="cli:direct", source="agent")
        db.append_message("s1", role="user", content="Hello world")
        result = await tool.execute(query="nonexistent_topic_xyz")
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed["count"] == 0

    @pytest.mark.asyncio
    async def test_keyword_search_excludes_current_session_lineage(
        self, tool: SessionSearchTool, db: SessionDB
    ) -> None:
        from nanobot.agent.loop import _current_session_id
        db.create_session("s1", session_key="cli:direct", source="main")
        db.append_message("s1", role="user", content="docker deployment")
        db.create_session("s2", session_key="cli:direct", source="main", parent_session_id="s1")
        db.append_message("s2", role="user", content="kubernetes deployment")
        _current_session_id.set("s2")
        try:
            result = await tool.execute(query="docker")
        finally:
            _current_session_id.set(None)
        parsed = json.loads(result)
        assert all(r["session_id"] not in ("s1", "s2") for r in parsed["results"])

    @pytest.mark.asyncio
    async def test_limit_clamped_to_range(self, tool: SessionSearchTool) -> None:
        result = await tool.execute(limit=10)
        assert isinstance(result, str)
