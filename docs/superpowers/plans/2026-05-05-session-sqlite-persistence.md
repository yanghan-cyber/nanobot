# Session SQLite Persistence — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a SQLite sidecar database that shadows all session activity for permanent archival, full-text search, auto-title generation, and subagent conversation tracking — without changing any existing JSONL behavior.

**Architecture:** SQLite is a write-only shadow of JSONL. `SessionManager.save()` flushes new messages to SQLite after the JSONL write succeeds. A new `SessionSearchTool` enables the agent to search past conversations via FTS5 (with trigram CJK support). Auto-title runs as a background LLM call. Subagent conversations are persisted with parent-session linkage.

**Tech Stack:** SQLite3 (stdlib), FTS5 + trigram tokenizer, `threading.Lock` for write safety, WAL mode.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `nanobot/session/db.py` | **New** | SessionDB: schema, CRUD, FTS5 search, thread safety |
| `nanobot/session/title.py` | **New** | `auto_title()` LLM call, `clean_title()` sanitization |
| `nanobot/session/manager.py` | Modify | 2 new Session fields, SQLite flush in save/load |
| `nanobot/session/__init__.py` | Modify | Export SessionDB |
| `nanobot/agent/tools/session_search.py` | **New** | SessionSearchTool |
| `nanobot/agent/loop.py` | Modify | Register tool, trigger auto-title, pass session_manager to SubagentManager |
| `nanobot/agent/subagent.py` | Modify | Accept session_manager, persist subagent conversations |
| `nanobot/agent/autocompact.py` | Modify | Session split on compression |
| `tests/test_session_db.py` | **New** | SessionDB unit tests |
| `tests/test_session_manager_db.py` | **New** | SessionManager SQLite integration tests |
| `tests/test_session_search.py` | **New** | SessionSearchTool tests |
| `tests/test_session_title.py` | **New** | Auto-title tests |

---

### Task 1: SessionDB — Schema Bootstrap + Session CRUD

**Files:**
- Create: `nanobot/session/db.py`
- Test: `tests/test_session_db.py`

- [ ] **Step 1: Write failing tests for SessionDB init + session CRUD**

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_db.py -x -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nanobot.session.db'`

- [ ] **Step 3: Implement SessionDB schema + session CRUD**

Create `nanobot/session/db.py` with:

