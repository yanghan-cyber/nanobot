# Session Storage Design

Add structured SQLite session storage with FTS5 full-text search to nanobot, enabling cross-session memory retrieval for future memory system enhancements.

## Goals

- Persist all conversation history (including tool calls, reasoning) to SQLite alongside existing JSONL storage
- Enable FTS5-powered keyword search across all past sessions
- Support session lifecycle: creation, compression splitting, subagent recording
- Provide `session_search` built-in tool for the agent
- Keep existing JSONL storage unchanged (incremental addition)

## Non-Goals

- Vector/semantic search (future iteration, would require embedding model)
- Replacing existing JSONL storage
- Billing/cost tracking (no pricing data available yet)
- Real-time per-message SQLite writes (write at existing `save()` call points)

## Architecture Overview

```
SessionManager.save(session)
    ├─ JSONL full rewrite (unchanged)
    └─ SessionDB.persist(session)         ← new: incremental INSERT new messages
         ├─ ensure_session(db_id, ...)
         ├─ append_message(...) for messages[last_db_flush_idx:]
         └─ update session.last_db_flush_idx

Autocompact._archive(key)
    ├─ SessionDB.end_session(old_db_id, "compression")
    ├─ session.db_id = new_generated_id
    └─ SessionDB.create_session(new_db_id, parent_session_id=old_db_id)

SubagentManager._run_subagent(...)
    ├─ SessionDB.create_session(subagent_db_id, parent_session_id=...)
    ├─ [runner.run() completes]
    ├─ SessionDB.append_message(...) for all result.messages
    └─ SessionDB.end_session(subagent_db_id, stop_reason)
```

## Session ID Design

**Format:** `YYYYMMDD_HHMMSS_<6hex>` (e.g., `20260419_143052_a3f1b2`)

- Generated on session creation and autocompact split
- Persisted in JSONL metadata line for recovery across restarts
- `session_key` (e.g., `telegram:12345`) retained as grouping field

**Session identity flow:**

| Event | session_id behavior |
|-------|-------------------|
| New session created | Generate new `db_id` |
| Process restart | Recover `db_id` from JSONL metadata line |
| Autocompact split | Old `db_id` ended, new `db_id` generated |
| Subagent spawn | Independent `db_id`, linked via `parent_session_id` |
| User `/new` or clear | Old `db_id` ended, new `db_id` generated |

## SQLite Schema

**Location:** `~/.nanobot/state.db`

```sql
CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,                   -- YYYYMMDD_HHMMSS_<6hex>
    session_key TEXT NOT NULL,             -- "channel:chat_id", grouping field
    source TEXT NOT NULL DEFAULT 'cli',    -- platform: cli, telegram, discord, etc.
    user_id TEXT,                          -- sender_id from InboundMessage
    model TEXT,
    title TEXT,                            -- auto-generated or manual
    parent_session_id TEXT,                -- linked session (compression or subagent)
    end_reason TEXT,                       -- 'compression'|'ttl_archive'|'manual'|NULL
    started_at REAL NOT NULL,             -- unix timestamp
    ended_at REAL,                        -- unix timestamp, NULL = active
    message_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    system_prompt_snapshot TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,                    -- user|assistant|tool|system
    content TEXT,
    tool_calls TEXT,                       -- JSON-serialized
    tool_call_id TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning_content TEXT
);

-- FTS5 full-text search
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

-- Triggers to keep FTS5 in sync
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

-- Indexes
CREATE INDEX idx_sessions_key ON sessions(session_key, started_at DESC);
CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
```

### Field Sources

| Field | Data source | Location |
|-------|-----------|----------|
| `session_key` | `InboundMessage.session_key` | `bus/events.py:22-24` |
| `user_id` | `InboundMessage.sender_id` | `bus/events.py:13` |
| `model` | `AgentLoop.model` | `loop.py:174` |
| `reasoning_content` | `response.reasoning_content` | `runner.py:284` |
| `prompt_tokens` | `result.usage["prompt_tokens"]` | `runner.py:271` |
| `completion_tokens` | `result.usage["completion_tokens"]` | `runner.py:271` |
| `cached_tokens` | `result.usage["cached_tokens"]` | `loop.py:119` |
| `tool_calls` | `msg["tool_calls"]` | `helpers.py:326-328` |

