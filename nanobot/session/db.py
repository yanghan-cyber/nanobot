"""SQLite-backed session storage with FTS5 full-text search."""

from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

from loguru import logger

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id                    TEXT PRIMARY KEY,
    session_key           TEXT NOT NULL,
    source                TEXT NOT NULL DEFAULT 'agent',
    model                 TEXT,
    title                 TEXT,
    user_id               TEXT,
    parent_session_id     TEXT,
    system_prompt_snapshot TEXT,
    started_at            REAL NOT NULL,
    ended_at              REAL,
    end_reason            TEXT,
    message_count         INTEGER DEFAULT 0,
    input_tokens          INTEGER DEFAULT 0,
    output_tokens         INTEGER DEFAULT 0,
    cache_read_tokens     INTEGER DEFAULT 0
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
"""

_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(content, content='messages', content_rowid='id');

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram
    USING fts5(content, tokenize='trigram', content='messages', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, COALESCE(old.content, '') || ' ' || COALESCE(old.tool_name, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, COALESCE(old.content, '') || ' ' || COALESCE(old.tool_name, ''));
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_trigram_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_trigram_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content) VALUES('delete', old.id, COALESCE(old.content, '') || ' ' || COALESCE(old.tool_name, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_trigram_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content) VALUES('delete', old.id, COALESCE(old.content, '') || ' ' || COALESCE(old.tool_name, ''));
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (new.id, COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, ''));
END;
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_sessions_session_key ON sessions(session_key);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_ended_at ON sessions(ended_at);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
"""


def generate_session_id() -> str:
    """Generate a unique session ID in YYYYMMDD_HHMMSS_<6hex> format."""
    now = datetime.now()
    return now.strftime("%Y%m%d_%H%M%S") + "_" + token_hex(3)


