from __future__ import annotations

import sqlite3
from pathlib import Path

from nanobot.session.db import SessionDB


class TestSessionDBInit:
    def test_creates_database_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        SessionDB(db_path)
        assert db_path.exists()

    def test_creates_sessions_table(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        conn = sqlite3.connect(str(db.path))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "sessions" in tables

    def test_creates_messages_table(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        conn = sqlite3.connect(str(db.path))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "messages" in tables

    def test_creates_fts5_tables(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        conn = sqlite3.connect(str(db.path))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "messages_fts" in tables
        assert "messages_fts_trigram" in tables

    def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        conn = sqlite3.connect(str(db.path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"


class TestSessionCRUD:
    def test_create_session(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent", model="gpt-4")
        row = db.get_session("sess_001")
        assert row is not None
        assert row["id"] == "sess_001"
        assert row["session_key"] == "cli:direct"
        assert row["source"] == "agent"
        assert row["model"] == "gpt-4"
        assert row["ended_at"] is None

    def test_ensure_session_idempotent(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.ensure_session("sess_001", session_key="cli:direct", source="agent", model="gpt-4")
        db.ensure_session("sess_001", session_key="cli:direct", source="agent", model="gpt-4")
        row = db.get_session("sess_001")
        assert row is not None
        assert row["id"] == "sess_001"

    def test_end_session(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.end_session("sess_001", "compression")
        row = db.get_session("sess_001")
        assert row["ended_at"] is not None
        assert row["end_reason"] == "compression"

    def test_end_nonexistent_session_is_noop(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.end_session("nonexistent", "manual")

    def test_get_session_returns_none_for_missing(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        assert db.get_session("nonexistent") is None

    def test_get_active_session(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        active = db.get_active_session("cli:direct")
        assert active is not None
        assert active["id"] == "sess_001"

    def test_get_active_session_returns_none_when_ended(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.end_session("sess_001", "manual")
        assert db.get_active_session("cli:direct") is None

    def test_update_session(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.update_session("sess_001", title="Test Session", input_tokens=100, output_tokens=50)
        row = db.get_session("sess_001")
        assert row["title"] == "Test Session"
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50

    def test_update_session_ignores_unknown_columns(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.update_session("sess_001", bogus_field="oops")
        row = db.get_session("sess_001")
        assert row["title"] is None


class TestMessageCRUD:
    def test_append_message(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.append_message("sess_001", role="user", content="Hello")
        db.append_message("sess_001", role="assistant", content="Hi there!")
        msgs = db.get_messages("sess_001")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello"
        assert msgs[1]["role"] == "assistant"

    def test_append_message_with_tool_calls(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.append_message(
            "sess_001", role="assistant", content="",
            tool_calls=[{"name": "bash", "arguments": '{"command": "ls"}'}],
        )
        msgs = db.get_messages("sess_001")
        assert "bash" in msgs[0]["tool_calls"]

    def test_get_messages_empty(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        assert db.get_messages("sess_001") == []

    def test_message_count_updated(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.append_message("sess_001", role="user", content="Hello")
        db.append_message("sess_001", role="assistant", content="Hi")
        row = db.get_session("sess_001")
        assert row["message_count"] == 2


class TestTitleManagement:
    def test_set_and_get_title(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.set_session_title("sess_001", "Debug API")
        assert db.get_session_title("sess_001") == "Debug API"

    def test_get_title_returns_none_for_missing(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        assert db.get_session_title("nonexistent") is None

    def test_next_title_in_lineage_first(self) -> None:
        assert SessionDB.get_next_title_in_lineage("Debug API") == "Debug API #2"

    def test_next_title_in_lineage_increments(self) -> None:
        assert SessionDB.get_next_title_in_lineage("Debug API #2") == "Debug API #3"


class TestTokenCounts:
    def test_update_token_counts_additive(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.update_token_counts("sess_001", input_tokens=100, output_tokens=50, cache_read_tokens=20)
        db.update_token_counts("sess_001", input_tokens=30, output_tokens=10, cache_read_tokens=5)
        row = db.get_session("sess_001")
        assert row["input_tokens"] == 130
        assert row["output_tokens"] == 60
        assert row["cache_read_tokens"] == 25


class TestListRecentSessions:
    def test_list_recent_sessions(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("s1", session_key="cli:direct", source="agent", model="gpt-4")
        db.append_message("s1", role="user", content="Hi")
        db.update_session("s1", title="Test Chat")
        sessions = db.list_recent_sessions(limit=5)
        assert len(sessions) >= 1
        assert sessions[0]["id"] == "s1"
        assert sessions[0]["title"] == "Test Chat"

    def test_list_recent_empty(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        assert db.list_recent_sessions() == []


class TestFTS5Search:
    def test_search_finds_matching_content(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.append_message("sess_001", role="user", content="How to deploy docker containers?")
        db.append_message("sess_001", role="assistant", content="Use docker-compose up")
        results = db.search_messages("docker")
        assert len(results) > 0

    def test_search_returns_session_info(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.append_message("sess_001", role="user", content="Python testing with pytest")
        results = db.search_messages("pytest")
        assert results[0]["session_id"] == "sess_001"

    def test_search_no_match_returns_empty(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.append_message("sess_001", role="user", content="Hello world")
        results = db.search_messages("nonexistent_topic_xyz")
        assert results == []

    def test_search_or_syntax(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.append_message("sess_001", role="user", content="Deploying with kubernetes")
        results = db.search_messages("docker OR kubernetes")
        assert len(results) > 0

    def test_search_cjk_trigram(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.append_message("sess_001", role="user", content="今天天气真好我们去公园散步吧")
        results = db.search_messages("天气真好")
        assert len(results) > 0

    def test_search_cjk_short_like_fallback(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.append_message("sess_001", role="user", content="测试中文搜索功能")
        results = db.search_messages("搜索")
        assert len(results) > 0

    def test_search_with_role_filter(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.append_message("sess_001", role="user", content="Deploy docker")
        db.append_message("sess_001", role="assistant", content="Use docker-compose")
        results = db.search_messages("docker", role_filter=["user"])
        assert len(results) == 1
        assert results[0]["role"] == "user"

    def test_search_exclude_sources(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="agent")
        db.create_session("sess_002", session_key="sub:t1", source="subagent")
        db.append_message("sess_001", role="user", content="Deploy docker containers")
        db.append_message("sess_002", role="user", content="docker build")
        results = db.search_messages("docker", exclude_sources=["subagent"])
        assert all(r["session_id"] == "sess_001" for r in results)

    def test_contains_cjk(self) -> None:
        from nanobot.session.db import _contains_cjk
        assert _contains_cjk("中文测试") is True
        assert _contains_cjk("hello world") is False
        assert _contains_cjk("mixed 中文 mixed") is True

    def test_sanitize_fts5_query(self) -> None:
        from nanobot.session.db import SessionDB
        assert SessionDB._sanitize_fts5_query('hello world') == 'hello world'
        assert SessionDB._sanitize_fts5_query('"exact phrase"') == '"exact phrase"'
        assert SessionDB._sanitize_fts5_query('test -exclude') == 'test exclude'
