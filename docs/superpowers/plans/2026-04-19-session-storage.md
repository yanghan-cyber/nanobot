# Session Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add SQLite + FTS5 session storage to nanobot for cross-session memory search.

**Architecture:** New `SessionDB` class manages SQLite at `~/.nanobot/state.db`. Integrated into existing `SessionManager.save()` as incremental write alongside JSONL. Session splitting on autocompact, subagent recording, and `session_search` built-in tool.

**Tech Stack:** Python 3.11+, SQLite3 (stdlib), FTS5, pytest

**Design spec:** `docs/superpowers/specs/2026-04-19-session-storage-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `nanobot/session/db.py` | CREATE | SessionDB class — SQLite CRUD, FTS5 search, write retry |
| `nanobot/session/manager.py` | MODIFY | Add db_id/last_db_flush_idx to Session, SQLite writes in save/load |
| `nanobot/agent/tools/session_search.py` | CREATE | session_search built-in tool (FTS5 search + LLM summary) |
| `nanobot/agent/autocompact.py` | MODIFY | Session split on compression |
| `nanobot/agent/subagent.py` | MODIFY | Subagent session recording to SQLite |
| `nanobot/agent/loop.py` | MODIFY | Register session_search tool, pass sessions to SubagentManager |
| `tests/test_session_db.py` | CREATE | SessionDB unit tests |
| `tests/test_session_search.py` | CREATE | SessionSearchTool tests |

---

### Task 1: SessionDB — Schema, Init, and Session CRUD

**Files:**
- Create: `nanobot/session/db.py`
- Create: `tests/test_session_db.py`

- [ ] **Step 1: Write failing test for SessionDB init and schema**

Create `tests/test_session_db.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nanobot.session.db import SessionDB


