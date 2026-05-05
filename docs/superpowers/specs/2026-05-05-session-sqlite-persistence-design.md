# Session SQLite Persistence — Design Spec

## Context

Nanobot's current session storage is JSONL-based. Each session is a single JSONL file that gets overwritten on every save. When autocompact triggers compression, old messages are permanently lost — there is no way to search or retrieve past conversations.

This spec adds a SQLite sidecar database as a **read-only shadow** of all session activity. SQLite retains every message ever written, enabling full-text search (FTS5 with trigram CJK support), session lineage tracking, auto-title generation, and subagent conversation persistence. The existing JSONL logic is completely untouched.

## Architecture Overview

```
SessionManager.save()
    ├── SQLite incremental flush (new, failure = warning only)  ← before JSONL so last_db_flush_idx aligns
    └── JSONL atomic write (unchanged, metadata includes updated flush_idx)

AgentLoop
    ├── registers SessionSearchTool (new)
    └── triggers auto_title after first exchanges (new)

SubagentManager._run_subagent()
    └── persists full subagent conversation to SQLite (new)

AutoCompact._archive()
    └── session split: end old SQLite session, create new with lineage (new)
```

**Key principle:** SQLite is append-only. No records are ever deleted. JSONL remains the source of truth for the running session; SQLite is the permanent archive.

## Files Changed

| File | Action | Description |
|------|--------|-------------|
| `nanobot/session/db.py` | **New** | SessionDB: SQLite schema, CRUD, FTS5 search |
| `nanobot/session/title.py` | **New** | Auto-title generation (LLM call + sanitization) |
| `nanobot/session/manager.py` | Modify | Add `db_id`, `last_db_flush_idx` to Session; SQLite flush in `save()` |
| `nanobot/agent/tools/session_search.py` | **New** | SessionSearchTool: FTS5 search + LLM summarization |
| `nanobot/agent/loop.py` | Modify | Register SessionSearchTool, trigger auto-title |
| `nanobot/agent/subagent.py` | Modify | Persist subagent conversations to SQLite |
| `nanobot/agent/autocompact.py` | Modify | Session split on compression |
| `tests/test_session_db.py` | **New** | SessionDB tests |
| `tests/test_session_manager_db.py` | **New** | SessionManager SQLite integration tests |
| `tests/test_session_search.py` | **New** | SessionSearchTool tests |
| `tests/test_session_title.py` | **New** | Auto-title tests |

## Part 1: SessionDB (`nanobot/session/db.py`)

### SQLite Schema

```sql
CREATE TABLE schema_version (
    version INTEGER NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE sessions (
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

CREATE TABLE messages (
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
```

### FTS5 — Dual Tables (hermes-agent pattern)

```sql
-- Default tokenizer (English/punctuation)
CREATE VIRTUAL TABLE messages_fts
    USING fts5(content, content='messages', content_rowid='id');

-- Trigram tokenizer (CJK substring matching)
CREATE VIRTUAL TABLE messages_fts_trigram
    USING fts5(content, tokenize='trigram', content='messages', content_rowid='id');
```

Each table has INSERT/DELETE/UPDATE triggers that sync from `messages`. FTS content = `COALESCE(content, '') || ' ' || COALESCE(tool_name, '')`.

### Indexes

```sql
CREATE INDEX idx_sessions_session_key ON sessions(session_key);
CREATE INDEX idx_sessions_started_at ON sessions(started_at);
CREATE INDEX idx_sessions_ended_at ON sessions(ended_at);
CREATE INDEX idx_messages_session_id ON messages(session_id);
```

### SessionDB Class

**Constructor:** `SessionDB(path: Path)`
- `PRAGMA journal_mode=WAL`
- `PRAGMA foreign_keys=ON`
- `threading.Lock` for write operations
- `_init_schema()` on first use