- Module docstring: `"SQLite-backed session storage with FTS5 full-text search."`
- Imports: `__future__`, `json`, `random`, `re`, `sqlite3`, `threading`, `time`, `datetime`/`timezone`, `Path`, `token_hex`, `Any` from typing, `logger` from loguru
- `_SCHEMA_VERSION = 1`
- `_SCHEMA_SQL` string with `schema_version`, `sessions`, `messages` tables per spec
- `_FTS_SQL` string with `messages_fts` (default tokenizer), `messages_fts_trigram` (trigram tokenizer), and INSERT/DELETE/UPDATE triggers for each. FTS content = `COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '')`
- `_INDEX_SQL` string with 4 indexes
- `generate_session_id() -> str`: format `YYYYMMDD_HHMMSS_<6hex>`
- `class SessionDB`:
  - `__init__(self, path: Path)`: create parent dir, connect with `check_same_thread=False`, WAL mode, foreign keys ON, `row_factory = sqlite3.Row`, `self._lock = threading.Lock()`, call `_init_schema()`
  - `_init_schema()`: execute the three SQL strings, record schema version if not yet recorded
  - `_execute_write(self, sql, params=())`: acquire `self._lock`, `BEGIN IMMEDIATE`, execute, commit; on locked error retry 15x with random 20-150ms jitter; log warnings
  - `create_session(session_id, *, session_key, source='agent', model=None, user_id=None, parent_session_id=None)`: insert via `_execute_write`
  - `ensure_session(...)`: check `get_session`, create if None
  - `end_session(session_id, end_reason)`: update ended_at + end_reason
  - `_SESSION_UPDATABLE_COLS` frozenset of allowed update columns
  - `update_session(session_id, **kwargs)`: filter to updatable cols, dynamic SET
  - `get_session(session_id) -> dict | None`: select * from sessions
  - `get_active_session(session_key) -> dict | None`: where ended_at IS NULL, order by started_at DESC limit 1

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_db.py::TestSessionDBInit tests/test_session_db.py::TestSessionCRUD -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/session/db.py tests/test_session_db.py
git commit -m "feat(session): add SessionDB with SQLite schema and session CRUD"
```

---

### Task 2: SessionDB — Message CRUD + Title + Token Counts + Listing

**Files:**
- Modify: `nanobot/session/db.py`
- Modify: `tests/test_session_db.py`

- [ ] **Step 1: Write failing tests for message CRUD, title, tokens, listing**

Append to `tests/test_session_db.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_db.py::TestMessageCRUD -v`
Expected: FAIL — `AttributeError: 'SessionDB' object has no attribute 'append_message'`

- [ ] **Step 3: Implement message CRUD, title, token counts, listing**

Add to `SessionDB` in `nanobot/session/db.py`:

- `append_message(session_id, *, role, content=None, tool_calls=None, tool_call_id=None, tool_name=None, token_count=None, finish_reason=None, reasoning_content=None)`: if `tool_calls` is list, json.dumps it. Use `_execute_write` inside a combined transaction (insert message + increment message_count). Wrap both in single `self._lock` acquisition by using `_execute_write` for the message insert and a separate call for the count update (or inline the full transaction).
- `get_messages(session_id) -> list[dict]`: select * from messages order by created_at
- `set_session_title(session_id, title)`: delegate to `update_session`
- `get_session_title(session_id) -> str | None`: select title
- `get_next_title_in_lineage(title) -> str`: `@staticmethod`, regex match `^(.*) #(\d+)$`
- `update_token_counts(session_id, *, input_tokens=0, output_tokens=0, cache_read_tokens=0)`: additive update
- `list_recent_sessions(limit=10) -> list[dict]`: select id, session_key, source, model, title, started_at, ended_at, message_count, input_tokens, output_tokens order by started_at DESC

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_db.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/session/db.py tests/test_session_db.py
git commit -m "feat(session): add message CRUD, title, tokens, listing to SessionDB"
```

---

### Task 3: SessionDB — FTS5 Search (default + trigram + CJK fallback)

**Files:**
- Modify: `nanobot/session/db.py`
- Modify: `tests/test_session_db.py`

- [ ] **Step 1: Write failing tests for FTS5 search**

Append to `tests/test_session_db.py`:

```python
import re


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_db.py::TestFTS5Search -v`
Expected: FAIL — `AttributeError` on search methods

- [ ] **Step 3: Implement FTS5 search with CJK support**

Add to `nanobot/session/db.py`:

- Module-level helper `_contains_cjk(text: str) -> bool`: check for CJK Unicode ranges (`一-鿿`, `㐀-䶿`, `豈-﫿`, etc.) using regex
- `_count_cjk(text: str) -> int`: count CJK characters
- `SessionDB._sanitize_fts5_query(query: str) -> str` (static): preserve quoted phrases (`"..."'`), strip special FTS5 chars from non-quoted parts
- `SessionDB.search_messages(self, query, *, role_filter=None, exclude_sources=None, limit=20) -> list[dict]`:
  - If `_contains_cjk(query)`:
    - If `_count_cjk(query) >= 3`: use `messages_fts_trigram MATCH` with quoted non-operator tokens
    - Else: use `LIKE '%query%'` on `m.content OR m.tool_name OR m.tool_calls`
  - Else: use `messages_fts MATCH` with `_sanitize_fts5_query(query)`
  - Build WHERE clause: join with sessions table for source filtering, apply role_filter
  - Return `session_id, role, content, created_at` for each match

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_db.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/session/db.py tests/test_session_db.py
git commit -m "feat(session): add FTS5 search with CJK trigram and LIKE fallback"
```

---

### Task 4: SessionManager — Add SQLite flush to save/load

**Files:**
- Modify: `nanobot/session/manager.py` (lines 27-36 Session fields, lines 246-251 __init__, lines 265-283 get_or_create, lines 285-330 _load, lines 338-388 _repair, lines 418-428 save metadata_line, lines 393-401 _session_payload)
- Modify: `nanobot/session/__init__.py`
- Test: `tests/test_session_manager_db.py`

- [ ] **Step 1: Write failing tests for SessionManager SQLite integration**

Create `tests/test_session_manager_db.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.session.manager import Session, SessionManager


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_manager_db.py -v`
Expected: FAIL — missing `db_id` attribute on Session

- [ ] **Step 3: Add `db_id` and `last_db_flush_idx` to Session dataclass**

In `nanobot/session/manager.py`, add 2 fields to the Session dataclass after `last_consolidated` (line 35):

```python
    db_id: str = ""                    # SQLite session ID
    last_db_flush_idx: int = 0         # messages already flushed to SQLite