class TestSessionDBInit:
    def test_creates_database_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "state.db"
        db = SessionDB(db_path)
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

    def test_creates_fts5_index(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        conn = sqlite3.connect(str(db.path))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "messages_fts" in tables

    def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        conn = sqlite3.connect(str(db.path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nanobot.session.db'`

- [ ] **Step 3: Implement SessionDB init with schema**

Create `nanobot/session/db.py`:

```python
"""SQLite-backed session storage with FTS5 full-text search."""

from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'cli',
    user_id TEXT,
    model TEXT,
    title TEXT,
    parent_session_id TEXT,
    end_reason TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    system_prompt_snapshot TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning_content TEXT
);
"""

_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(session_key, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
"""


def generate_session_id() -> str:
    """Generate a unique session ID: YYYYMMDD_HHMMSS_<6hex>."""
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


class SessionDB:
    """SQLite-backed session storage with FTS5 search."""

    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020
    _WRITE_RETRY_MAX_S = 0.150

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.executescript(_FTS_SQL)
        self._conn.executescript(_INDEX_SQL)
        row = self._conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
        if row[0] == 0:
            self._conn.execute("INSERT INTO schema_version VALUES (?)", (_SCHEMA_VERSION,))
        self._conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_session_db.py::TestSessionDBInit -v`
Expected: PASS

- [ ] **Step 5: Write failing tests for session CRUD**

Append to `tests/test_session_db.py`:

```python
class TestSessionCRUD:
    def test_create_session(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli", model="gpt-4")
        row = db.get_session("sess_001")
        assert row is not None
        assert row["id"] == "sess_001"
        assert row["session_key"] == "cli:direct"
        assert row["model"] == "gpt-4"
        assert row["ended_at"] is None

    def test_ensure_session_idempotent(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.ensure_session("sess_001", session_key="cli:direct", source="cli", model="gpt-4")
        db.ensure_session("sess_001", session_key="cli:direct", source="cli", model="gpt-4")
        row = db.get_session("sess_001")
        assert row is not None
        assert row["id"] == "sess_001"

    def test_end_session(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli", model="gpt-4")
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
        db.create_session("sess_001", session_key="cli:direct", source="cli", model="gpt-4")
        active = db.get_active_session("cli:direct")
        assert active is not None
        assert active["id"] == "sess_001"

    def test_get_active_session_returns_none_when_ended(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli", model="gpt-4")
        db.end_session("sess_001", "manual")
        assert db.get_active_session("cli:direct") is None

    def test_update_session(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli", model="gpt-4")
        db.update_session("sess_001", title="Test Session", input_tokens=100, output_tokens=50)
        row = db.get_session("sess_001")
        assert row["title"] == "Test Session"
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_session_db.py::TestSessionCRUD -v`
Expected: FAIL — `AttributeError: 'SessionDB' object has no attribute 'create_session'`

- [ ] **Step 7: Implement session CRUD methods**

Append to `SessionDB` class in `nanobot/session/db.py`:

```python
    # ── Write helpers ──

    def _execute_write(self, sql: str, params: tuple = ()) -> None:
        import random
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(sql, params)
                self._conn.commit()
                return
            except sqlite3.OperationalError:
                self._conn.rollback()
                if attempt == self._WRITE_MAX_RETRIES - 1:
                    raise
                time.sleep(random.uniform(self._WRITE_RETRY_MIN_S, self._WRITE_RETRY_MAX_S))

    # ── Session CRUD ──

    def create_session(
        self,
        session_id: str,
        *,
        session_key: str,
        source: str = "cli",
        model: str | None = None,
        user_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        self._execute_write(
            "INSERT INTO sessions (id, session_key, source, user_id, model, parent_session_id, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, session_key, source, user_id, model, parent_session_id, time.time()),
        )

    def ensure_session(self, session_id: str, **kwargs: Any) -> None:
        if self.get_session(session_id) is not None:
            return
        self.create_session(session_id, **kwargs)

    def end_session(self, session_id: str, end_reason: str) -> None:
        try:
            self._execute_write(
                "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                (time.time(), end_reason, session_id),
            )
        except Exception:
            logger.debug("Failed to end session {}: ", session_id, exc_info=True)

    def update_session(self, session_id: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        self._execute_write(
            f"UPDATE sessions SET {sets} WHERE id = ?",
            (*kwargs.values(), session_id),
        )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._conn.execute("SELECT * FROM sessions LIMIT 0").description]
        return dict(zip(cols, row))

    def get_active_session(self, session_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_key = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (session_key,),
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._conn.execute("SELECT * FROM sessions LIMIT 0").description]
        return dict(zip(cols, row))
```

- [ ] **Step 8: Run all SessionDB tests**

Run: `uv run pytest tests/test_session_db.py -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add nanobot/session/db.py tests/test_session_db.py
git commit -m "feat(session): add SessionDB with SQLite schema and session CRUD"
```

### Task 2: SessionDB — Messages, FTS5 Search, and Title

**Files:**
- Modify: `nanobot/session/db.py`
- Modify: `tests/test_session_db.py`

- [ ] **Step 1: Write failing tests for message CRUD**

Append to `tests/test_session_db.py`:

```python
class TestMessageCRUD:
    def test_append_message(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli")
        db.append_message("sess_001", role="user", content="Hello")
        db.append_message("sess_001", role="assistant", content="Hi there!")
        msgs = db.get_messages("sess_001")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello"
        assert msgs[1]["role"] == "assistant"

    def test_append_message_with_tool_calls(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli")
        db.append_message(
            "sess_001", role="assistant", content="",
            tool_calls=[{"name": "bash", "arguments": '{"command": "ls"}'}],
        )
        msgs = db.get_messages("sess_001")
        assert msgs[0]["tool_calls"] == '[{"name": "bash", "arguments": "{\\"command\\": \\"ls\\"}"}]'

    def test_get_messages_empty(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli")
        assert db.get_messages("sess_001") == []

    def test_message_count_updated(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli")
        db.append_message("sess_001", role="user", content="Hello")
        db.append_message("sess_001", role="assistant", content="Hi")
        row = db.get_session("sess_001")
        assert row["message_count"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_db.py::TestMessageCRUD -v`
Expected: FAIL — `AttributeError: 'SessionDB' object has no attribute 'append_message'`

- [ ] **Step 3: Implement append_message and get_messages**

Append to `SessionDB` class in `nanobot/session/db.py`:

```python
    # ── Messages ──

    def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str | None = None,
        tool_calls: list[dict] | str | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        token_count: int | None = None,
        finish_reason: str | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        import json as _json
        tc_str = None
        if isinstance(tool_calls, list):
            tc_str = _json.dumps(tool_calls, ensure_ascii=False)
        elif isinstance(tool_calls, str):
            tc_str = tool_calls
        self._execute_write(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, "
            "tool_name, timestamp, token_count, finish_reason, reasoning_content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, tc_str, tool_call_id, tool_name,
             time.time(), token_count, finish_reason, reasoning_content),
        )
        self._conn.execute(
            "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
            (session_id,),
        )
        self._conn.commit()

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp, id",
            (session_id,),
        ).fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._conn.execute("SELECT * FROM messages LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_session_db.py::TestMessageCRUD -v`
Expected: PASS

- [ ] **Step 5: Write failing tests for FTS5 search**

Append to `tests/test_session_db.py`:

```python
class TestFTS5Search:
    def test_search_finds_matching_content(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli")
        db.append_message("sess_001", role="user", content="How to deploy docker containers?")
        db.append_message("sess_001", role="assistant", content="Use docker-compose up")
        results = db.search_messages("docker")
        assert len(results) > 0

    def test_search_returns_session_info(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli")
        db.append_message("sess_001", role="user", content="Python testing with pytest")
        results = db.search_messages("pytest")
        assert results[0]["session_id"] == "sess_001"

    def test_search_no_match_returns_empty(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli")
        db.append_message("sess_001", role="user", content="Hello world")
        results = db.search_messages("nonexistent_topic_xyz")
        assert results == []

    def test_search_or_syntax(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli")
        db.append_message("sess_001", role="user", content="Deploying with kubernetes")
        db.append_message("sess_001", role="assistant", content="Use kubectl apply")
        results = db.search_messages("docker OR kubernetes")
        assert len(results) > 0

    def test_list_recent_sessions(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli", model="gpt-4")
        db.append_message("sess_001", role="user", content="Hi")
        db.update_session("sess_001", title="Test Chat")
        sessions = db.list_recent_sessions(limit=5)
        assert len(sessions) >= 1
        assert sessions[0]["id"] == "sess_001"
        assert sessions[0]["title"] == "Test Chat"
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_session_db.py::TestFTS5Search -v`
Expected: FAIL — `AttributeError: 'SessionDB' object has no attribute 'search_messages'`

- [ ] **Step 7: Implement search and list_recent_sessions**

Append to `SessionDB` class in `nanobot/session/db.py`:

```python
    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Sanitize a user query for FTS5 MATCH."""
        import re
        if not query or not query.strip():
            return ""
        # Preserve quoted phrases
        quoted: list[str] = re.findall(r'"[^"]*"', query)
        remainder = re.sub(r'"[^"]*"', " ", query)
        # Strip FTS5 special chars from unquoted parts
        remainder = re.sub(r"[^a-zA-Z0-9\s]", " ", remainder)
        tokens = remainder.split()
        # Wrap hyphenated/dotted terms in quotes
        cleaned_tokens: list[str] = []
        for t in tokens:
            if "-" in t or "." in t:
                cleaned_tokens.append(f'"{t}"')
            else:
                cleaned_tokens.append(t)
        parts = quoted + cleaned_tokens
        result = " ".join(parts).strip()
        # Remove dangling boolean operators
        result = re.sub(r"^\s*(AND|OR|NOT)\s+", "", result)
        result = re.sub(r"\s+(AND|OR|NOT)\s*$", "", result)
        return result

    def search_messages(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        sanitized = self._sanitize_fts5_query(query)
        if not sanitized:
            return []
        try:
            rows = self._conn.execute(
                "SELECT m.session_id, m.role, m.content, m.timestamp "
                "FROM messages_fts fts "
                "JOIN messages m ON m.id = fts.rowid "
                "WHERE messages_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (sanitized, limit),
            ).fetchall()
        except Exception:
            logger.debug("FTS5 search failed for query: {}", query, exc_info=True)
            return []
        return [
            {"session_id": r[0], "role": r[1], "content": r[2], "timestamp": r[3]}
            for r in rows
        ]

    def list_recent_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, session_key, source, model, title, started_at, ended_at, "
            "message_count, input_tokens, output_tokens "
            "FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        cols = ["id", "session_key", "source", "model", "title", "started_at",
                "ended_at", "message_count", "input_tokens", "output_tokens"]
        return [dict(zip(cols, row)) for row in rows]
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_session_db.py::TestFTS5Search -v`
Expected: PASS

- [ ] **Step 9: Write failing tests for title management**

Append to `tests/test_session_db.py`:

```python
class TestTitleManagement:
    def test_set_and_get_title(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        db.create_session("sess_001", session_key="cli:direct", source="cli")
        db.set_session_title("sess_001", "Debug API")
        assert db.get_session_title("sess_001") == "Debug API"

    def test_get_title_returns_none_for_missing(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        assert db.get_session_title("nonexistent") is None

    def test_next_title_in_lineage_first(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        assert db.get_next_title_in_lineage("Debug API") == "Debug API #2"

    def test_next_title_in_lineage_increments(self, tmp_path: Path) -> None:
        db = SessionDB(tmp_path / "state.db")
        assert db.get_next_title_in_lineage("Debug API #2") == "Debug API #3"
```

- [ ] **Step 10: Run test to verify it fails**

Run: `uv run pytest tests/test_session_db.py::TestTitleManagement -v`
Expected: FAIL — `AttributeError`

- [ ] **Step 11: Implement title methods**

Append to `SessionDB` class in `nanobot/session/db.py`:

```python
    # ── Title ──

    def set_session_title(self, session_id: str, title: str) -> None:
        self.update_session(session_id, title=title)

    def get_session_title(self, session_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return row[0]

    def get_next_title_in_lineage(self, title: str) -> str:
        """Get next numbered title: 'Foo' -> 'Foo #2', 'Foo #2' -> 'Foo #3'."""
        import re
        match = re.match(r"^(.+?)\s*#\s*(\d+)$", title)
        if match:
            base, num = match.group(1), int(match.group(2))
            return f"{base} #{num + 1}"
        return f"{title} #2"

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        self._conn.execute(
            "UPDATE sessions SET "
            "input_tokens = input_tokens + ?, "
            "output_tokens = output_tokens + ?, "
            "cache_read_tokens = cache_read_tokens + ? "
            "WHERE id = ?",
            (input_tokens, output_tokens, cache_read_tokens, session_id),
        )
        self._conn.commit()
```

- [ ] **Step 12: Run all SessionDB tests**

Run: `uv run pytest tests/test_session_db.py -v`
Expected: All PASS

- [ ] **Step 13: Commit**

```bash
git add nanobot/session/db.py tests/test_session_db.py
git commit -m "feat(session): add message CRUD, FTS5 search, and title management to SessionDB"
```

### Task 3: Session Dataclass Extension and SessionManager Integration

**Files:**
- Modify: `nanobot/session/manager.py` (Session dataclass + save + load + get_or_create)

- [ ] **Step 1: Add db_id and last_db_flush_idx to Session dataclass**

In `nanobot/session/manager.py`, modify the `Session` dataclass (line ~17-25):

```python
from nanobot.session.db import SessionDB, generate_session_id

@dataclass
class Session:
    """A conversation session."""

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0
    db_id: str = ""
    last_db_flush_idx: int = 0
```

- [ ] **Step 2: Initialize SessionDB in SessionManager.__init__**

In `SessionManager.__init__` (line ~103-107), add SessionDB:

```python
class SessionManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = get_legacy_sessions_dir()
        self._cache: dict[str, Session] = {}
        self._db = SessionDB(Path.home() / ".nanobot" / "state.db")
```

- [ ] **Step 3: Generate db_id in get_or_create for new sessions**

In `SessionManager.get_or_create` (line ~119-137), pass `db_id` when creating new Session:

```python
    def get_or_create(self, key: str) -> Session:
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key, db_id=generate_session_id())

        self._cache[key] = session
        return session
```

- [ ] **Step 4: Add SQLite incremental write to save()**

In `SessionManager.save()` (line ~189-206), add SQLite write after JSONL and persist `db_id` in metadata:

```python
    def save(self, session: Session) -> None:
        """Save a session to disk and SQLite."""
        path = self._get_session_path(session.key)

        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated,
                "db_id": session.db_id,
                "last_db_flush_idx": session.last_db_flush_idx,
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

        self._cache[session.key] = session

        # SQLite incremental write
        if session.db_id:
            self._db.ensure_session(
                session.db_id,
                session_key=session.key,
                source="cli",
            )
            for msg in session.messages[session.last_db_flush_idx:]:
                role = msg.get("role", "unknown")
                content = msg.get("content")
                if isinstance(content, list):
                    import json as _json
                    content = _json.dumps(content, ensure_ascii=False)
                self._db.append_message(
                    session.db_id,
                    role=role,
                    content=content,
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id"),
                    tool_name=msg.get("name"),
                    reasoning_content=msg.get("reasoning_content"),
                )
            session.last_db_flush_idx = len(session.messages)
```

- [ ] **Step 5: Recover db_id from JSONL in _load()**

In `SessionManager._load()` (line ~139-187), recover `db_id` and `last_db_flush_idx` from metadata:

```python
    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            updated_at = None
            last_consolidated = 0
            db_id = ""
            last_db_flush_idx = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        updated_at = datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                        db_id = data.get("db_id", "")
                        last_db_flush_idx = data.get("last_db_flush_idx", 0)
                    else:
                        messages.append(data)

            # Legacy JSONL migration: generate db_id if missing
            if not db_id:
                db_id = generate_session_id()
                last_db_flush_idx = 0

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated,
                db_id=db_id,
                last_db_flush_idx=last_db_flush_idx,
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            return None
```

- [ ] **Step 6: Run existing tests to verify no regressions**

Run: `uv run pytest tests/ -x --timeout=30`
Expected: All existing tests PASS. SessionManager changes are backward-compatible.

- [ ] **Step 7: Commit**

```bash
git add nanobot/session/manager.py
git commit -m "feat(session): integrate SessionDB into SessionManager save/load"
```

### Task 4: Autocompact Session Split

**Files:**
- Modify: `nanobot/agent/autocompact.py` (add session split in `_archive`)

- [ ] **Step 1: Add session split logic to `_archive()`**

In `nanobot/agent/autocompact.py`, modify `_archive()` method. After the existing archiving logic (around line 92-95 where `session.messages = kept_msgs`), add session split:

```python
from nanobot.session.db import generate_session_id
```

Then in `_archive()`, after `session.messages = kept_msgs` and before `session.updated_at = datetime.now()`:

```python
            # Session split: end old SQLite session, create new one
            if session.db_id:
                db = self.sessions._db
                db.end_session(session.db_id, "compression")
                old_db_id = session.db_id
                session.db_id = generate_session_id()
                session.last_db_flush_idx = 0
                db.create_session(
                    session.db_id,
                    session_key=key,
                    source="cli",
                    parent_session_id=old_db_id,
                )
                # Inherit title with auto-numbering
                old_title = db.get_session_title(old_db_id)
                if old_title:
                    new_title = db.get_next_title_in_lineage(old_title)
                    db.set_session_title(session.db_id, new_title)
```

- [ ] **Step 2: Run existing tests to verify no regressions**

Run: `uv run pytest tests/ -x --timeout=30`
Expected: All PASS. Autocompact changes only trigger when `session.db_id` is set.

- [ ] **Step 3: Commit**

```bash
git add nanobot/agent/autocompact.py
git commit -m "feat(session): add session split on autocompact compression"
```

### Task 5: Subagent Session Recording

**Files:**
- Modify: `nanobot/agent/subagent.py` (add SQLite persist in `_run_subagent`)
- Modify: `nanobot/agent/loop.py` (pass sessions to SubagentManager)

- [ ] **Step 1: Pass SessionManager to SubagentManager**

In `nanobot/agent/loop.py` (line ~202-212), add `session_manager` parameter to `SubagentManager`:

```python
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            web_config=self.web_config,
            max_tool_result_chars=self.max_tool_result_chars,
            bash_config=self.bash_config,
            restrict_to_workspace=restrict_to_workspace,
            disabled_skills=disabled_skills,
            session_manager=self.sessions,
        )
```

In `nanobot/agent/subagent.py`, update `SubagentManager.__init__` to accept and store `session_manager`:

```python
    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        web_config: "WebToolsConfig | None" = None,
        bash_config: "BashToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        session_manager: "SessionManager | None" = None,
    ):
        ...
        self.sessions = session_manager
```

- [ ] **Step 2: Add subagent SQLite recording in `_run_subagent()`**

In `nanobot/agent/subagent.py`, modify `_run_subagent()` to record the full conversation to SQLite. Add after the `result = await self.runner.run(...)` block (around line 233) and before the result handling:

```python
from nanobot.session.db import generate_session_id
import json as _json
```

Then add the recording logic inside `_run_subagent()`, right after `result = await self.runner.run(...)`:

```python
            # Persist full subagent conversation to SQLite
            if self.sessions and result.messages:
                db = self.sessions._db
                parent_db_id = None
                # Try to find parent session db_id from session_key mapping
                session_key = session_key  # from closure or outer scope
                if session_key:
                    parent_session = self.sessions.get_or_create(session_key)
                    parent_db_id = parent_session.db_id

                subagent_db_id = generate_session_id()
                db.create_session(
                    subagent_db_id,
                    session_key=f"subagent:{task_id}",
                    source="subagent",
                    model=self.model,
                    parent_session_id=parent_db_id,
                )
                for msg in result.messages:
                    role = msg.get("role", "unknown")
                    content = msg.get("content")
                    if isinstance(content, list):
                        content = _json.dumps(content, ensure_ascii=False)
                    db.append_message(
                        subagent_db_id,
                        role=role,
                        content=content,
                        tool_calls=msg.get("tool_calls"),
                        tool_call_id=msg.get("tool_call_id"),
                        tool_name=msg.get("name"),
                        reasoning_content=msg.get("reasoning_content"),
                    )
                usage = result.usage or {}
                db.update_token_counts(
                    subagent_db_id,
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                    cache_read_tokens=usage.get("cached_tokens", 0),
                )
                db.end_session(subagent_db_id, result.stop_reason or "completed")
```

Note: `session_key` is passed into `spawn()` and accessible via the `_run_subagent()` method. Update `spawn()` to pass `session_key` through to `_run_subagent()`. Currently `_run_subagent()` receives `origin` but not `session_key`. Add it as a parameter.

Update `_run_subagent` signature:

```python
    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        session_key: str | None = None,
    ) -> None:
```

Update `spawn()` to pass `session_key`:

```python
        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_label, origin, status, session_key=session_key)
        )
```

- [ ] **Step 3: Run existing tests**

Run: `uv run pytest tests/ -x --timeout=30`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add nanobot/agent/subagent.py nanobot/agent/loop.py
git commit -m "feat(session): record full subagent conversations to SQLite"
```

### Task 6: SessionSearchTool

**Files:**
- Create: `nanobot/agent/tools/session_search.py`
- Create: `tests/test_session_search.py`
- Modify: `nanobot/agent/loop.py` (register tool)

- [ ] **Step 1: Write failing tests for SessionSearchTool**

Create `tests/test_session_search.py`:

```python
from __future__ import annotations

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
    return SessionSearchTool(db=db, provider=MagicMock(), model="test-model")


class TestSessionSearchToolSchema:
    def test_name(self, tool: SessionSearchTool) -> None:
        assert tool.name == "session_search"

    def test_schema_has_query_param(self, tool: SessionSearchTool) -> None:
        props = tool.schema["parameters"]["properties"]
        assert "query" in props

    def test_schema_has_limit_param(self, tool: SessionSearchTool) -> None:
        props = tool.schema["parameters"]["properties"]
        assert "limit" in props


class TestSessionSearchRecent:
    @pytest.mark.asyncio
    async def test_recent_sessions_no_query(self, tool: SessionSearchTool, db: SessionDB) -> None:
        db.create_session("s1", session_key="cli:direct", source="cli", model="gpt-4")
        db.append_message("s1", role="user", content="Hello")
        db.update_session("s1", title="Test Chat")
        result = await tool.execute()
        assert "s1" in result
        assert "Test Chat" in result

    @pytest.mark.asyncio
    async def test_recent_sessions_empty_db(self, tool: SessionSearchTool) -> None:
        result = await tool.execute()
        assert "No sessions" in result or "no sessions" in result.lower()


class TestSessionSearchKeyword:
    @pytest.mark.asyncio
    async def test_keyword_search_finds_match(self, tool: SessionSearchTool, db: SessionDB) -> None:
        db.create_session("s1", session_key="cli:direct", source="cli", model="gpt-4")
        db.append_message("s1", role="user", content="How to deploy docker containers?")
        db.update_session("s1", title="Docker Deploy")
        # Mock LLM to return a summary
        tool._provider.chat_with_retry = AsyncMock(return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content="Discussed docker deployment"))]
        ))
        result = await tool.execute(query="docker")
        assert "s1" in result

    @pytest.mark.asyncio
    async def test_keyword_search_no_match(self, tool: SessionSearchTool, db: SessionDB) -> None:
        db.create_session("s1", session_key="cli:direct", source="cli", model="gpt-4")
        db.append_message("s1", role="user", content="Hello world")
        result = await tool.execute(query="nonexistent_topic_xyz")
        assert "No matching" in result or "no matching" in result.lower() or "not found" in result.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_search.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement SessionSearchTool**