## Components

### 1. SessionDB (`nanobot/session/db.py`) — NEW

SQLite operations class. Key responsibilities:

- Initialize DB with WAL mode, create tables and FTS5 indexes
- Session CRUD (create, ensure, end, update, get)
- Message append with incremental flush tracking
- FTS5 search with query sanitization
- Application-level write retry with random jitter

**Core methods:**

```python
class SessionDB:
    def __init__(self, path: Path)
    # Session CRUD
    def create_session(self, session_id, session_key, source, model,
                       user_id=None, parent_session_id=None) -> None
    def ensure_session(self, session_id, **kwargs) -> None  # INSERT OR IGNORE
    def end_session(self, session_id, end_reason) -> None   # set ended_at, end_reason
    def update_session(self, session_id, **kwargs) -> None
    def get_session(self, session_id) -> dict | None
    def get_active_session(self, session_key) -> dict | None
    # Messages
    def append_message(self, session_id, role, content, **kwargs) -> None
    def get_messages(self, session_id) -> list[dict]
    # Search
    def search_messages(self, query, limit=20) -> list[dict]
    def list_recent_sessions(self, limit=10) -> list[dict]
    # Title
    def set_session_title(self, session_id, title) -> None
    def get_session_title(self, session_id) -> str | None
    def get_next_title_in_lineage(self, title) -> str  # "Foo" → "Foo #2"
    # Stats
    def update_token_counts(self, session_id, input_tokens, output_tokens,
                            cache_read_tokens) -> None
```

**Write contention handling** (borrowed from hermes):

```python
_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.020   # 20ms
_WRITE_RETRY_MAX_S = 0.150   # 150ms
# Each retry: BEGIN IMMEDIATE + random jitter sleep
```

### 2. Session Dataclass Extension (`nanobot/session/manager.py`)

Add two fields to existing `Session`:

```python
@dataclass
class Session:
    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0
    # New fields
    db_id: str = ""                  # SQLite session_id
    last_db_flush_idx: int = 0       # tracks which messages already in SQLite
```

### 3. SessionManager Integration (`nanobot/session/manager.py`)

**`__init__`:** Initialize `SessionDB`

```python
def __init__(self, workspace: Path):
    self.workspace = workspace
    self.sessions_dir = ensure_dir(self.workspace / "sessions")
    self._cache: dict[str, Session] = {}
    self._db = SessionDB(Path.home() / ".nanobot" / "state.db")
```

**`save()`:** Add SQLite incremental write after JSONL

```python
def save(self, session: Session) -> None:
    # Existing JSONL write (unchanged)
    ...

    # New: SQLite incremental write
    self._db.ensure_session(
        session.db_id,
        session_key=session.key,
        source=...,
        model=...,
    )
    for msg in session.messages[session.last_db_flush_idx:]:
        self._db.append_message(session.db_id, ...)
    session.last_db_flush_idx = len(session.messages)
```

**`get_or_create()`:** Generate `db_id` for new sessions

```python
def get_or_create(self, key: str) -> Session:
    if key in self._cache:
        return self._cache[key]
    session = self._load(key)
    if session is None:
        session = Session(key=key, db_id=_generate_session_id())
    self._cache[key] = session
    return session
```

### 4. JSONL Metadata Extension (`nanobot/session/manager.py`)

**`save()`:** Persist `db_id` and `last_db_flush_idx` in metadata line

```python
metadata_line = {
    "_type": "metadata",
    "key": session.key,
    "created_at": session.created_at.isoformat(),
    "updated_at": session.updated_at.isoformat(),
    "metadata": session.metadata,
    "last_consolidated": session.last_consolidated,
    "db_id": session.db_id,                      # NEW
    "last_db_flush_idx": session.last_db_flush_idx,  # NEW
}
```

**`_load()`:** Recover `db_id` from metadata line

```python
db_id = data.get("db_id", "")
last_db_flush_idx = data.get("last_db_flush_idx", 0)
if not db_id:
    # Legacy JSONL migration: generate new db_id
    db_id = _generate_session_id()
    last_db_flush_idx = 0  # will re-import all messages
return Session(key=key, ..., db_id=db_id, last_db_flush_idx=last_db_flush_idx)
```

