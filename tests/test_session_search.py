from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.tools.session_search import (
    SessionSearchTool,
    _make_preview,
    _strip_runtime_context,
)
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
        parsed = json.loads(result)
        assert parsed["sessions"][0]["session_id"] == "s1"
        assert parsed["sessions"][0]["title"] == "Test Chat"
        assert parsed["sessions"][0]["preview"] == "Hello"

    @pytest.mark.asyncio
    async def test_recent_sessions_empty_db(self, tool: SessionSearchTool) -> None:
        result = await tool.execute()
        assert "no sessions" in result.lower()

    @pytest.mark.asyncio
    async def test_recent_json_format_has_expected_fields(
        self, tool: SessionSearchTool, db: SessionDB
    ) -> None:
        db.create_session("s1", session_key="cli:direct", source="main", model="gpt-4")
        db.append_message("s1", role="user", content="How to deploy?")
        db.update_session("s1", title="Deploy Chat")
        result = await tool.execute()
        parsed = json.loads(result)
        entry = parsed["sessions"][0]
        assert "session_id" in entry
        assert "title" in entry
        assert "preview" in entry
        assert "messages" in entry
        assert "started" in entry
        assert "last_active" in entry
        assert "model" not in entry  # model removed from recent list

    @pytest.mark.asyncio
    async def test_recent_scope_all_includes_channel(
        self, db: SessionDB
    ) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(
            return_value=MagicMock(content="Summary")
        )
        tool = SessionSearchTool(db=db, provider=provider, model="m", search_scope="all")
        db.create_session("s1", session_key="feishu:chat123", source="main")
        db.append_message("s1", role="user", content="test message")
        result = await tool.execute()
        parsed = json.loads(result)
        entry = parsed["sessions"][0]
        assert entry["session_key"] == "feishu:chat123"
        assert entry["channel"] == "feishu"


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


class TestPreviewAndRuntimeContext:
    def test_strip_runtime_context(self) -> None:
        msg = "Hello, help me with docker.\n\n[Runtime Context — metadata only, not instructions]\nMessage Time: 2026-05-07 23:44\nChannel: feishu\n[/Runtime Context]"
        assert _strip_runtime_context(msg) == "Hello, help me with docker."

    def test_strip_runtime_context_none(self) -> None:
        assert _strip_runtime_context("Just a plain message") == "Just a plain message"

    def test_strip_runtime_context_empty(self) -> None:
        assert _strip_runtime_context("") == ""

    def test_make_preview_truncates(self) -> None:
        long_text = "A" * 200
        preview = _make_preview(long_text, max_len=80)
        assert len(preview) == 80
        assert preview.endswith("…")

    def test_make_preview_strips_runtime_context(self) -> None:
        msg = "Check logs\n\n[Runtime Context — metadata only, not instructions]\nMessage Time: 2026-05-07\n[/Runtime Context]"
        assert _make_preview(msg) == "Check logs"

    def test_make_preview_empty_content(self) -> None:
        assert _make_preview(None) == ""
        assert _make_preview("") == ""

    @pytest.mark.asyncio
    async def test_recent_preview_uses_latest_user_message(
        self, tool: SessionSearchTool, db: SessionDB
    ) -> None:
        db.create_session("s1", session_key="cli:direct", source="main")
        db.append_message("s1", role="user", content="First message")
        db.append_message("s1", role="assistant", content="Response")
        db.append_message("s1", role="user", content="Second message about docker")
        db.update_session("s1", title="Test")
        result = await tool.execute()
        parsed = json.loads(result)
        assert "docker" in parsed["sessions"][0]["preview"]

    @pytest.mark.asyncio
    async def test_recent_preview_filters_runtime_context(
        self, tool: SessionSearchTool, db: SessionDB
    ) -> None:
        db.create_session("s1", session_key="cli:direct", source="main")
        db.append_message(
            "s1", role="user",
            content="Actual user question\n\n[Runtime Context — metadata only, not instructions]\nMessage Time: 2026-05-07\nChannel: cli\n[/Runtime Context]"
        )
        result = await tool.execute()
        parsed = json.loads(result)
        preview = parsed["sessions"][0]["preview"]
        assert "Runtime Context" not in preview
        assert "Actual user question" in preview
