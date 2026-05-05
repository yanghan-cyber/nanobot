from __future__ import annotations

import json
from pathlib import Path

from nanobot.session.manager import SessionManager


class TestSessionDbId:
    def test_new_session_has_db_id(self, tmp_path: Path) -> None:
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:direct")
        assert session.db_id != ""
        assert len(session.db_id) > 10  # YYYYMMDD_HHMMSS_<6hex>

    def test_new_session_flush_idx_zero(self, tmp_path: Path) -> None:
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:direct")
        assert session.last_db_flush_idx == 0


class TestSessionManagerSQLite:
    def test_save_writes_to_sqlite(self, tmp_path: Path) -> None:
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:direct")
        session.add_message("user", "Hello")
        mgr.save(session)
        msgs = mgr._db.get_messages(session.db_id)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello"

    def test_save_incremental_flush(self, tmp_path: Path) -> None:
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:direct")
        session.add_message("user", "First")
        mgr.save(session)
        assert session.last_db_flush_idx == 1
        session.add_message("assistant", "Second")
        mgr.save(session)
        assert session.last_db_flush_idx == 2
        msgs = mgr._db.get_messages(session.db_id)
        assert len(msgs) == 2

    def test_db_id_persisted_in_jsonl(self, tmp_path: Path) -> None:
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:direct")
        session.add_message("user", "Test")
        mgr.save(session)
        original_db_id = session.db_id
        mgr.invalidate("cli:direct")
        loaded = mgr.get_or_create("cli:direct")
        assert loaded.db_id == original_db_id

    def test_flush_idx_persisted_in_jsonl(self, tmp_path: Path) -> None:
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:direct")
        session.add_message("user", "Test")
        mgr.save(session)
        assert session.last_db_flush_idx == 1
        mgr.invalidate("cli:direct")
        loaded = mgr.get_or_create("cli:direct")
        assert loaded.last_db_flush_idx == 1

    def test_legacy_jsonl_migration(self, tmp_path: Path) -> None:
        """Old JSONL without db_id should get a new one on load."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        path = sessions_dir / "cli_direct.jsonl"
        meta = {"_type": "metadata", "key": "cli:direct"}
        path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:direct")
        assert session.db_id != ""

    def test_tool_calls_persisted(self, tmp_path: Path) -> None:
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:direct")
        session.add_message("user", "List files")
        session.messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "bash", "arguments": '{"command": "ls"}'}],
        })
        session.messages.append({
            "role": "tool",
            "content": "file1.txt\nfile2.txt",
            "tool_call_id": "tc_123",
            "name": "bash",
        })
        mgr.save(session)
        msgs = mgr._db.get_messages(session.db_id)
        assert len(msgs) == 3
        assert msgs[1]["tool_calls"] is not None
        assert msgs[2]["tool_call_id"] == "tc_123"
        assert msgs[2]["tool_name"] == "bash"

    def test_sqlite_failure_does_not_block_jsonl(self, tmp_path: Path) -> None:
        mgr = SessionManager(tmp_path)
        session = mgr.get_or_create("cli:direct")
        session.add_message("user", "Hello")
        mgr.save(session)
        # Corrupt the SQLite connection
        mgr._db._conn.close()
        session.add_message("assistant", "World")
        # Should not raise
        mgr.save(session)
        # JSONL should still be updated
        mgr.invalidate("cli:direct")
        loaded = mgr.get_or_create("cli:direct")
        assert len(loaded.messages) == 2
