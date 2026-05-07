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