class SessionDB:
    """SQLite-backed session storage.

    Manages session lifecycle (create, read, update, end) with FTS5 full-text
    search support for messages.
    """

    _SESSION_UPDATABLE_COLS: frozenset[str] = frozenset({
        "title",
        "model",
        "system_prompt_snapshot",
        "message_count",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
    })

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=wal")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        """Create or verify all schema objects."""
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.executescript(_FTS_SQL)
        self._conn.executescript(_INDEX_SQL)
        cursor = self._conn.execute("SELECT COUNT(*) FROM schema_version")
        if cursor.fetchone()[0] == 0:
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, time.time()),
            )
            self._conn.commit()

    def _execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a write statement with retry on lock contention."""
        max_retries = 15
        for attempt in range(max_retries):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    cursor = self._conn.execute(sql, params)
                    self._conn.commit()
                    return cursor
            except sqlite3.OperationalError as e:
                self._conn.rollback()
                error_str = str(e)
                if "locked" not in error_str and "busy" not in error_str:
                    raise
                if attempt < max_retries - 1:
                    jitter = random.uniform(0.02, 0.15)
                    logger.warning(
                        "Database locked (attempt {}/{}), retrying in {:.0f}ms: {}",
                        attempt + 1,
                        max_retries,
                        jitter * 1000,
                        e,
                    )
                    time.sleep(jitter)
                else:
                    logger.error(
                        "Database locked after {} attempts: {}",
                        max_retries,
                        e,
                    )
                    raise

    def create_session(
        self,
        session_id: str,
        *,
        session_key: str,
        source: str = "agent",
        model: str | None = None,
        user_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        """Create a new session."""
        now = time.time()
        self._execute_write(
            "INSERT INTO sessions (id, session_key, source, model, user_id, parent_session_id, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, session_key, source, model, user_id, parent_session_id, now),
        )

    def ensure_session(
        self,
        session_id: str,
        *,
        session_key: str,
        source: str = "agent",
        model: str | None = None,
        user_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        """Create a session only if it doesn't already exist."""
        existing = self.get_session(session_id)
        if existing is None:
            self.create_session(
                session_id,
                session_key=session_key,
                source=source,
                model=model,
                user_id=user_id,
                parent_session_id=parent_session_id,
            )

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended with the given reason."""
        self._execute_write(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (time.time(), end_reason, session_id),
        )

    def update_session(self, session_id: str, **kwargs: Any) -> None:
        """Update updatable columns on a session.

        Only keys in _SESSION_UPDATABLE_COLS are applied; unknown keys are
        silently ignored.
        """
        filtered = {k: v for k, v in kwargs.items() if k in self._SESSION_UPDATABLE_COLS}
        if not filtered:
            return
        set_clause = ", ".join(f"{col} = ?" for col in filtered)
        values = list(filtered.values())
        values.append(session_id)
        self._execute_write(
            f"UPDATE sessions SET {set_clause} WHERE id = ?",
            tuple(values),
        )

    def get_session(self, session_id: str) -> dict | None:
        """Retrieve a session by ID, or None if not found."""
        cursor = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def get_active_session(self, session_key: str) -> dict | None:
        """Retrieve the most recent active (non-ended) session for a key."""
        cursor = self._conn.execute(
            "SELECT * FROM sessions WHERE session_key = ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
            (session_key,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str | None = None,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        token_count: int | None = None,
        finish_reason: str | None = None,
        reasoning_content: str | None = None,
    ) -> None:
        """Append a message to a session and increment its message_count atomically."""
        if tool_calls is not None:
            tool_calls = json.dumps(tool_calls)
        max_retries = 15
        for attempt in range(max_retries):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    self._conn.execute(
                        "INSERT INTO messages (session_id, role, content, tool_calls, "
                        "tool_call_id, tool_name, token_count, finish_reason, "
                        "reasoning_content, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_id, role, content, tool_calls,
                            tool_call_id, tool_name, token_count,
                            finish_reason, reasoning_content, time.time(),
                        ),
                    )
                    self._conn.execute(
                        "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                        (session_id,),
                    )
                    self._conn.commit()
                    return
            except sqlite3.OperationalError as e:
                self._conn.rollback()
                error_str = str(e)
                if "locked" not in error_str and "busy" not in error_str:
                    raise
                if attempt < max_retries - 1:
                    jitter = random.uniform(0.02, 0.15)
                    logger.warning(
                        "Database locked (attempt {}/{}), retrying in {:.0f}ms: {}",
                        attempt + 1,
                        max_retries,
                        jitter * 1000,
                        e,
                    )
                    time.sleep(jitter)
                else:
                    logger.error(
                        "Database locked after {} attempts: {}",
                        max_retries,
                        e,
                    )
                    raise

    def get_messages(self, session_id: str) -> list[dict]:
        """Retrieve all messages for a session, ordered by creation time."""
        cursor = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def set_session_title(self, session_id: str, title: str) -> None:
        """Set the title of a session."""
        self.update_session(session_id, title=title)

    def get_session_title(self, session_id: str) -> str | None:
        """Get the title of a session, or None if not found."""
        cursor = self._conn.execute(
            "SELECT title FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return row["title"]

    @staticmethod
    def get_next_title_in_lineage(title: str) -> str:
        """Increment a session title lineage, e.g. 'Foo' -> 'Foo #2', 'Foo #2' -> 'Foo #3'."""
        m = re.fullmatch(r"^(.*) #(\d+)$", title)
        if m:
            base = m.group(1)
            num = int(m.group(2)) + 1
            return f"{base} #{num}"
        return f"{title} #2"

    def update_token_counts(
        self,
        session_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """Additively update token counts for a session."""
        self._execute_write(
            "UPDATE sessions SET input_tokens = input_tokens + ?, "
            "output_tokens = output_tokens + ?, "
            "cache_read_tokens = cache_read_tokens + ? "
            "WHERE id = ?",
            (input_tokens, output_tokens, cache_read_tokens, session_id),
        )

    def list_recent_sessions(self, limit: int = 10) -> list[dict]:
        """List the most recent sessions, ordered by started_at descending."""
        cursor = self._conn.execute(
            "SELECT id, session_key, source, model, title, started_at, ended_at, "
            "message_count, input_tokens, output_tokens "
            "FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]
