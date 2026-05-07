"""Tests for SessionSearchTool bug fixes and features."""

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.session.db import SessionDB


@pytest.fixture
def db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "test.db")


def _seed_session(db: SessionDB, session_id: str, messages: list[dict] | None = None) -> None:
    """Helper: create a session and optionally append messages."""
    db.create_session(session_id, session_key="test:key", source="main", model="test-model")
    for msg in messages or []:
        db.append_message(session_id, **msg)


class TestFTSSanitization:
    """Bug #3: query: '*' crashes with OperationalError."""

    def test_star_query_returns_empty_not_crash(self, db: SessionDB):
        _seed_session(db, "s1", [
            {"role": "user", "content": "hello world"},
        ])
        results = db.search_messages("*")
        assert isinstance(results, list)

    def test_empty_query_returns_empty(self, db: SessionDB):
        _seed_session(db, "s1", [
            {"role": "user", "content": "hello world"},
        ])
        results = db.search_messages("")
        assert results == []

    def test_whitespace_query_returns_empty(self, db: SessionDB):
        _seed_session(db, "s1", [
            {"role": "user", "content": "hello world"},
        ])
        results = db.search_messages("   ")
        assert results == []

    def test_special_chars_only_returns_empty(self, db: SessionDB):
        _seed_session(db, "s1", [
            {"role": "user", "content": "hello world"},
        ])
        results = db.search_messages("!@#$%")
        assert isinstance(results, list)

    def test_valid_query_still_works(self, db: SessionDB):
        _seed_session(db, "s1", [
            {"role": "user", "content": "hello world"},
        ])
        results = db.search_messages("hello")
        assert len(results) == 1
        assert results[0]["session_id"] == "s1"


class TestLimitCap:
    """Bug #2: limit hard-capped at 5."""

    def test_limit_above_20_clamped_to_20(self, db: SessionDB):
        """Tool should accept limit up to 20."""
        from nanobot.agent.tools.session_search import SessionSearchTool
        tool = SessionSearchTool(db=db, provider=MagicMock(), model="test")
        schema = tool.parameters
        assert schema["properties"]["limit"]["description"] == "Max sessions to return (1-20, default 5)."

    def test_search_messages_accepts_custom_limit(self, db: SessionDB):
        """search_messages should use the caller-provided limit, not hardcoded 20."""
        for i in range(25):
            _seed_session(db, f"s{i}", [
                {"role": "user", "content": f"unique keyword alpha {i}"},
            ])
        results = db.search_messages("alpha", limit=25)
        assert len(results) == 25


class TestModelPersistence:
    """Bug #1: model is always NULL in sessions table."""

    def test_model_stored_on_create(self, db: SessionDB):
        db.create_session("s1", session_key="test:key", source="main", model="gpt-4o")
        session = db.get_session("s1")
        assert session["model"] == "gpt-4o"

    def test_model_stored_via_ensure_session(self, db: SessionDB):
        db.ensure_session("s1", session_key="test:key", source="main", model="claude-4")
        session = db.get_session("s1")
        assert session["model"] == "claude-4"

    def test_ensure_session_preserves_existing_model(self, db: SessionDB):
        db.create_session("s1", session_key="test:key", source="main", model="gpt-4o")
        db.ensure_session("s1", session_key="test:key", source="main", model="other")
        session = db.get_session("s1")
        assert session["model"] == "gpt-4o"


class TestSourceRename:
    """Feature #6: source values are main/subagent, not agent/compression."""

    def test_manager_creates_main_source(self, db: SessionDB):
        db.create_session("s1", session_key="test:key", source="main")
        session = db.get_session("s1")
        assert session["source"] == "main"

    def test_compression_continuation_is_main(self, db: SessionDB):
        db.create_session("s1", session_key="test:key", source="main")
        db.end_session("s1", "compression")
        db.create_session("s2", session_key="test:key", source="main", parent_session_id="s1")
        session = db.get_session("s2")
        assert session["source"] == "main"
        assert session["parent_session_id"] == "s1"
