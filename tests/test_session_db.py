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
        assert row["terminated_at"] is None

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
        assert row["terminated_at"] is not None
        assert row["termination_reason"] == "compression"

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
            tool_calls=[{"function": {"name": "bash", "arguments": '{"command": "ls"}'}}],
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


class TestSchemaMigration:
    def test_migrate_v1_to_v2(self, tmp_path: Path) -> None:
        """Test migration from v1 schema (ended_at/end_reason) to v2 (last_active_at/terminated_at/termination_reason)."""
        db_path = tmp_path / "state.db"

        # Create a v1 database manually
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL,
                applied_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'agent',
                model TEXT,
                title TEXT,
                user_id TEXT,
                parent_session_id TEXT,
                system_prompt_snapshot TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id          TEXT NOT NULL REFERENCES sessions(id),
                role                TEXT NOT NULL,
                content             TEXT,
                tool_calls          TEXT,
                tool_call_id        TEXT,
                tool_name           TEXT,
                token_count         INTEGER,
                finish_reason       TEXT,
                reasoning_content   TEXT,
                metadata            TEXT,
                created_at          REAL NOT NULL
            );
            INSERT INTO schema_version (version, applied_at) VALUES (1, 0);
            INSERT INTO sessions (id, session_key, started_at, ended_at, end_reason)
            VALUES ('old_session', 'cli:direct', 1000.0, 2000.0, 'user_new');
            INSERT INTO sessions (id, session_key, started_at)
            VALUES ('active_session', 'cli:direct', 3000.0);
            -- Session that was saved but never explicitly ended (ended_at set by save(), end_reason NULL)
            INSERT INTO sessions (id, session_key, started_at, ended_at)
            VALUES ('saved_session', 'cli:direct', 4000.0, 5000.0);
        """)
        conn.commit()
        conn.close()

        # Open with SessionDB - should trigger migration
        db = SessionDB(db_path)

        # Verify migration
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row[0] == 3

        cols = [desc[0] for desc in conn.execute("SELECT * FROM sessions LIMIT 0").description]

        # Terminated session: should have terminated_at set
        row = conn.execute("SELECT * FROM sessions WHERE id = 'old_session'").fetchone()
        row_dict = dict(zip(cols, row))
        assert row_dict["last_active_at"] is not None
        assert row_dict["terminated_at"] == 2000.0
        assert row_dict["termination_reason"] == "user_new"

        # Active session (never saved): should have null terminated_at
        row = conn.execute("SELECT * FROM sessions WHERE id = 'active_session'").fetchone()
        row_dict = dict(zip(cols, row))
        assert row_dict["last_active_at"] is None
        assert row_dict["terminated_at"] is None

        # Saved but not ended: should have last_active_at set, terminated_at NULL
        row = conn.execute("SELECT * FROM sessions WHERE id = 'saved_session'").fetchone()
        row_dict = dict(zip(cols, row))
        assert row_dict["last_active_at"] == 5000.0
        assert row_dict["terminated_at"] is None
        assert row_dict["termination_reason"] is None
        conn.close()

        # Verify get_active_session works after migration
        # Both active_session and saved_session are active (terminated_at IS NULL)
        # get_active_session returns the most recent one by started_at
        active = db.get_active_session("cli:direct")
        assert active is not None
        assert active["id"] == "saved_session"  # started_at=4000 > 3000


class TestToolInvocations:
    """Tests for the tool_invocations table and its auto-population trigger."""

    def _make_db_with_session(self, tmp_path: Path, session_id: str = "sess_001") -> SessionDB:
        db = SessionDB(tmp_path / "state.db")
        db.create_session(session_id, session_key="cli:direct", source="agent")
        return db

    def _fetch_invocations(self, db: SessionDB) -> list[dict]:
        conn = sqlite3.connect(str(db.path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM tool_invocations ORDER BY id").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def test_trigger_inserts_on_tool_calls(self, tmp_path: Path) -> None:
        db = self._make_db_with_session(tmp_path)
        db.append_message(
            "sess_001", role="assistant", content="",
            tool_calls=[{"function": {"name": "bash", "arguments": '{"command": "ls"}'}, "id": "tc_1"}],
        )
        rows = self._fetch_invocations(db)
        assert len(rows) == 1
        assert rows[0]["session_id"] == "sess_001"
        assert rows[0]["tool_name"] == "bash"
        assert rows[0]["skill_name"] is None

    def test_trigger_ignores_non_tool_messages(self, tmp_path: Path) -> None:
        db = self._make_db_with_session(tmp_path)
        db.append_message("sess_001", role="user", content="Hello")
        db.append_message("sess_001", role="assistant", content="Hi there!")
        rows = self._fetch_invocations(db)
        assert len(rows) == 0

    def test_trigger_ignores_tool_result_messages(self, tmp_path: Path) -> None:
        db = self._make_db_with_session(tmp_path)
        db.append_message(
            "sess_001", role="tool", content="output",
            tool_call_id="tc_1", tool_name="bash",
        )
        rows = self._fetch_invocations(db)
        assert len(rows) == 0

    def test_multiple_tool_calls_in_one_message(self, tmp_path: Path) -> None:
        db = self._make_db_with_session(tmp_path)
        db.append_message(
            "sess_001", role="assistant", content="",
            tool_calls=[
                {"function": {"name": "bash", "arguments": '{"command": "ls"}'}, "id": "tc_1"},
                {"function": {"name": "read_file", "arguments": '{"path": "/tmp/f"}'}, "id": "tc_2"},
                {"function": {"name": "grep", "arguments": '{"pattern": "foo"}'}, "id": "tc_3"},
            ],
        )
        rows = self._fetch_invocations(db)
        assert len(rows) == 3
        assert [r["tool_name"] for r in rows] == ["bash", "read_file", "grep"]

    def test_skill_name_extracted_from_load_skill(self, tmp_path: Path) -> None:
        db = self._make_db_with_session(tmp_path)
        db.append_message(
            "sess_001", role="assistant", content="",
            tool_calls=[{
                "function": {"name": "load_skill", "arguments": '{"skill_name": "brainstorming"}'},
                "id": "tc_1",
            }],
        )
        rows = self._fetch_invocations(db)
        assert len(rows) == 1
        assert rows[0]["tool_name"] == "load_skill"
        assert rows[0]["skill_name"] == "brainstorming"

    def test_skill_name_null_for_non_skill_tools(self, tmp_path: Path) -> None:
        db = self._make_db_with_session(tmp_path)
        db.append_message(
            "sess_001", role="assistant", content="",
            tool_calls=[{"function": {"name": "bash", "arguments": '{"command": "ls"}'}, "id": "tc_1"}],
        )
        rows = self._fetch_invocations(db)
        assert rows[0]["skill_name"] is None

    def test_cascade_delete_on_session_removal(self, tmp_path: Path) -> None:
        db = self._make_db_with_session(tmp_path)
        db.append_message(
            "sess_001", role="assistant", content="",
            tool_calls=[{"function": {"name": "bash", "arguments": '{"command": "ls"}'}, "id": "tc_1"}],
        )
        assert len(self._fetch_invocations(db)) == 1

        conn = sqlite3.connect(str(db.path))
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM messages WHERE session_id = 'sess_001'")
        conn.execute("DELETE FROM sessions WHERE id = 'sess_001'")
        conn.commit()
        conn.close()

        assert len(self._fetch_invocations(db)) == 0

    def test_accross_sessions(self, tmp_path: Path) -> None:
        db = self._make_db_with_session(tmp_path, "sess_001")
        db.create_session("sess_002", session_key="cli:other", source="agent")
        db.append_message(
            "sess_001", role="assistant", content="",
            tool_calls=[{"function": {"name": "bash", "arguments": '{"command": "ls"}'}, "id": "tc_1"}],
        )
        db.append_message(
            "sess_002", role="assistant", content="",
            tool_calls=[{"function": {"name": "grep", "arguments": '{"pattern": "foo"}'}, "id": "tc_2"}],
        )
        rows = self._fetch_invocations(db)
        assert len(rows) == 2
        assert rows[0]["session_id"] == "sess_001"
        assert rows[0]["tool_name"] == "bash"
        assert rows[1]["session_id"] == "sess_002"
        assert rows[1]["tool_name"] == "grep"


class TestToolInvocationMigration:
    """Tests for v2→v3 migration including backfill of existing data."""

    def _create_v2_db(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL,
                applied_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'agent',
                model TEXT,
                title TEXT,
                user_id TEXT,
                parent_session_id TEXT,
                system_prompt_snapshot TEXT,
                started_at REAL NOT NULL,
                last_active_at REAL,
                terminated_at REAL,
                termination_reason TEXT,
                message_count INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id          TEXT NOT NULL REFERENCES sessions(id),
                role                TEXT NOT NULL,
                content             TEXT,
                tool_calls          TEXT,
                tool_call_id        TEXT,
                tool_name           TEXT,
                token_count         INTEGER,
                finish_reason       TEXT,
                reasoning_content   TEXT,
                metadata            TEXT,
                created_at          REAL NOT NULL
            );
            INSERT INTO schema_version (version, applied_at) VALUES (2, 0);
            INSERT INTO sessions (id, session_key, started_at)
            VALUES ('sess_old', 'cli:direct', 1000.0);
            INSERT INTO messages (session_id, role, content, tool_calls, created_at)
            VALUES ('sess_old', 'assistant', '',
                    '[{"function":{"name":"bash","arguments":"{\\"command\\":\\"ls\\"}"},"id":"tc_1"}]',
                    1001.0);
            INSERT INTO messages (session_id, role, content, tool_calls, created_at)
            VALUES ('sess_old', 'user', 'hello', NULL, 1002.0);
            INSERT INTO messages (session_id, role, content, tool_calls, created_at)
            VALUES ('sess_old', 'assistant', '',
                    '[{"function":{"name":"load_skill","arguments":"{\\"skill_name\\":\\"brainstorming\\"}"},"id":"tc_2"}]',
                    1003.0);
        """)
        conn.commit()
        conn.close()

    def test_migration_creates_tool_invocations_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        self._create_v2_db(db_path)
        SessionDB(db_path)
        conn = sqlite3.connect(str(db_path))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "tool_invocations" in tables

    def test_migration_backfills_existing_tool_calls(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        self._create_v2_db(db_path)
        SessionDB(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM tool_invocations ORDER BY id"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0]["tool_name"] == "bash"
        assert rows[0]["session_id"] == "sess_old"
        assert rows[1]["tool_name"] == "load_skill"
        assert rows[1]["skill_name"] == "brainstorming"

    def test_migration_creates_views(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        self._create_v2_db(db_path)
        SessionDB(db_path)
        conn = sqlite3.connect(str(db_path))
        views = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()}
        conn.close()
        assert "v_tool_stats" in views
        assert "v_skill_stats" in views

    def test_tool_stats_view(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        self._create_v2_db(db_path)
        SessionDB(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM v_tool_stats").fetchall()
        conn.close()
        by_name = {r["tool_name"]: r["total_calls"] for r in rows}
        assert by_name["bash"] == 1
        assert by_name["load_skill"] == 1

    def test_skill_stats_view(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        self._create_v2_db(db_path)
        SessionDB(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM v_skill_stats").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["skill_name"] == "brainstorming"
        assert rows[0]["total_calls"] == 1

    def test_migration_schema_version(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        self._create_v2_db(db_path)
        SessionDB(db_path)
        conn = sqlite3.connect(str(db_path))
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        conn.close()
        assert version == 3