Create `nanobot/agent/tools/session_search.py`:

```python
"""Built-in tool for searching past conversation sessions."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider
from nanobot.session.db import SessionDB

_SCHEMA = {
    "name": "session_search",
    "description": (
        "Search your long-term memory of past conversations, or browse recent sessions. "
        "TWO MODES:\n"
        "1. Recent sessions (no query): browse what was worked on recently. Zero LLM cost.\n"
        "2. Keyword search (with query): search for specific topics across all past sessions. "
        "Returns LLM-generated summaries of matching sessions.\n\n"
        "USE PROACTIVELY when:\n"
        "- The user says 'we did this before', 'remember when', 'last time'\n"
        "- The user asks about a topic you worked on before\n"
        "- You want to check if you've solved a similar problem before\n\n"
        "Search syntax: keywords with OR for broad recall (docker OR kubernetes), "
        'phrases for exact match ("docker networking"), boolean (python NOT java), '
        "prefix (deploy*). Use OR between keywords for best results."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search keywords. Supports OR/NOT/phrase/prefix. "
                    "Omit to browse recent sessions instead."
                ),
            },
            "limit": {
                "type": "integer",
                "default": 3,
                "description": "Max sessions to return (1-5).",
            },
        },
    },
}

_SUMMARY_SYSTEM_PROMPT = (
    "You are reviewing a past conversation transcript. Summarize with focus on the search topic. "
    "Include: what was asked, what actions were taken, key decisions/solutions, specific technical details. "
    "Be concise. Write in past tense."
)


class SessionSearchTool:
    """Search past sessions using FTS5."""

    def __init__(self, db: SessionDB, provider: LLMProvider, model: str) -> None:
        self._db = db
        self._provider = provider
        self._model = model

    @property
    def name(self) -> str:
        return "session_search"

    @property
    def schema(self) -> dict[str, Any]:
        return _SCHEMA

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, *, query: str | None = None, limit: int = 3) -> str:
        limit = max(1, min(5, limit))
        if not query or not query.strip():
            return self._format_recent(limit)
        return await self._search(query.strip(), limit)

    def _format_recent(self, limit: int) -> str:
        sessions = self._db.list_recent_sessions(limit=limit)
        if not sessions:
            return "No sessions found."
        lines = ["Recent sessions:\n"]
        for s in sessions:
            title = s.get("title") or s.get("session_key", "Untitled")
            from datetime import datetime
            started = datetime.fromtimestamp(s["started_at"]).strftime("%Y-%m-%d %H:%M")
            msg_count = s.get("message_count", 0)
            lines.append(f"- [{s['id']}] {title} ({started}, {msg_count} msgs, {s.get('source', 'cli')})")
        return "\n".join(lines)

    async def _search(self, query: str, limit: int) -> str:
        results = self._db.search_messages(query, limit=limit * 10)
        if not results:
            return f"No matching sessions found for: {query}"

        # Group by session_id
        session_ids: dict[str, list[dict]] = {}
        for r in results:
            session_ids.setdefault(r["session_id"], []).append(r)

        # Take top N sessions
        top_sessions = list(session_ids.keys())[:limit]

        summaries = await asyncio.gather(
            *[self._summarize_session(sid, query) for sid in top_sessions],
            return_exceptions=True,
        )

        lines = [f"Search results for '{query}':\n"]
        for sid, summary in zip(top_sessions, summaries):
            if isinstance(summary, Exception):
                meta = self._db.get_session(sid) or {}
                lines.append(f"- [{sid}] {meta.get('title', 'Untitled')} (summary failed)")
            else:
                lines.append(f"- [{sid}] {summary}")
        return "\n".join(lines)

    async def _summarize_session(self, session_id: str, query: str) -> str:
        messages = self._db.get_messages(session_id)
        meta = self._db.get_session(session_id) or {}
        title = meta.get("title", "Untitled")

        # Truncate conversation for summarization
        conv_parts: list[str] = []
        total_chars = 0
        for msg in messages[-20:]:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                conv_parts.append(f"{role.upper()}: {content[:500]}")
                total_chars += len(conv_parts[-1])
                if total_chars > 3000:
                    break
        conv_text = "\n".join(conv_parts)
        if not conv_text:
            return title

        try:
            response = await self._provider.chat_with_retry(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Search query: {query}\n\nConversation:\n{conv_text}"},
                ],
            )
            content = response.choices[0].message.content
            return f"{title}: {content}" if content else title
        except Exception:
            logger.debug("Session summary failed for {}", session_id, exc_info=True)
            return title
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_session_search.py -v`
Expected: PASS

