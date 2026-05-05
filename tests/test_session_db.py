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