```

Add import at top: `from nanobot.session.db import SessionDB, generate_session_id`

- [ ] **Step 4: Update SessionManager.__init__**

Change `nanobot/session/manager.py` line 246-251:

```python
def __init__(self, workspace: Path, db_path: Path | None = None):
    self.workspace = workspace
    self.sessions_dir = ensure_dir(self.workspace / "sessions")
    self.legacy_sessions_dir = get_legacy_sessions_dir()
    self._cache: dict[str, Session] = {}
    self._db = SessionDB(db_path or Path.home() / ".nanobot" / "state.db")
```

- [ ] **Step 5: Update get_or_create for new sessions**

In `get_or_create()`, after `session = Session(key=key)` (line 279), add:

```python
session.db_id = generate_session_id()
```

- [ ] **Step 6: Update _load() to read db_id and last_db_flush_idx**

In `_load()` (around line 300-330), add to the local variable initialization block:

```python
db_id = ""
last_db_flush_idx = 0
```

In the metadata parsing block (after `last_consolidated = ...`), add:

```python
db_id = data.get("db_id", "")
last_db_flush_idx = data.get("last_db_flush_idx", 0)
```

In the `Session(...)` constructor call, add:

```python
db_id=db_id or generate_session_id(),
last_db_flush_idx=last_db_flush_idx
```

- [ ] **Step 7: Update _repair() similarly**

In `_repair()` (around line 338-388), add the same `db_id`/`last_db_flush_idx` parsing from metadata and pass to `Session()` constructor.

- [ ] **Step 8: Add SQLite flush BEFORE JSONL write, update metadata_line**

Flush must happen **before** building metadata_line so that `last_db_flush_idx` in JSONL matches the actual SQLite state.

In `save()`, before `metadata_line = {` (line 418), add the SQLite flush block:

```python
# SQLite shadow flush — failure is non-fatal; do it BEFORE JSONL write
# so last_db_flush_idx in metadata is consistent with SQLite state.
try:
    self._db.ensure_session(
        session.db_id, session_key=session.key, source="agent"
    )
    for msg in session.messages[session.last_db_flush_idx:]:
        role = msg.get("role", "")
        content = msg.get("content")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
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
except Exception:
    logger.warning("SQLite flush failed for session {}", session.key, exc_info=True)
```

Then in the `metadata_line` dict (line 418-425), add the two new fields:

```python
"db_id": session.db_id,
"last_db_flush_idx": session.last_db_flush_idx,
```

- [ ] **Step 9: Update _session_payload()**

No changes needed — `db_id` and `last_db_flush_idx` are internal fields not part of the payload.

- [ ] **Step 10: Update __init__.py**

In `nanobot/session/__init__.py`, add:

```python
from nanobot.session.db import SessionDB
```

And add `"SessionDB"` to `__all__`.

- [ ] **Step 11: Run tests to verify they pass**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_manager_db.py -v`
Expected: All PASS

Also run existing tests to ensure nothing broke:
Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_db.py tests/test_session_manager_db.py -v`

- [ ] **Step 12: Commit**

```bash
git add nanobot/session/manager.py nanobot/session/__init__.py tests/test_session_manager_db.py
git commit -m "feat(session): integrate SQLite flush into SessionManager save/load"
```

---

### Task 5: Auto-Title (`nanobot/session/title.py`)

**Files:**
- Create: `nanobot/session/title.py`
- Test: `tests/test_session_title.py`

- [ ] **Step 1: Write failing tests for auto-title**

Create `tests/test_session_title.py`:

```python
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.session.title import auto_title, clean_title


class TestCleanTitle:
    def test_strips_quotes(self) -> None:
        assert clean_title('"Debug API"') == "Debug API"

    def test_strips_single_quotes(self) -> None:
        assert clean_title("'Debug API'") == "Debug API"

    def test_removes_trailing_punctuation(self) -> None:
        assert clean_title("Debug API.") == "Debug API"
        assert clean_title("Debug API!") == "Debug API"
        assert clean_title("Debug API?") == "Debug API"

    def test_truncates_long_title(self) -> None:
        long_title = "A" * 100
        result = clean_title(long_title)
        assert len(result) == 80
        assert result.endswith("...")

    def test_returns_empty_for_empty(self) -> None:
        assert clean_title("") == ""
        assert clean_title("   ") == ""

    def test_preserves_normal_title(self) -> None:
        assert clean_title("Deploy Docker Containers") == "Deploy Docker Containers"


class TestAutoTitle:
    @pytest.mark.asyncio
    async def test_auto_title_returns_cleaned(self) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(
            return_value=MagicMock(content='"Fix authentication bug"')
        )
        result = await auto_title(provider, "gpt-4", "Fix the login bug", "I found the issue")
        assert result == "Fix authentication bug"

    @pytest.mark.asyncio
    async def test_auto_title_truncates_long_input(self) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(
            return_value=MagicMock(content="Some title")
        )
        long_content = "x" * 1000
        await auto_title(provider, "gpt-4", long_content, long_content)
        # Verify the provider was called (inputs were truncated)
        assert provider.chat_with_retry.called

    @pytest.mark.asyncio
    async def test_auto_title_handles_list_content(self) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(
            return_value=MagicMock(content="Test Title")
        )
        user_content = [{"type": "text", "text": "Hello world"}]
        result = await auto_title(provider, "gpt-4", user_content, "Hi")
        assert result == "Test Title"

    @pytest.mark.asyncio
    async def test_auto_title_returns_none_on_failure(self) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(side_effect=Exception("API error"))
        result = await auto_title(provider, "gpt-4", "Hello", "Hi")
        assert result is None

    @pytest.mark.asyncio
    async def test_auto_title_returns_none_on_empty_response(self) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(
            return_value=MagicMock(content="")
        )
        result = await auto_title(provider, "gpt-4", "Hello", "Hi")
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_title.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nanobot.session.title'`

- [ ] **Step 3: Implement clean_title and auto_title**

Create `nanobot/session/title.py`:

```python
"""Auto-title generation for conversation sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

from loguru import logger

_SYSTEM_PROMPT = (
    "Generate a short descriptive title (3-7 words) for a conversation "
    "starting with the following exchange. Return ONLY the title, "
    "no quotes, no punctuation at the end."
)

_MAX_TITLE_LEN = 80


def clean_title(raw: str) -> str:
    """Sanitize a raw LLM title output."""
    title = raw.strip().strip('"').strip("'").strip()
    if not title:
        return ""
    while title.endswith((".", "!", "?")):
        title = title[:-1].strip()
    if len(title) > _MAX_TITLE_LEN:
        title = title[: _MAX_TITLE_LEN - 3] + "..."
    return title


def _extract_text(content: object, max_len: int = 500) -> str:
    """Extract a text string from various content formats."""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        text = " ".join(parts)
    else:
        text = str(content)
    return text[:max_len]


async def auto_title(
    provider: LLMProvider,
    model: str,
    user_content: object,
    assistant_content: object,
) -> str | None:
    """Generate a session title from the first user-assistant exchange."""
    user_text = _extract_text(user_content)
    assistant_text = _extract_text(assistant_content)
    if not user_text:
        return None

    try:
        response = await provider.chat_with_retry(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"User: {user_text}\nAssistant: {assistant_text}"},
            ],
        )
        content = response.content if response else None
        if not content:
            return None
        return clean_title(content) or None
    except Exception:
        logger.debug("Auto-title generation failed", exc_info=True)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_title.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/session/title.py tests/test_session_title.py
git commit -m "feat(session): add auto-title generation module"
```

---

### Task 6: SessionSearchTool (`nanobot/agent/tools/session_search.py`)

**Files:**
- Create: `nanobot/agent/tools/session_search.py`
- Test: `tests/test_session_search.py`

- [ ] **Step 1: Write failing tests for SessionSearchTool**

Create `tests/test_session_search.py`:

```python
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
    async def test_recent_sessions_no_query(self, tool: SessionSearchTool, db: SessionDB) -> None:
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
        db.create_session("s1", session_key="cli:direct", source="agent")
        db.append_message("s1", role="user", content="docker deployment")
        db.create_session("s2", session_key="cli:direct", source="agent", parent_session_id="s1")
        db.append_message("s2", role="user", content="kubernetes deployment")
        # Searching from s2 should exclude both s2 and s1 (its parent)
        result = await tool.execute(query="docker", current_session_id="s2")
        parsed = json.loads(result)
        assert all(r["session_id"] not in ("s1", "s2") for r in parsed["results"])

    @pytest.mark.asyncio
    async def test_limit_clamped_to_range(self, tool: SessionSearchTool) -> None:
        result = await tool.execute(limit=10)
        # Should not error even with limit > 5
        assert isinstance(result, str)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_search.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement SessionSearchTool**

Create `nanobot/agent/tools/session_search.py`:

```python
"""SessionSearchTool: search and browse past conversation sessions."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.session.db import SessionDB
from nanobot.providers.base import LLMProvider

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Search keyword(s). Supports FTS5 syntax. "
                "Omit to list recent sessions."
            ),
        },
        "role_filter": {
            "type": "string",
            "description": (
                "Comma-separated roles to include (e.g. 'user,assistant')"
            ),
        },
        "limit": {
            "type": "integer",
            "description": "Max sessions to return (1-5, default 3).",
            "default": 3,
        },
    },
}

_SUMMARY_SYSTEM_PROMPT = (
    "You are a helpful assistant that summarizes conversations. "
    "Given messages from a past conversation and a search query, "
    "produce a concise 1-3 sentence summary of what was discussed "
    "relevant to the query. Focus on key topics, decisions, and outcomes."
)

_MAX_TRANSCRIPT_CHARS = 3000


class SessionSearchTool(Tool):
    """Search past sessions by keyword (FTS5) or list recent ones."""

    def __init__(
        self, db: SessionDB, provider: LLMProvider, model: str
    ) -> None:
        self._db = db
        self._provider = provider
        self._model = model

    @property
    def name(self) -> str:
        return "session_search"

    @property
    def description(self) -> str:
        return (
            "Search and browse past conversation sessions.\n"
            "Two modes:\n"
            "1. No query: list the most recent sessions.\n"
            "2. With query: full-text search across all stored messages, "
            "then summarise each matching session.\n"
            "Use this to recall past conversations or find previously "
            "discussed topics."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _SCHEMA.copy()

    @property
    def schema(self) -> dict[str, Any]:
        return self.to_schema()["function"]

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        *,
        query: str | None = None,
        role_filter: str | None = None,
        limit: int = 3,
        current_session_id: str | None = None,
        **_kwargs: Any,
    ) -> str:
        limit = max(1, min(5, limit))

        if not query or not query.strip():
            return self._format_recent(limit)

        return await self._search(
            query.strip(), limit, role_filter, current_session_id
        )

    # ------------------------------------------------------------------
    # Recent sessions
    # ------------------------------------------------------------------

    def _format_recent(self, limit: int) -> str:
        sessions = self._db.list_recent_sessions(limit=limit)
        if not sessions:
            return "No sessions found."
        lines: list[str] = []
        for s in sessions:
            started = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(s.get("started_at", 0))
            )
            title = s.get("title") or "(untitled)"
            sid = s.get("id", "?")
            model = s.get("model", "?")
            msg_count = s.get("message_count", 0)
            lines.append(
                f"- [{sid}] {title}  "
                f"(model: {model}, messages: {msg_count}, started: {started})"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Keyword search + LLM summarisation
    # ------------------------------------------------------------------

    def _get_lineage_ids(self, session_id: str) -> set[str]:
        """Collect all session IDs in the lineage of session_id."""
        ids: set[str] = set()
        current = session_id
        for _ in range(20):  # safety limit
            ids.add(current)
            row = self._db.get_session(current)
            if not row or not row.get("parent_session_id"):
                break
            current = row["parent_session_id"]
        return ids

    async def _search(
        self,
        query: str,
        limit: int,
        role_filter: str | None,
        current_session_id: str | None,
    ) -> str:
        exclude_ids: set[str] = set()
        if current_session_id:
            exclude_ids = self._get_lineage_ids(current_session_id)

        parsed_roles = None
        if role_filter:
            parsed_roles = [r.strip() for r in role_filter.split(",") if r.strip()]

        rows = self._db.search_messages(
            query,
            role_filter=parsed_roles,
            exclude_sources=None,
        )

        # Group by session, exclude current lineage
        by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            sid = row["session_id"]
            if sid not in exclude_ids:
                by_session[sid].append(row)

        top_ids = list(by_session.keys())[:limit]

        results: list[dict[str, Any]] = []
        for sid in top_ids:
            session_info = self._db.get_session(sid)
            summary = await self._summarize_session(sid, query)
            started = time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime((session_info or {}).get("started_at", 0)),
            )
            results.append({
                "session_id": sid,
                "when": started,
                "source": (session_info or {}).get("source", "unknown"),
                "model": (session_info or {}).get("model", "unknown"),
                "title": (session_info or {}).get("title") or "(untitled)",
                "summary": summary,
            })

        return json.dumps({
            "success": True,
            "query": query,
            "count": len(results),
            "results": results,
        }, ensure_ascii=False)

    async def _summarize_session(
        self, session_id: str, query: str
    ) -> str:
        messages = self._db.get_messages(session_id)
        if not messages:
            return "(no messages)"

        parts: list[str] = []
        total_len = 0
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content") or ""
            line = f"{role}: {content}"
            if total_len + len(line) > _MAX_TRANSCRIPT_CHARS:
                remaining = _MAX_TRANSCRIPT_CHARS - total_len
                if remaining > 20:
                    parts.append(line[:remaining] + "...")
                break
            parts.append(line)
            total_len += len(line)

        transcript = "\n".join(parts)
        try:
            resp = await self._provider.chat_with_retry(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Search query: {query}\n\nTranscript:\n{transcript}"
                        ),
                    },
                ],
            )
            content = resp.content
            return content.strip() if content else "(no summary)"
        except Exception as exc:
            logger.warning("session_search summarisation failed: {}", exc)
            return f"(summary unavailable: {exc})"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_search.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/agent/tools/session_search.py tests/test_session_search.py
git commit -m "feat(session): add SessionSearchTool with FTS5 search and LLM summarization"
```

---

### Task 7: AgentLoop — Register SessionSearchTool + Auto-Title

**Files:**
- Modify: `nanobot/agent/loop.py`

- [ ] **Step 1: Add import for SessionSearchTool**

At `nanobot/agent/loop.py` around line 38 (after the `self import MyTool` import), add:

```python
from nanobot.agent.tools.session_search import SessionSearchTool
```

- [ ] **Step 2: Add import for auto_title**

Add:

```python
from nanobot.session.title import auto_title
```

- [ ] **Step 3: Register SessionSearchTool in _register_default_tools()**

In `_register_default_tools()` at `nanobot/agent/loop.py`, after the CronTool registration block (around line 413), add:

```python
self.tools.register(
    SessionSearchTool(
        db=self.sessions._db, provider=self.provider, model=self.model
    )
)
```

- [ ] **Step 4: Pass session_manager to SubagentManager**

In `AgentLoop.__init__()` at line 261-272 where SubagentManager is constructed, add `session_manager=self.sessions`:

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
    max_iterations=self.max_iterations,
    session_manager=self.sessions,
)
```

- [ ] **Step 5: Add _auto_title method and trigger in _save_turn area**

Add this method to AgentLoop (near `_schedule_background`):

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
    assistant_content = assistant_msgs[0].get("content", "")
    title = await auto_title(
        self.provider, self.model, user_content, assistant_content
    )
    if title and session.db_id:
        db.set_session_title(session.db_id, title)
```

After the main `self.sessions.save(session)` call at line 1184, add the auto-title trigger:

```python
# Auto-title generation for new sessions
if (
    session.db_id
    and not self.sessions._db.get_session_title(session.db_id)
):
    user_msg_count = sum(
        1
        for m in session.messages[: session.last_db_flush_idx]
        if m.get("role") == "user"
    )
    if 1 <= user_msg_count <= 2:
        self._schedule_background(self._auto_title(session))
```

- [ ] **Step 6: Run existing tests to verify nothing broke**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/ -x --timeout=30 -q`
Expected: No new failures (pre-existing failures are acceptable)

- [ ] **Step 7: Commit**

```bash
git add nanobot/agent/loop.py
git commit -m "feat(agent): register SessionSearchTool and add auto-title trigger in AgentLoop"
```

---

### Task 8: SubagentManager — Persist subagent conversations to SQLite

**Files:**
- Modify: `nanobot/agent/subagent.py`

- [ ] **Step 1: Add session_manager parameter to __init__**

In `nanobot/agent/subagent.py`, update `SubagentManager.__init__()` signature (line 84) to add:

```python
session_manager: SessionManager | None = None,
```

And store it:

```python
self.sessions = session_manager
```

Add the type import at top if needed:

```python
from __future__ import annotations  # already present
# TYPE_CHECKING block: add SessionManager
```

- [ ] **Step 2: Thread session_key through to _run_subagent**

In the `asyncio.create_task()` call (line 143), `session_key` is available in the caller (`start_task`). Pass it to `_run_subagent`:

```python
bg_task = asyncio.create_task(
    self._run_subagent(
        task_id, task, display_label, origin, status,
        origin_message_id, session_key=session_key,
    )
)
```

- [ ] **Step 3: Add session_key parameter to _run_subagent**

Update `_run_subagent()` signature (line 163) to add:

```python
session_key: str | None = None,
```

- [ ] **Step 4: Add SQLite persistence after status.phase = "done"**

After `status.phase = "done"` and `status.stop_reason = result.stop_reason` (line 249), and before the `if result.stop_reason == "tool_error":` block, add:

```python
# Persist full subagent conversation to SQLite
if self.sessions and result.messages:
    from nanobot.session.db import generate_session_id
    import json as _json
    db = self.sessions._db
    parent_db_id = None
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

- [ ] **Step 5: Run existing tests to verify nothing broke**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/ -x --timeout=30 -q`
Expected: No new failures

- [ ] **Step 6: Commit**

```bash
git add nanobot/agent/subagent.py
git commit -m "feat(subagent): persist full subagent conversations to SQLite with parent linkage"
```

---

### Task 9: AutoCompact — Session split on compression

**Files:**
- Modify: `nanobot/agent/autocompact.py`

- [ ] **Step 1: Add imports**

At top of `nanobot/agent/autocompact.py`, add:

```python
from nanobot.session.db import generate_session_id
```

- [ ] **Step 2: Add session split in _archive()**

In `_archive()` method, after `session.last_consolidated = 0` (line 93) and before `session.updated_at = datetime.now()` (line 94), add:

```python
# Session split: end old SQLite session, create new one with lineage
if session.db_id:
    db = self.sessions._db
    db.end_session(session.db_id, "compression")
    old_db_id = session.db_id
    session.db_id = generate_session_id()
    session.last_db_flush_idx = 0
    db.create_session(
        session.db_id,
        session_key=key,
        source="compression",
        parent_session_id=old_db_id,
    )
    old_title = db.get_session_title(old_db_id)
    if old_title:
        db.set_session_title(
            session.db_id, db.get_next_title_in_lineage(old_title)
        )
```

- [ ] **Step 3: Run existing tests to verify nothing broke**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/ -x --timeout=30 -q`
Expected: No new failures

- [ ] **Step 4: Commit**

```bash
git add nanobot/agent/autocompact.py
git commit -m "feat(autocompact): add SQLite session split on compression with lineage tracking"
```

---

### Task 10: Full integration test run

- [ ] **Step 1: Run all new tests together**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/test_session_db.py tests/test_session_manager_db.py tests/test_session_search.py tests/test_session_title.py -v`
Expected: All PASS

- [ ] **Step 2: Run full test suite**

Run: `cd /home/yanghan/workspace/nanobot && uv run pytest tests/ -x --timeout=30 -q`
Expected: No new failures (pre-existing failures OK)

- [ ] **Step 3: Verify ruff passes**

Run: `cd /home/yanghan/workspace/nanobot && uv run ruff check nanobot/session/db.py nanobot/session/title.py nanobot/agent/tools/session_search.py`
Expected: No errors