- [ ] **Step 5: Register SessionSearchTool in AgentLoop**

In `nanobot/agent/loop.py`, add import and registration in `_register_default_tools()` (after the existing tool registrations, around line 298):

```python
from nanobot.agent.tools.session_search import SessionSearchTool
```

In `_register_default_tools()`, after `self.tools.register(LoadSkillTool(...))`:

```python
        self.tools.register(
            SessionSearchTool(db=self.sessions._db, provider=self.provider, model=self.model)
        )
```

- [ ] **Step 6: Run all tests**

Run: `uv run pytest tests/ -x --timeout=30`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add nanobot/agent/tools/session_search.py tests/test_session_search.py nanobot/agent/loop.py
git commit -m "feat(session): add session_search built-in tool with FTS5 and LLM summarization"
```

### Task 7: Session Title Auto-Generation

**Files:**
- Modify: `nanobot/agent/loop.py` (add auto-title trigger after turn)

- [ ] **Step 1: Add auto-title trigger in `_process_message()`**

In `nanobot/agent/loop.py`, after the `_save_turn()` + `sessions.save()` calls in the main message processing flow (around line 818-821 for non-system messages), add auto-title generation:

```python
        # Auto-title generation for new sessions
        if session.db_id and not self.sessions._db.get_session_title(session.db_id):
            user_msg_count = sum(
                1 for m in session.messages[:session.last_db_flush_idx]
                if m.get("role") == "user"
            )
            if 1 <= user_msg_count <= 2:
                self._schedule_background(
                    self._auto_title(session)
                )