**Session CRUD:**
- `create_session(session_id, *, session_key, source, model, user_id, parent_session_id)`
- `ensure_session(...)` — idempotent create
- `end_session(session_id, end_reason)`
- `update_session(session_id, **kwargs)` — whitelist updatable columns
- `get_session(session_id) -> dict | None`
- `get_active_session(session_key) -> dict | None`

**Message CRUD:**
- `append_message(session_id, *, role, content, tool_calls, tool_call_id, tool_name, token_count, finish_reason, reasoning_content)` — also increments `session.message_count`
- `get_messages(session_id) -> list[dict]`

**FTS5 Search (hermes-agent logic):**
- `_contains_cjk(query) -> bool` — detect CJK characters
- `_sanitize_fts5_query(query) -> str` — preserve quoted phrases, strip special chars
- `search_messages(query, *, role_filter=None, exclude_sources=None, limit=20) -> list[dict]`
  - Non-CJK query → `messages_fts MATCH`
  - CJK query >= 3 chars → `messages_fts_trigram MATCH`
  - CJK query 1-2 chars → `LIKE '%query%'` fallback on content + tool_name + tool_calls
  - Returns session_id, role, content, created_at for each match

**Title:**
- `set_session_title(session_id, title)`
- `get_session_title(session_id) -> str | None`
- `get_next_title_in_lineage(title) -> str` — "Foo" → "Foo #2"

**Token counts:**
- `update_token_counts(session_id, *, input_tokens, output_tokens, cache_read_tokens)` — additive

**Listing:**
- `list_recent_sessions(limit=10) -> list[dict]`

**Session ID generation:**
- `generate_session_id() -> str` — format `YYYYMMDD_HHMMSS_<6hex>`

**Thread safety:**
- All writes go through `_execute_write(sql, params)` which acquires `self._lock`
- Retry on locked DB (15 attempts, random jitter 20-150ms)
- Failures logged as warnings, never propagated to caller

## Part 2: SessionManager Integration (`nanobot/session/manager.py`)

### Session dataclass — 2 new fields

```python
db_id: str = ""                    # SQLite session ID
last_db_flush_idx: int = 0         # messages already flushed to SQLite
```

### SessionManager constructor

- New parameter: `db_path: Path | None = None`
- Default: `self.workspace / "session" / "db" / "state.db"` (per-workspace isolation)
- Creates `self._db = SessionDB(db_path)`

### `get_or_create()` changes

- New sessions get `db_id = generate_session_id()`
- Loaded sessions retain their `db_id` from JSONL metadata

### `save()` changes

Before the JSONL atomic write (so that `last_db_flush_idx` is aligned in the metadata line):

```python
try:
    self._db.ensure_session(session.db_id, session_key=session.key, source='agent')
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

The JSONL metadata line is updated to include `db_id` and `last_db_flush_idx` so they survive reload.

### `_load()` changes

Read `db_id` and `last_db_flush_idx` from JSONL metadata line. If `db_id` is missing (legacy file), generate a new one.

**All other SessionManager methods unchanged:** `_repair()`, `delete_session()`, `read_session_file()`, `flush_all()`, `list_sessions()`, `enforce_file_cap()`.

## Part 3: Auto-Title (`nanobot/session/title.py`)

### `clean_title(raw: str) -> str`

- Strip quotes and surrounding whitespace
- Remove trailing punctuation (.!?)
- Truncate to 80 chars with "..." suffix
- Return empty string if result is empty

### `async auto_title(provider, model, user_content, assistant_content) -> str | None`

- Truncate inputs to 500 chars each
- Handle list content (join text blocks)
- LLM call with system prompt: "Generate a short descriptive title (3-7 words)... Return ONLY the title"
- Return `clean_title(response.content)` or `None` on failure

### Integration in `loop.py`

After `_save_turn()` and `sessions.save()`, check:
1. `session.db_id` exists
2. No title yet (`db.get_session_title(session.db_id) is None`)
3. 1-2 user messages so far

If conditions met, schedule background task:
```python
self._schedule_background(self._auto_title(session))
```

Where `_auto_title` calls `title.auto_title()` and `db.set_session_title()`.

## Part 4: Subagent Persistence (`nanobot/agent/subagent.py`)

### SubagentManager changes

- Constructor receives `session_manager: SessionManager | None = None`
- `_run_subagent()` gains `session_key: str | None = None` parameter

After `status.phase = "done"`:

```python
if self.sessions and result.messages:
    db = self.sessions._db
    parent_db_id = None
    if session_key:
        parent_session = self.sessions.get_or_create(session_key)
        parent_db_id = parent_session.db_id

    subagent_db_id = generate_session_id()
    db.create_session(
        subagent_db_id,
        session_key=f"subagent:{task_id}",
        source='subagent',
        model=self.model,
        parent_session_id=parent_db_id,
    )
    for msg in result.messages:
        content = msg.get("content")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False)
        db.append_message(
            subagent_db_id,
            role=msg.get("role", "unknown"),
            content=content,
            tool_calls=msg.get("tool_calls"),
            tool_call_id=msg.get("tool_call_id"),
            tool_name=msg.get("name"),
            reasoning_content=msg.get("reasoning_content"),
        )
    usage = result.usage or {}
    db.update_token_counts(subagent_db_id, ...)
    db.end_session(subagent_db_id, result.stop_reason or "completed")