### 5. Autocompact Session Split (`nanobot/agent/autocompact.py`)

In `_archive()`, after archiving old messages:

1. `SessionDB.end_session(old_db_id, "compression")`
2. Generate new `db_id`, reset `last_db_flush_idx = 0`
3. `SessionDB.create_session(new_db_id, parent_session_id=old_db_id)`
4. Inherit title with auto-numbering (`"Debug API"` → `"Debug API #2"`)

### 6. Subagent Session Recording (`nanobot/agent/subagent.py`)

In `_run_subagent()`:

1. Before `runner.run()`: `SessionDB.create_session(subagent_db_id, parent_session_id=...)`
2. After `runner.run()`: batch write all `result.messages` to SQLite
3. `SessionDB.end_session(subagent_db_id, stop_reason)`
4. Update token counts from `result.usage`

SubagentManager needs access to `SessionDB` via `SessionManager`:

```python
class SubagentManager:
    def __init__(self, ..., session_manager: SessionManager):
        self.sessions = session_manager
```

### 7. SessionSearchTool (`nanobot/agent/tools/session_search.py`) — NEW

Two modes:

**Mode 1: Recent sessions (no query, zero LLM cost)**

- Query SQLite for latest sessions with metadata
- Return titles, previews, timestamps

**Mode 2: Keyword search (with query, LLM summarization)**

- FTS5 search → group by session → top N sessions
- Concatenate matched messages → LLM generates summary
- Return structured results

**Tool schema:**

```python
{
    "name": "session_search",
    "description": "Search past conversation memory or browse recent sessions...",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search keywords (supports OR/NOT/phrases/prefix). Empty = recent sessions."
            },
            "limit": {
                "type": "integer",
                "default": 3,
                "description": "Max sessions to return (1-5)"
            }
        }
    }
}
```

### 8. Session Title Auto-Generation

Triggered after session's first 2 user exchanges:

- Background async task (non-blocking)
- LLM prompt: "Generate a 3-7 word title for this conversation"
- Uses configured model (same as agent)
- Stored in `sessions.title` in SQLite

## ended_at Update Triggers

| Trigger | end_reason | Description |
|---------|-----------|-------------|
| Autocompact split | `"compression"` | Old session closed, new session takes over |
| TTL expiry archive | `"ttl_archive"` | Session idle too long |
| User `/new` or clear | `"manual"` | Explicit new conversation |
| Process shutdown | — | NOT triggered (session continues across restarts) |

`ended_at IS NULL` means session is still active.

## Recovery Scenarios

| Scenario | Behavior |
|----------|----------|
| Normal restart | Recover `db_id` from JSONL metadata, continue writing to same SQLite session |
| Legacy JSONL (no `db_id`) | Generate new `db_id`, import existing messages to SQLite |
| JSONL exists, state.db deleted | Rebuild session record + messages from JSONL |
| After autocompact restart | `db_id` is already the new one, old session properly ended |

## Files Changed

| File | Type | Description |
|------|------|-------------|
| `nanobot/session/db.py` | NEW | SessionDB class with SQLite + FTS5 |
| `nanobot/session/manager.py` | MODIFY | Extend Session dataclass, add SQLite writes to save/load |
| `nanobot/agent/tools/session_search.py` | NEW | session_search built-in tool |
| `nanobot/agent/autocompact.py` | MODIFY | Add session split logic |
| `nanobot/agent/subagent.py` | MODIFY | Add subagent session recording |
| `nanobot/agent/loop.py` | MODIFY | Register session_search tool, pass SessionManager to SubagentManager |
| `tests/test_session_db.py` | NEW | SessionDB unit tests |
| `tests/test_session_search.py` | NEW | SessionSearchTool tests |

## Implementation Order

1. **SessionDB module** — core SQLite operations, schema, FTS5
2. **Session dataclass + SessionManager integration** — db_id, save/load changes
3. **Autocompact split** — session lifecycle on compression
4. **Subagent recording** — batch persist subagent results
5. **SessionSearchTool** — FTS5 search + LLM summarization
6. **Session title generation** — auto-title on first exchanges
7. **Tests** — unit tests for all new components