```

Add the `_auto_title` method to `AgentLoop`:

```python
    async def _auto_title(self, session: Session) -> None:
        """Auto-generate a session title after first exchanges."""
        if not session.db_id:
            return
        db = self.sessions._db
        if db.get_session_title(session.db_id):
            return
        user_msgs = [m for m in session.messages if m.get("role") == "user"]
        assistant_msgs = [m for m in session.messages if m.get("role") == "assistant"]
        if not user_msgs or not assistant_msgs:
            return
        user_content = user_msgs[0].get("content", "")
        if isinstance(user_content, list):
            user_content = " ".join(b.get("text", "") for b in user_content if isinstance(b, dict))
        assistant_content = assistant_msgs[0].get("content", "")
        if isinstance(assistant_content, list):
            assistant_content = " ".join(
                b.get("text", "") for b in assistant_content if isinstance(b, dict)
            )
        user_content = str(user_content)[:500]
        assistant_content = str(assistant_content)[:500]
        try:
            response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {"role": "system", "content": (
                        "Generate a short descriptive title (3-7 words) for a conversation "
                        "starting with the following exchange. Return ONLY the title, "
                        "no quotes, no punctuation at the end."
                    )},
                    {"role": "user", "content": f"User: {user_content}\nAssistant: {assistant_content}"},
                ],
            )
            title = response.choices[0].message.content or ""
            title = title.strip().strip('"').strip("'")
            if title.endswith((".", "!", "?")):
                title = title[:-1].strip()
            if len(title) > 80:
                title = title[:77] + "..."
            if title and session.db_id:
                db.set_session_title(session.db_id, title)
        except Exception:
            logger.debug("Auto-title generation failed for {}", session.db_id, exc_info=True)
```

- [ ] **Step 2: Run all tests**

Run: `uv run pytest tests/ -x --timeout=30`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add nanobot/agent/loop.py
git commit -m "feat(session): add auto-title generation for new sessions"
```




