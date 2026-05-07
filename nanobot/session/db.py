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
from typing import Any, Callable

from loguru import logger

_SCHEMA_VERSION = 3

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
    last_active_at        REAL,
    terminated_at         REAL,
    termination_reason    TEXT,
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

CREATE TABLE IF NOT EXISTS tool_invocations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tool_name   TEXT NOT NULL,
    skill_name  TEXT,
    created_at  REAL NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_tool_invocations_ai
AFTER INSERT ON messages
WHEN NEW.tool_calls IS NOT NULL
BEGIN
    INSERT INTO tool_invocations (session_id, tool_name, skill_name, created_at)
    SELECT
        NEW.session_id,
        json_extract(tc.value, '$.function.name'),
        CASE
            WHEN json_extract(tc.value, '$.function.name') = 'load_skill'
            THEN json_extract(json_extract(tc.value, '$.function.arguments'), '$.skill_name')
            ELSE NULL
        END,
        NEW.created_at
    FROM json_each(NEW.tool_calls) AS tc;
END;

CREATE VIEW IF NOT EXISTS v_tool_stats AS
    SELECT tool_name, COUNT(*) AS total_calls
    FROM tool_invocations
    GROUP BY tool_name
    ORDER BY total_calls DESC;

CREATE VIEW IF NOT EXISTS v_skill_stats AS
    SELECT skill_name, COUNT(*) AS total_calls
    FROM tool_invocations
    WHERE skill_name IS NOT NULL
    GROUP BY skill_name
    ORDER BY total_calls DESC;
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
CREATE INDEX IF NOT EXISTS idx_sessions_terminated_at ON sessions(terminated_at);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_inv_session ON tool_invocations(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_inv_tool_name ON tool_invocations(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_inv_skill_name ON tool_invocations(skill_name);
"""


def _contains_cjk(text: str) -> bool:
    """Return True if *text* contains any CJK characters."""
    return any('一' <= c <= '鿿' or '㐀' <= c <= '䶿' for c in text)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Migrate schema from v1 to v2: rename ended_at/end_reason fields."""
    # Rename ended_at -> last_active_at
    conn.execute("ALTER TABLE sessions RENAME COLUMN ended_at TO last_active_at")
    # Add terminated_at column
    conn.execute("ALTER TABLE sessions ADD COLUMN terminated_at REAL")
    # Only set terminated_at for sessions that were explicitly ended (end_reason IS NOT NULL).
    # In v1, save() set ended_at on every save, but end_session() also set end_reason.
    # Sessions with end_reason IS NULL were saved but never terminated -- they should stay active.
    conn.execute(
        "UPDATE sessions SET terminated_at = last_active_at "
        "WHERE last_active_at IS NOT NULL AND end_reason IS NOT NULL"
    )
    # Rename end_reason -> termination_reason
    conn.execute("ALTER TABLE sessions RENAME COLUMN end_reason TO termination_reason")
    # Update index
    conn.execute("DROP INDEX IF EXISTS idx_sessions_ended_at")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_terminated_at ON sessions(terminated_at)")
    conn.commit()


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Migrate schema from v2 to v3: add tool_invocations table with trigger, indexes, views; backfill."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_invocations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            tool_name   TEXT NOT NULL,
            skill_name  TEXT,
            created_at  REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_tool_invocations_ai
        AFTER INSERT ON messages
        WHEN NEW.tool_calls IS NOT NULL
        BEGIN
            INSERT INTO tool_invocations (session_id, tool_name, skill_name, created_at)
            SELECT
                NEW.session_id,
                json_extract(tc.value, '$.function.name'),
                CASE
                    WHEN json_extract(tc.value, '$.function.name') = 'load_skill'
                    THEN json_extract(json_extract(tc.value, '$.function.arguments'), '$.skill_name')
                    ELSE NULL
                END,
                NEW.created_at
            FROM json_each(NEW.tool_calls) AS tc;
        END
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_inv_session ON tool_invocations(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_inv_tool_name ON tool_invocations(tool_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_inv_skill_name ON tool_invocations(skill_name)")
    # Backfill from existing messages (guard against DBs where messages table doesn't exist yet)
    has_messages = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()[0] > 0
    if has_messages:
        conn.execute("""
            INSERT INTO tool_invocations (session_id, tool_name, skill_name, created_at)
            SELECT
                m.session_id,
                json_extract(tc.value, '$.function.name'),
                CASE
                    WHEN json_extract(tc.value, '$.function.name') = 'load_skill'
                    THEN json_extract(json_extract(tc.value, '$.function.arguments'), '$.skill_name')
                    ELSE NULL
                END,
                m.created_at
            FROM messages m, json_each(m.tool_calls) AS tc
            WHERE m.tool_calls IS NOT NULL
        """)
    conn.execute("""
        CREATE VIEW IF NOT EXISTS v_tool_stats AS
            SELECT tool_name, COUNT(*) AS total_calls
            FROM tool_invocations
            GROUP BY tool_name
            ORDER BY total_calls DESC
    """)
    conn.execute("""
        CREATE VIEW IF NOT EXISTS v_skill_stats AS
            SELECT skill_name, COUNT(*) AS total_calls
            FROM tool_invocations
            WHERE skill_name IS NOT NULL
            GROUP BY skill_name
            ORDER BY total_calls DESC
    """)
    conn.commit()


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_v1_to_v2,
    2: _migrate_v2_to_v3,
}


def _count_cjk(text: str) -> int:
    """Count the number of CJK characters in *text*."""
    return sum(1 for c in text if '一' <= c <= '鿿' or '㐀' <= c <= '䶿')


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
        "last_active_at",
    })

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=wal")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA cache_size = -128")  # 128 KB, avoid per-connection bloat
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def close(self) -> None:
        """Explicitly close the SQLite connection, freeing C-level resources."""
        try:
            self._conn.close()
        except Exception:
            pass

    def _init_schema(self) -> None:
        """Create or verify all schema objects, running migrations as needed."""
        # Check if database exists
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        db_exists = cursor.fetchone()[0] > 0

        if not db_exists:
            # Fresh database - create everything
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.executescript(_FTS_SQL)
            self._conn.executescript(_INDEX_SQL)
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, time.time()),
            )
            self._conn.commit()
            return

        # Existing database - check version and migrate
        cursor = self._conn.execute("SELECT version FROM schema_version")
        row = cursor.fetchone()
        current_version = row[0] if row else 0

        if current_version < _SCHEMA_VERSION:
            logger.info("Migrating database from v{} to v{}", current_version, _SCHEMA_VERSION)
            for v in range(current_version, _SCHEMA_VERSION):
                if v in _MIGRATIONS:
                    _MIGRATIONS[v](self._conn)
                    self._conn.execute(
                        "UPDATE schema_version SET version = ?, applied_at = ?",
                        (v + 1, time.time()),
                    )
                    self._conn.commit()
                    logger.info("Migration v{} -> v{} complete", v, v + 1)
        elif current_version > _SCHEMA_VERSION:
            logger.warning(
                "Database version {} is newer than expected {}. Downgrading is not supported.",
                current_version,
                _SCHEMA_VERSION,
            )

    def _execute_write(self, sql: str, params: tuple = ()) -> None:
        """Execute a write statement with retry on lock contention."""
        max_retries = 15
        for attempt in range(max_retries):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    self._conn.execute(sql, params)
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

    def end_session(self, session_id: str, termination_reason: str) -> None:
        """Mark a session as terminated with the given reason.

        No-op when *session_id* does not exist.
        """
        row = self._conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return
        self._execute_write(
            "UPDATE sessions SET terminated_at = ?, termination_reason = ? WHERE id = ?",
            (time.time(), termination_reason, session_id),
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
        """Retrieve the most recent active (non-terminated) session for a key."""
        cursor = self._conn.execute(
            "SELECT * FROM sessions WHERE session_key = ? AND terminated_at IS NULL ORDER BY started_at DESC LIMIT 1",
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

    def list_recent_sessions(self, limit: int = 10, session_key: str | None = None) -> list[dict]:
        """List the most recent sessions, ordered by started_at descending."""
        if session_key:
            cursor = self._conn.execute(
                "SELECT id, session_key, source, model, title, started_at, "
                "last_active_at, terminated_at, termination_reason, "
                "message_count, input_tokens, output_tokens "
                "FROM sessions WHERE session_key = ? ORDER BY started_at DESC LIMIT ?",
                (session_key, limit),
            )
        else:
            cursor = self._conn.execute(
                "SELECT id, session_key, source, model, title, started_at, "
                "last_active_at, terminated_at, termination_reason, "
                "message_count, input_tokens, output_tokens "
                "FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Clean *query* so it is safe for FTS5 MATCH.

        - Preserve quoted phrases as-is.
        - Strip characters that are special to FTS5 but not useful for
          ad-hoc user queries.
        """
        parts: list[str] = []
        for token in re.split(r'(\"[^\"]*\")', query):
            if not token:
                continue
            if token.startswith('"') and token.endswith('"'):
                parts.append(token)
            else:
                cleaned = re.sub(r'[^\w\s]+', ' ', token)
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()
                if cleaned:
                    parts.append(cleaned)
        return ' '.join(parts)

    def search_messages(
        self,
        query: str,
        *,
        role_filter: list[str] | None = None,
        exclude_sources: list[str] | None = None,
        session_key: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search messages using FTS5, with CJK trigram and LIKE fallback."""
        sanitized = self._sanitize_fts5_query(query)
        if not sanitized.strip():
            return []

        if _contains_cjk(query):
            cjk_count = _count_cjk(query)
            if cjk_count >= 3:
                # Use trigram FTS5 for CJK queries with >= 3 chars
                sql = (
                    "SELECT s.id as session_id, s.source, m.role, m.content, m.created_at"
                    " FROM messages_fts_trigram fts"
                    " JOIN messages m ON m.id = fts.rowid"
                    " JOIN sessions s ON s.id = m.session_id"
                    " WHERE messages_fts_trigram MATCH ?"
                )
                params: list[Any] = [sanitized]
            else:
                # LIKE fallback for short CJK queries (1-2 chars)
                like_term = f"%{query}%"
                sql = (
                    "SELECT s.id as session_id, s.source, m.role, m.content, m.created_at"
                    " FROM messages m"
                    " JOIN sessions s ON s.id = m.session_id"
                    " WHERE (m.content LIKE ? OR m.tool_name LIKE ? OR m.tool_calls LIKE ?)"
                )
                params = [like_term, like_term, like_term]
        else:
            # Default FTS5 for non-CJK queries
            sql = (
                "SELECT s.id as session_id, s.source, m.role, m.content, m.created_at"
                " FROM messages_fts fts"
                " JOIN messages m ON m.id = fts.rowid"
                " JOIN sessions s ON s.id = m.session_id"
                " WHERE messages_fts MATCH ?"
            )
            params = [sanitized]

        if role_filter:
            placeholders = ','.join('?' * len(role_filter))
            sql += f" AND m.role IN ({placeholders})"
            params.extend(role_filter)

        if exclude_sources:
            placeholders = ','.join('?' * len(exclude_sources))
            sql += f" AND s.source NOT IN ({placeholders})"
            params.extend(exclude_sources)

        if session_key:
            sql += " AND s.session_key = ?"
            params.append(session_key)

        if _contains_cjk(query) and _count_cjk(query) < 3:
            sql += " ORDER BY m.created_at DESC"
        else:
            sql += " ORDER BY rank"

        sql += " LIMIT ?"
        params.append(limit)

        try:
            cursor = self._conn.execute(sql, tuple(params))
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in cursor.fetchall()]