```

## Part 5: Session Split on Compression (`nanobot/agent/autocompact.py`)

In `_archive()`, after `session.messages = kept_msgs` and `session.last_consolidated = 0`:

```python
if session.db_id:
    db = self.sessions._db
    db.end_session(session.db_id, "compression")
    old_db_id = session.db_id
    session.db_id = generate_session_id()
    session.last_db_flush_idx = 0
    db.create_session(
        session.db_id,
        session_key=key,
        source='compression',
        parent_session_id=old_db_id,
    )
    old_title = db.get_session_title(old_db_id)
    if old_title:
        db.set_session_title(session.db_id, db.get_next_title_in_lineage(old_title))
```

This creates a parent-child chain in SQLite:
```
session A (parent=None) → compression → session B (parent=A) → compression → session C (parent=B)
```

## Part 6: SessionSearchTool (`nanobot/agent/tools/session_search.py`)

### Schema

```python
{
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search keywords. Supports FTS5 syntax. Omit to list recent sessions."
        },
        "role_filter": {
            "type": "string",
            "description": "Comma-separated roles to include (e.g. 'user,assistant')"
        },
        "limit": {
            "type": "integer",
            "description": "Max sessions to summarize (1-5, default 3)",
            "default": 3
        }
    }
}
```

### Two modes

**Mode 1: No query → list recent sessions**
- `db.list_recent_sessions(limit)`
- Format: `[{session_id, when, source, model, title, message_count}]`

**Mode 2: With query → FTS5 search + LLM summary**
1. `db.search_messages(query, role_filter=..., exclude_sources=["subagent"])`
2. Group matches by session_id
3. Exclude current session and its lineage
4. Take top `limit` unique sessions
5. For each: get full messages, truncate transcript (3000 chars), call LLM for summary
6. Return JSON: `{success, query, count, results: [{session_id, when, source, model, summary}]}`

### Registration in `loop.py`

```python
self.tools.register(
    SessionSearchTool(db=self.sessions._db, provider=self.provider, model=self.model)
)
```

## Verification

1. **Unit tests:** `pytest tests/test_session_db.py tests/test_session_manager_db.py tests/test_session_search.py tests/test_session_title.py -x`
2. **Integration check:** Run nanobot CLI, have a conversation, check `~/.nanobot/state.db` contains sessions + messages
3. **Search test:** Use session_search tool to find messages from past sessions
4. **Compression test:** Trigger autocompact, verify parent-child lineage in SQLite
5. **Subagent test:** Spawn a subagent, verify its conversation appears in SQLite with correct parent_session_id
6. **CJK test:** Search with Chinese characters, verify trigram FTS5 and LIKE fallback work
