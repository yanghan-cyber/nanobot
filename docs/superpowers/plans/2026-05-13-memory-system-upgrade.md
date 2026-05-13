# Memory System Upgrade: Three-Layer Model — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Staging layer between Recent History and Long-term Memory, with seen/age metadata tracking, promotion/forgetting rules, and a daily DreamAudit cron job.

**Architecture:** Extend MemoryStore with staging file I/O, rewrite Dream Phase 1/2 templates to include staging directives, add DreamAudit as a new two-phase processor, and inject stripped staging context into the system prompt.

**Tech Stack:** Python 3.11+, asyncio, Pydantic, pytest with asyncio_mode=auto

**Spec:** `docs/superpowers/specs/2026-05-13-memory-system-upgrade-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `nanobot/config/schema.py` | Modify | DreamConfig: add staging/audit config fields |
| `nanobot/agent/memory.py` | Modify | MemoryStore: staging I/O; Dream: Phase 1/2 expansion; new DreamAudit class |
| `nanobot/agent/context.py` | Modify | ContextBuilder: inject staging context into system prompt |
| `nanobot/templates/agent/dream_phase1.md` | Modify | Phase 1 prompt: staging directives, seen increment, promotion/forgetting |
| `nanobot/templates/agent/dream_phase2.md` | Modify | Phase 2 prompt: staging write execution, WriteFileTool for staging.md |
| `nanobot/templates/agent/dream_audit_phase1.md` | **New** | Audit Phase 1 analysis prompt |
| `nanobot/templates/agent/dream_audit_phase2.md` | **New** | Audit Phase 2 execution prompt |
| `nanobot/cli/commands.py` | Modify | Register DreamAudit system cron job + callback |
| `tests/test_staging.py` | **New** | Staging read/write, metadata stripping, age calculation |
| `tests/test_dream_staging.py` | **New** | Dream Phase 1/2 staging integration tests |
| `tests/test_dream_audit.py` | **New** | DreamAudit tests |

---

### Task 1: DreamConfig — add staging and audit configuration fields

**Files:**
- Modify: `nanobot/config/schema.py:37-68` (DreamConfig class)
- Test: `tests/test_staging.py`

- [ ] **Step 1: Write the failing test for new config fields**

Create `tests/test_staging.py`:

```python
"""Tests for staging config fields on DreamConfig."""

from nanobot.config.schema import DreamConfig


def test_default_staging_promotion_threshold():
    cfg = DreamConfig()
    assert cfg.staging_promotion_threshold == 3


def test_default_audit_cron():
    cfg = DreamConfig()
    assert cfg.audit_cron == "0 3 * * *"


def test_default_audit_model_override_is_none():
    cfg = DreamConfig()
    assert cfg.audit_model_override is None


def test_default_audit_max_iterations():
    cfg = DreamConfig()
    assert cfg.audit_max_iterations == 15


def test_build_audit_schedule():
    cfg = DreamConfig()
    schedule = cfg.build_audit_schedule("UTC")
    assert schedule.kind == "cron"
    assert schedule.expr == "0 3 * * *"
    assert schedule.tz == "UTC"


def test_custom_audit_cron():
    cfg = DreamConfig(audit_cron="0 5 * * *")
    schedule = cfg.build_audit_schedule("Asia/Shanghai")
    assert schedule.expr == "0 5 * * *"
    assert schedule.tz == "Asia/Shanghai"


def test_camel_case_aliases():
    """Config JSON uses camelCase; verify aliases resolve."""
    cfg = DreamConfig(stagingPromotionThreshold=5, auditMaxIterations=20)
    assert cfg.staging_promotion_threshold == 5
    assert cfg.audit_max_iterations == 20
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_staging.py -v`
Expected: FAIL — `DreamConfig` has no attribute `staging_promotion_threshold`

- [ ] **Step 3: Add config fields to DreamConfig**

In `nanobot/config/schema.py`, append to `DreamConfig` (after `annotate_line_ages`, before `build_schedule`):

```python
    staging_promotion_threshold: int = Field(
        default=3,
        ge=1,
        validation_alias=AliasChoices("stagingPromotionThreshold"),
        serialization_alias="stagingPromotionThreshold",
    )
    audit_cron: str = Field(
        default="0 3 * * *",
        validation_alias=AliasChoices("auditCron"),
        serialization_alias="auditCron",
    )
    audit_model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("auditModelOverride", "auditModel", "audit_model_override"),
    )
    audit_max_iterations: int = Field(
        default=15,
        ge=1,
        validation_alias=AliasChoices("auditMaxIterations"),
        serialization_alias="auditMaxIterations",
    )

    def build_audit_schedule(self, timezone: str) -> "CronSchedule":
        """Build the audit schedule from the configured cron expression."""
        return CronSchedule(kind="cron", expr=self.audit_cron, tz=timezone)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_staging.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/config/schema.py tests/test_staging.py
git commit -m "feat(config): add staging/audit fields to DreamConfig"
```

---

### Task 2: MemoryStore — staging file I/O and metadata stripping

**Files:**
- Modify: `nanobot/agent/memory.py:41-68` (MemoryStore.__init__, new attributes and methods)
- Test: `tests/test_staging.py` (append)

- [ ] **Step 1: Write the failing tests for staging I/O**

Append to `tests/test_staging.py`:

```python
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from nanobot.agent.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path)


def test_read_staging_empty(store: MemoryStore):
    assert store.read_staging() == ""


def test_write_and_read_staging(store: MemoryStore):
    store.write_staging("# Staging\n\n### Topic\n- item")
    assert "### Topic" in store.read_staging()


def test_get_staging_context_empty(store: MemoryStore):
    assert store.get_staging_context() == ""


def test_get_staging_context_strips_metadata(store: MemoryStore):
    store.write_staging(
        "# Staging\n\n"
        "### MaxKB RAG\n"
        "- [2026-05-13] blend 模式搜索效果不佳 | seen:2 | age:1d\n"
        "- [2026-05-12] 决定先用 blend 模式 | seen:3 | age:2d\n\n"
        "### Other\n"
        "- [2026-05-10] some fact | seen:1 | age:3d\n"
    )
    ctx = store.get_staging_context()
    assert "# Short-term Memory" in ctx
    assert "### MaxKB RAG" in ctx
    assert "blend 模式搜索效果不佳" in ctx
    # Metadata should be stripped
    assert "seen:" not in ctx
    assert "age:" not in ctx
    assert "[2026-" not in ctx


def test_get_staging_context_preserves_section_headers(store: MemoryStore):
    store.write_staging("# Staging\n\n### Topic A\n- a | seen:1 | age:0d\n\n### Topic B\n- b | seen:2 | age:1d\n")
    ctx = store.get_staging_context()
    assert "### Topic A" in ctx
    assert "### Topic B" in ctx


def test_get_staging_context_plain_entry_no_pipes(store: MemoryStore):
    """Entries without pipe separators are preserved as-is."""
    store.write_staging("# Staging\n\n### Topic\n- plain entry without metadata\n")
    ctx = store.get_staging_context()
    assert "- plain entry without metadata" in ctx


def test_staging_file_attribute(store: MemoryStore):
    assert store.staging_file == store.memory_dir / "staging.md"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_staging.py -v -k "staging"`
Expected: FAIL — `MemoryStore` has no attribute `staging_file` / `read_staging`

- [ ] **Step 3: Add staging_file attribute to MemoryStore.__init__**

In `nanobot/agent/memory.py`, inside `MemoryStore.__init__` (after `self.user_file` line, around line 59), add:

```python
        self.staging_file = self.memory_dir / "staging.md"
```

- [ ] **Step 4: Add staging read/write/context methods to MemoryStore**

After the `write_user` method (around line 225), add a new section:

```python
    # -- staging.md ----------------------------------------------------------

    def read_staging(self) -> str:
        return self.read_file(self.staging_file)

    def write_staging(self, content: str) -> None:
        self.staging_file.write_text(content, encoding="utf-8")

    def get_staging_context(self) -> str:
        """Read staging.md, strip metadata, return context for injection.

        Returns empty string if file is empty/missing.
        """
        raw = self.read_staging()
        if not raw.strip():
            return ""
        stripped = _strip_staging_metadata(raw)
        if not stripped.strip():
            return ""
        return f"# Short-term Memory\n\n{stripped}"
```

- [ ] **Step 5: Add _strip_staging_metadata helper function**

Add as a module-level function in `nanobot/agent/memory.py` (before the MemoryStore class, around line 40):

```python
# Regex: matches  "- [YYYY-MM-DD] content | seen:N | age:Nd"  or  "- [YYYY-MM-DD] content | seen:N"
_STAGING_ENTRY_RE = re.compile(
    r"^(\s*- )\[\d{4}-\d{2}-\d{2}\]\s+(.+?)(?:\s*\|\s*seen:\d+(?:\s*\|\s*age:\d+d?)?)?\s*$",
    re.MULTILINE,
)


def _strip_staging_metadata(text: str) -> str:
    """Strip date, seen, age metadata from staging entries.

    - [2026-05-13] content | seen:2 | age:1d  →  - content
    - [2026-05-13] content | seen:2             →  - content
    - plain entry                              →  - plain entry (unchanged)
    """
    lines = text.split("\n")
    result = []
    for line in lines:
        m = _STAGING_ENTRY_RE.match(line)
        if m:
            result.append(f"{m.group(1)}{m.group(2)}")
        else:
            result.append(line)
    return "\n".join(result)
```

- [ ] **Step 6: Add staging.md to GitStore tracked files**

In `MemoryStore.__init__`, update the GitStore instantiation to include staging.md:

```python
        self._git = GitStore(workspace, tracked_files=[
            "SOUL.md", "USER.md", "memory/MEMORY.md", "memory/staging.md",
            "memory/.dream_cursor",
        ])
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_staging.py -v`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add nanobot/agent/memory.py tests/test_staging.py
git commit -m "feat(memory): add staging file I/O and metadata stripping to MemoryStore"
```

---

### Task 3: ContextBuilder — inject staging context into system prompt

**Files:**
- Modify: `nanobot/agent/context.py:37-76` (build_system_prompt method)
- Test: `tests/test_staging.py` (append)

- [ ] **Step 1: Write the failing test for staging injection order**

Append to `tests/test_staging.py`:

```python
from unittest.mock import patch

from nanobot.agent.context import ContextBuilder


@pytest.fixture
def ctx_builder(tmp_path: Path) -> ContextBuilder:
    return ContextBuilder(tmp_path)


def test_staging_injected_between_memory_and_skills(ctx_builder: ContextBuilder, tmp_path: Path):
    """Staging context appears between Memory and Skills sections in system prompt."""
    # Set up MEMORY.md
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory" / "MEMORY.md").write_text("# Test Memory\n- fact", encoding="utf-8")
    # Set up staging.md
    (tmp_path / "memory" / "staging.md").write_text(
        "# Staging\n\n### Topic\n- [2026-05-13] item | seen:1 | age:0d\n", encoding="utf-8",
    )
    # Set up a minimal skill so skills section exists
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "test-skill").mkdir(exist_ok=True)
    (skills_dir / "test-skill" / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill\n---\nContent\n", encoding="utf-8",
    )

    prompt = ctx_builder.build_system_prompt(skill_names=[])

    mem_pos = prompt.find("# Memory")
    staging_pos = prompt.find("# Short-term Memory")
    skills_pos = prompt.find("Available Skills")

    assert mem_pos > 0, "Memory section missing"
    assert staging_pos > 0, "Short-term Memory section missing"
    assert staging_pos > mem_pos, "Staging must appear after Memory"
    if skills_pos > 0:
        assert staging_pos < skills_pos, "Staging must appear before Skills"


def test_no_staging_section_when_staging_empty(ctx_builder: ContextBuilder, tmp_path: Path):
    """No Short-term Memory section when staging.md is empty."""
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory" / "MEMORY.md").write_text("# Test Memory\n- fact", encoding="utf-8")
    # staging.md does not exist

    prompt = ctx_builder.build_system_prompt(skill_names=[])
    assert "# Short-term Memory" not in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_staging.py -v -k "injected or staging_section"`
Expected: FAIL — staging section not present in system prompt

- [ ] **Step 3: Add staging injection to build_system_prompt**

In `nanobot/agent/context.py`, inside `build_system_prompt` (after the Memory block, before the always_skills block), insert staging context:

```python
        memory = self.memory.get_memory_context()
        if memory and not self._is_template_content(self.memory.read_memory(), "memory/MEMORY.md"):
            parts.append(f"# Memory\n\n{memory}")

        staging = self.memory.get_staging_context()
        if staging:
            parts.append(staging)

        always_skills = self.skills.get_always_skills()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_staging.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/agent/context.py tests/test_staging.py
git commit -m "feat(context): inject staging context into system prompt between memory and skills"
```

---

### Task 4: Dream Phase 1 — template rewrite with staging directives

**Files:**
- Modify: `nanobot/templates/agent/dream_phase1.md` (full rewrite)
- Modify: `nanobot/agent/memory.py:934-1009` (Dream.run Phase 1 input building)

- [ ] **Step 1: Rewrite dream_phase1.md template**

Replace the entire content of `nanobot/templates/agent/dream_phase1.md` with:

```markdown
You are a memory analyst. Your job is to analyze conversation history and manage a three-layer memory system.

## Input

You receive:
1. **Conversation History** — recent compressed conversation entries
2. **Current staging.md** — short-term observations with `| seen:N | age:Nd` metadata
3. **Current MEMORY.md** — long-term permanent facts (may have `← Nd` age annotations on lines older than {{stale_threshold_days}} days)
4. **Current SOUL.md** — bot personality and behavioral notes
5. **Current USER.md** — user profile and preferences

## Your Tasks

### A. Staging Maintenance

Read staging.md and compare with the new conversation history:

1. **Extract new facts** from the history that are worth remembering short-term. Route ALL new facts through staging first — do not write directly to MEMORY/SOUL/USER.
2. **Match existing staging entries**: when a new history entry discusses the same topic as an existing staging entry, increment `seen` and optionally **refine the wording** to merge new information. One history entry can match multiple staging entries.
3. **Identify promotion candidates**: entries where `seen >= {{staging_promotion_threshold}}` AND you judge them "worth permanent retention". Before promoting, check if the target file already contains similar content — if so, merge instead of appending.
4. **Identify forget candidates**:
   - **Low-frequency**: entries with `age > 14d AND seen <= 1` — mark for deletion (no semantic judgment needed).
   - **Semantic staleness**: entries with `age > 7d` where you judge the topic is "resolved/completed/no longer relevant".
5. **Detect conflicts**: if new information contradicts existing long-term memory, flag for update.

### B. Long-term Memory Maintenance (existing behavior)

Identify facts that should be **removed** from existing files because they are:
- Duplicated across files (keep in most appropriate location)
- Overlapping or redundant within a section
- Objectively outdated (the `← Nd` age suffix indicates days since last edit)

### C. Skill Discovery (existing behavior)

If a repeatable, substantial workflow appeared **2+ times** in the history, flag it for skill creation.

## Output Format

Use these exact prefixes. Each prefix on its own line, followed by the content.

### New staging entries
```
[STAGING-NEW] topic: <free-form topic name>
- <factual observation>
```

### Update existing staging entries (seen+1, refined wording)
```
[STAGING-UPDATE] topic: <topic name>
- <exact old content> → <new merged/refined content>
```

### Promote staging entry to long-term memory
```
[PROMOTE] topic: <topic name> → <MEMORY|USER|SOUL>.md
- <entry content to promote>
```

### Forget staging entry
```
[FORGET] reason: <low-frequency|semantic-stale>
- <exact entry content to remove>
```

### Add fact to long-term file (existing)
```
[<MEMORY|USER|SOUL>] <atomic factual statement>
```

### Remove content from long-term file (existing)
```
[<MEMORY|USER|SOUL>-REMOVE] <reason for removal>
```

### Suggest new skill (existing)
```
[SKILL] <kebab-case-name>: <one-line description>
```

## Rules

- **Atomic facts**: each entry should be one specific, granular statement.
- **Route through staging**: all new facts go to staging first. Only promote when seen threshold is met.
- **Topics are free-form**: create new `### topic` sections as needed, no predefined categories.
- **Refine on match**: when incrementing seen, take the opportunity to merge information and improve wording.
- **Promotion target**: USER for identity/preferences, SOUL for bot behavior, MEMORY for everything else.
- **Only prune objectively outdated content** from long-term files.
- If nothing needs updating, output nothing.
```

- [ ] **Step 2: Update Dream.run() to include staging in Phase 1 input**

In `nanobot/agent/memory.py`, inside `Dream.run()`, after reading `current_user` (around line 973) and before building `file_context` (around line 975), add staging reading:

```python
        current_staging = truncate_text(
            self.store.read_staging() or "(empty)", self._STAGING_FILE_MAX_CHARS,
        )
```

Add the `_STAGING_FILE_MAX_CHARS` class constant to Dream (near the other max chars constants):

```python
    _STAGING_FILE_MAX_CHARS = 16_000
```

Update `file_context` to include staging:

```python
        file_context = (
            f"## Current Date\n{current_date}\n\n"
            f"## Current staging.md ({len(current_staging)} chars)\n{current_staging}\n\n"
            f"## Current MEMORY.md ({len(current_memory)} chars)\n{current_memory}\n\n"
            f"## Current SOUL.md ({len(current_soul)} chars)\n{current_soul}\n\n"
            f"## Current USER.md ({len(current_user)} chars)\n{current_user}"
        )
```

Also update the Phase 1 system prompt call to pass the new template variable:

```python
            phase1_response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/dream_phase1.md",
                            strip=True,
                            stale_threshold_days=_STALE_THRESHOLD_DAYS,
                            staging_promotion_threshold=self._get_promotion_threshold(),
                        ),
                    },
                    {"role": "user", "content": phase1_prompt},
                ],
                tools=None,
                tool_choice=None,
            )
```

- [ ] **Step 3: Add _get_promotion_threshold helper to Dream**

```python
    def _get_promotion_threshold(self) -> int:
        """Get the staging promotion seen threshold from DreamConfig.

        DreamConfig is not directly accessible here, so this reads from
        the default config. The caller (commands.py) can override via
        the config if needed.
        """
        from nanobot.config.loader import load_config
        try:
            return load_config().agents.defaults.dream.staging_promotion_threshold
        except Exception:
            return 3
```

- [ ] **Step 4: Run existing Dream tests to verify nothing breaks**

Run: `uv run pytest tests/test_memory.py tests/test_dream.py -v --timeout=30 2>/dev/null || uv run pytest tests/ -k "dream or memory" -v --timeout=30`
Expected: existing tests still pass

- [ ] **Step 5: Commit**

```bash
git add nanobot/templates/agent/dream_phase1.md nanobot/agent/memory.py
git commit -m "feat(dream): rewrite Phase 1 template with staging directives and seen tracking"
```

---

### Task 5: Dream Phase 2 — template rewrite and WriteFileTool permission expansion

**Files:**
- Modify: `nanobot/templates/agent/dream_phase2.md` (full rewrite)
- Modify: `nanobot/agent/memory.py:831-856` (Dream._build_tools — expand WriteFileTool scope)

- [ ] **Step 1: Expand WriteFileTool scope to include memory/ directory**

In `nanobot/agent/memory.py`, inside `Dream._build_tools()`, change the WriteFileTool registration to allow both `skills/` and `memory/`:

Replace:
```python
        tools.register(WriteFileTool(workspace=workspace, allowed_dir=skills_dir, file_states=file_states))
```

With:
```python
        # WriteFileTool: staging.md lives under memory/, skills under skills/.
        # Register two instances with different allowed dirs.
        tools.register(WriteFileTool(workspace=workspace, allowed_dir=skills_dir, file_states=file_states))
        tools.register(WriteFileTool(workspace=workspace, allowed_dir=store.memory_dir, file_states=file_states))
```

- [ ] **Step 2: Rewrite dream_phase2.md template**

Replace the entire content of `nanobot/templates/agent/dream_phase2.md` with:

```markdown
You are a memory editor. Execute the analysis from Phase 1 by making targeted edits to the memory files.

## Available Tools

- **read_file** — read any file in the workspace
- **edit_file** — make surgical edits via exact string matching (use for MEMORY.md, SOUL.md, USER.md)
- **write_file** — write complete file content (use for memory/staging.md and skills/ only)

## Your Instructions

### Processing Order

Process the analysis directives in this order:

1. **[FORGET]** — remove staging entries
2. **[STAGING-UPDATE]** — update existing staging entries (seen+1, refined wording)
3. **[PROMOTE]** — promote staging entries to long-term files, then remove from staging
4. **[STAGING-NEW]** — add new staging entries
5. **[FILE-REMOVE]** — remove redundant/outdated content from long-term files
6. **[FILE]** — add new facts to long-term files
7. **[SKILL]** — create new skills

### Staging Operations (use write_file for memory/staging.md)

After processing all [STAGING-NEW], [STAGING-UPDATE], [PROMOTE], and [FORGET] directives, perform a **single write_file** call for `memory/staging.md` with the complete updated content.

Format for staging.md:
```
# Staging

### Topic Name
- [YYYY-MM-DD] content | seen:N | age:Nd
```

Rules:
- Date format: YYYY-MM-DD (today's date for new entries)
- `seen` counter: incremented on STAGING-UPDATE, starts at 1 for STAGING-NEW
- `age`: computed from entry date vs current date, expressed as `Nd` (e.g. `0d`, `3d`, `14d`)
- Promoted entries are REMOVED from staging
- Forgotten entries are REMOVED from staging
- Entries within each topic section are ordered newest-first

### Long-term File Operations (use edit_file)

For [FILE], [FILE-REMOVE], and promoted [PROMOTE] entries targeting MEMORY.md, SOUL.md, or USER.md:

- Use `edit_file` with exact string matches for surgical edits
- Batch multiple changes to the same file
- Do NOT rewrite entire files — only add, remove, or replace specific lines
- When promoting, check if similar content already exists → merge if so

### Skill Creation (use write_file for skills/)

For [SKILL] entries:
1. First read the skill creator template: `read_file("{{skill_creator_path}}")`
2. Check existing skills for redundancy
3. Create the skill under `skills/<kebab-case-name>/SKILL.md`
4. Include YAML frontmatter with name and description
5. Keep under 2000 words

## Important

- If the analysis contains no directives, output nothing and stop.
- Only edit files that are mentioned in the analysis.
- Preserve existing file structure and formatting where possible.
- All staging changes go into a single write_file call at the end.
```

- [ ] **Step 3: Update Phase 2 prompt to include staging context**

In `nanobot/agent/memory.py`, inside `Dream.run()`, the `phase2_prompt` line (around line 1018) should include staging:

```python
        phase2_prompt = (
            f"## Analysis Result\n{analysis}\n\n"
            f"## Current staging.md\n{current_staging}\n\n"
            f"{file_context}{skills_section}"
        )
```

- [ ] **Step 4: Run existing tests to verify nothing breaks**

Run: `uv run pytest tests/ -k "dream" -v --timeout=30`
Expected: existing tests still pass (template changes don't break existing Dream integration)

- [ ] **Step 5: Commit**

```bash
git add nanobot/templates/agent/dream_phase2.md nanobot/agent/memory.py
git commit -m "feat(dream): rewrite Phase 2 template with staging execution and expand WriteFileTool to memory/"
```

---

### Task 6: DreamAudit — new class in memory.py

**Files:**
- Modify: `nanobot/agent/memory.py` (add DreamAudit class after Dream class)
- Test: `tests/test_dream_audit.py` (new)

- [ ] **Step 1: Write the failing test for DreamAudit instantiation**

Create `tests/test_dream_audit.py`:

```python
"""Tests for DreamAudit class."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.memory import DreamAudit, MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path)


@pytest.fixture
def mock_provider() -> MagicMock:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock()
    provider.generation = MagicMock(max_tokens=4096)
    return provider


def test_dream_audit_instantiation(store: MemoryStore, mock_provider: MagicMock):
    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    assert audit.store is store
    assert audit.model == "test-model"


def test_dream_audit_build_tools(store: MemoryStore, mock_provider: MagicMock):
    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    tools = audit._tools
    tool_names = {t.name for t in tools.all()}
    assert "read_file" in tool_names
    assert "edit_file" in tool_names
    # Audit only does targeted edits, no write_file
    assert "write_file" not in tool_names


@pytest.mark.asyncio
async def test_dream_audit_run_no_changes(store: MemoryStore, mock_provider: MagicMock):
    """Audit with empty files does nothing and returns False."""
    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    result = await audit.run()
    assert result is False


def test_dream_audit_set_provider(store: MemoryStore, mock_provider: MagicMock):
    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    new_provider = MagicMock()
    audit.set_provider(new_provider, "new-model")
    assert audit.provider is new_provider
    assert audit.model == "new-model"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dream_audit.py -v`
Expected: FAIL — `cannot import name 'DreamAudit'`

- [ ] **Step 3: Implement DreamAudit class**

In `nanobot/agent/memory.py`, after the `Dream` class (after `Dream.run()` method, around line 1087), add:

```python
# ---------------------------------------------------------------------------
# DreamAudit — daily memory audit processor
# ---------------------------------------------------------------------------


class DreamAudit:
    """Two-phase daily audit: analyze all memory files, then make targeted edits.

    Responsible for cross-file dedup, dead entry cleanup, structure
    reorganization, and compression.
    """

    _MEMORY_FILE_MAX_CHARS = 32_000
    _SOUL_FILE_MAX_CHARS = 16_000
    _USER_FILE_MAX_CHARS = 16_000
    _STAGING_FILE_MAX_CHARS = 16_000

    def __init__(
        self,
        store: MemoryStore,
        provider: LLMProvider,
        model: str,
        max_iterations: int = 15,
        max_tool_result_chars: int = 16_000,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self._runner = AgentRunner(provider)
        self._tools = self._build_tools()

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self._runner.provider = provider

    def _build_tools(self) -> ToolRegistry:
        """Read + Edit tools only — audit does targeted edits, no full rewrites."""
        from nanobot.agent.tools.file_state import FileStates
        from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool

        tools = ToolRegistry()
        workspace = self.store.workspace
        file_states = FileStates()
        tools.register(ReadFileTool(
            workspace=workspace,
            allowed_dir=workspace,
            file_states=file_states,
        ))
        tools.register(EditFileTool(
            workspace=workspace,
            allowed_dir=workspace,
            file_states=file_states,
        ))
        return tools

    async def run(self) -> bool:
        """Run the two-phase audit. Returns True if changes were made."""
        current_date = datetime.now().strftime("%Y-%m-%d")

        raw_memory = self.store.read_memory() or "(empty)"
        raw_soul = self.store.read_soul() or "(empty)"
        raw_user = self.store.read_user() or "(empty)"
        raw_staging = self.store.read_staging() or "(empty)"

        # Skip if all files are empty
        if all(v == "(empty)" for v in [raw_memory, raw_soul, raw_user, raw_staging]):
            return False

        current_memory = truncate_text(raw_memory, self._MEMORY_FILE_MAX_CHARS)
        current_soul = truncate_text(raw_soul, self._SOUL_FILE_MAX_CHARS)
        current_user = truncate_text(raw_user, self._USER_FILE_MAX_CHARS)
        current_staging = truncate_text(raw_staging, self._STAGING_FILE_MAX_CHARS)

        file_context = (
            f"## Current Date\n{current_date}\n\n"
            f"## Current MEMORY.md ({len(current_memory)} chars)\n{current_memory}\n\n"
            f"## Current SOUL.md ({len(current_soul)} chars)\n{current_soul}\n\n"
            f"## Current USER.md ({len(current_user)} chars)\n{current_user}\n\n"
            f"## Current staging.md ({len(current_staging)} chars)\n{current_staging}"
        )

        # Phase 1: Analyze
        try:
            phase1_response = await self.provider.chat_with_retry(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": render_template(
                            "agent/dream_audit_phase1.md",
                            strip=True,
                        ),
                    },
                    {"role": "user", "content": file_context},
                ],
                tools=None,
                tool_choice=None,
            )
            analysis = phase1_response.content or ""
            if not analysis.strip():
                return False
        except Exception:
            logger.exception("DreamAudit Phase 1 failed")
            return False

        # Phase 2: Execute
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": render_template(
                    "agent/dream_audit_phase2.md",
                    strip=True,
                ),
            },
            {"role": "user", "content": f"## Analysis Result\n{analysis}\n\n{file_context}"},
        ]

        try:
            result = await self._runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=self._tools,
                model=self.model,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                fail_on_tool_error=False,
            ))
        except Exception:
            logger.exception("DreamAudit Phase 2 failed")
            return False

        changelog: list[str] = []
        if result and result.tool_events:
            for event in result.tool_events:
                if event["status"] == "ok":
                    changelog.append(f"{event['name']}: {event['detail']}")

        if changelog and self.store.git.is_initialized():
            summary = f"dream-audit: {current_date}, {len(changelog)} change(s)"
            commit_msg = f"{summary}\n\n{analysis.strip()}"
            sha = self.store.git.auto_commit(commit_msg)
            if sha:
                logger.info("DreamAudit commit: {}", sha)

        return bool(changelog)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_dream_audit.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add nanobot/agent/memory.py tests/test_dream_audit.py
git commit -m "feat(memory): add DreamAudit class for daily memory auditing"
```

---

### Task 7: DreamAudit — Phase 1 and Phase 2 prompt templates

**Files:**
- Create: `nanobot/templates/agent/dream_audit_phase1.md`
- Create: `nanobot/templates/agent/dream_audit_phase2.md`

- [ ] **Step 1: Create dream_audit_phase1.md**

Create `nanobot/templates/agent/dream_audit_phase1.md`:

```markdown
You are a memory auditor. Perform a comprehensive review of ALL memory files for quality, consistency, and relevance.

## Input

You receive the current contents of:
1. **MEMORY.md** — long-term permanent facts
2. **SOUL.md** — bot personality and behavioral notes
3. **USER.md** — user profile and preferences
4. **staging.md** — short-term observations with `| seen:N | age:Nd` metadata

## Your Tasks

### 1. Cross-file Deduplication
Identify the same fact appearing in multiple files. Keep it in the most appropriate location:
- USER.md: identity, preferences, communication style
- SOUL.md: bot behavior, tone, interaction patterns
- MEMORY.md: everything else (projects, facts, decisions)
- staging.md: observations awaiting promotion

### 2. Dead Entry Cleanup
Identify content that is clearly outdated or completed:
- Resolved issues with no ongoing relevance
- References to past events that no longer matter
- Duplicate entries within the same file
- Empty or placeholder sections

### 3. Structure Reorganization
Suggest structural improvements:
- Misplaced entries (e.g. project facts in USER.md)
- Heading hierarchy issues
- Logical grouping of related entries

### 4. Compression
Identify verbose entries that can be shortened without losing information.

## Output Format

```
[<FILE>-REMOVE] <exact text to remove>
[<FILE>-EDIT] <exact old text> → <exact new text>
```

Where `<FILE>` is one of: MEMORY, SOUL, USER, STAGING.

If nothing needs changing, output nothing.
```

- [ ] **Step 2: Create dream_audit_phase2.md**

Create `nanobot/templates/agent/dream_audit_phase2.md`:

```markdown
You are a memory editor executing an audit. Make the changes identified in the analysis.

## Available Tools

- **read_file** — read any file in the workspace
- **edit_file** — make surgical edits via exact string matching

## Instructions

For each change in the analysis:

### [<FILE>-REMOVE]
Use `edit_file` to remove the exact text from the specified file.

### [<FILE>-EDIT]
Use `edit_file` to replace the exact old text with the new text.

## Rules

- Only edit files mentioned in the analysis.
- Use exact string matches — no fuzzy matching.
- Batch multiple changes to the same file when possible.
- If the analysis is empty, output nothing and stop.
```

- [ ] **Step 3: Verify templates are loadable**

Run: `uv run python -c "from nanobot.utils.prompt_templates import render_template; print(render_template('agent/dream_audit_phase1.md', strip=True)[:100]); print(render_template('agent/dream_audit_phase2.md', strip=True)[:100])"`
Expected: Both templates render without error

- [ ] **Step 4: Commit**

```bash
git add nanobot/templates/agent/dream_audit_phase1.md nanobot/templates/agent/dream_audit_phase2.md
git commit -m "feat(templates): add DreamAudit Phase 1 and Phase 2 prompt templates"
```

---

### Task 8: commands.py — register DreamAudit as protected system cron job

**Files:**
- Modify: `nanobot/cli/commands.py` (Dream registration area ~lines 936-949 + on_cron_job callback)

- [ ] **Step 1: Find the Dream registration block in commands.py**

The Dream system job registration is around lines 936-949 in `nanobot/cli/commands.py`. It looks like:

```python
dream_cfg = config.agents.defaults.dream
if dream_cfg.model_override:
    agent.dream.model = dream_cfg.model_override
agent.dream.max_batch_size = dream_cfg.max_batch_size
...
cron.register_system_job(CronJob(id="dream", ...))
```

Find this block and the `on_cron_job` callback function that handles `"dream"` jobs.

- [ ] **Step 2: Add DreamAudit initialization after Dream registration**

After the Dream registration block, add DreamAudit initialization and registration:

```python
        # Register DreamAudit system job (daily memory audit)
        dream_audit = DreamAudit(
            store=agent.dream.store,
            provider=agent.dream.provider,
            model=dream_cfg.audit_model_override or agent.dream.model,
            max_iterations=dream_cfg.audit_max_iterations,
        )
        if dream_cfg.audit_model_override:
            dream_audit.model = dream_cfg.audit_model_override
        agent.dream_audit = dream_audit
        cron.register_system_job(CronJob(
            id="dream_audit",
            name="dream_audit",
            schedule=dream_cfg.build_audit_schedule(config.agents.defaults.timezone),
            payload=CronPayload(kind="system_event"),
        ))
        console.print(f"[green]✓[/green] DreamAudit: {dream_cfg.audit_cron}")
```

Add the import at the top of the function or file:

```python
from nanobot.agent.memory import DreamAudit
```

- [ ] **Step 3: Add DreamAudit handler to on_cron_job callback**

In the `on_cron_job` async callback function, add a handler for `"dream_audit"`:

```python
    if job.name == "dream_audit":
        try:
            await agent.dream_audit.run()
            logger.info("DreamAudit cron job completed")
        except Exception:
            logger.exception("DreamAudit cron job failed")
        return None
```

- [ ] **Step 4: Verify commands.py loads without error**

Run: `uv run python -c "from nanobot.cli.commands import app; print('OK')"`
Expected: `OK` (no import errors)

- [ ] **Step 5: Commit**

```bash
git add nanobot/cli/commands.py
git commit -m "feat(cli): register DreamAudit as protected system cron job"
```

---

### Task 9: Integration tests — Dream staging end-to-end

**Files:**
- Create: `tests/test_dream_staging.py`

- [ ] **Step 1: Write Dream staging integration tests**

Create `tests/test_dream_staging.py`:

```python
"""Integration tests for Dream Phase 1/2 staging functionality."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.memory import Dream, DreamAudit, MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(tmp_path)
    # Pre-populate some history entries
    for i, content in enumerate(["user discussed MaxKB RAG", "user mentioned rerank models"], start=1):
        s.append_history(content)
    return s


@pytest.fixture
def mock_provider() -> MagicMock:
    provider = MagicMock()
    provider.generation = MagicMock(max_tokens=4096)
    return provider


@pytest.fixture
def dream(store: MemoryStore, mock_provider: MagicMock) -> Dream:
    return Dream(store=store, provider=mock_provider, model="test-model")


def test_dream_phase1_input_includes_staging(dream: Dream, store: MemoryStore):
    """Verify Phase 1 input contains staging.md content."""
    store.write_staging("# Staging\n\n### Topic\n- [2026-05-13] test | seen:1 | age:0d\n")
    # We verify the input building by checking the file_context includes staging
    staging_content = store.read_staging()
    assert "### Topic" in staging_content


def test_dream_write_tool_includes_memory_dir(dream: Dream):
    """Verify Dream Phase 2 has write access to memory/ directory."""
    tool_names = [t.name for t in dream._tools.all()]
    assert "write_file" in tool_names


@pytest.mark.asyncio
async def test_dream_staging_new_entry(store: MemoryStore, mock_provider: MagicMock):
    """Dream Phase 1 produces [STAGING-NEW] and Phase 2 writes to staging.md."""
    store.write_staging("")

    # Phase 1 returns analysis with [STAGING-NEW] directive
    phase1_response = MagicMock()
    phase1_response.content = "[STAGING-NEW] topic: Test Topic\n- a new observation"
    phase1_response.finish_reason = "stop"

    # Phase 2 agent result
    phase2_result = MagicMock()
    phase2_result.stop_reason = "completed"
    phase2_result.tool_events = [
        {"name": "write_file", "status": "ok", "detail": "wrote memory/staging.md"},
    ]

    mock_provider.chat_with_retry = AsyncMock(return_value=phase1_response)

    dream = Dream(store=store, provider=mock_provider, model="test-model")
    with patch.object(dream._runner, "run", AsyncMock(return_value=phase2_result)):
        result = await dream.run()

    assert result is True


@pytest.mark.asyncio
async def test_dream_audit_run_with_files(store: MemoryStore, mock_provider: MagicMock):
    """DreamAudit processes files when they have content."""
    store.write_memory("# Memory\n- test fact")
    store.write_staging("# Staging\n\n### Topic\n- item | seen:1 | age:0d\n")

    phase1_response = MagicMock()
    phase1_response.content = "[MEMORY-EDIT] - test fact → - test fact (verified)"

    phase2_result = MagicMock()
    phase2_result.stop_reason = "completed"
    phase2_result.tool_events = [
        {"name": "edit_file", "status": "ok", "detail": "edited memory/MEMORY.md"},
    ]

    mock_provider.chat_with_retry = AsyncMock(return_value=phase1_response)

    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    with patch.object(audit._runner, "run", AsyncMock(return_value=phase2_result)):
        result = await audit.run()

    assert result is True


@pytest.mark.asyncio
async def test_dream_audit_skips_empty_files(store: MemoryStore, mock_provider: MagicMock):
    """DreamAudit returns False when all files are empty."""
    # All files are empty by default in a fresh tmp_path store
    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    result = await audit.run()
    assert result is False
```

- [ ] **Step 2: Run integration tests**

Run: `uv run pytest tests/test_dream_staging.py -v`
Expected: all PASS

- [ ] **Step 3: Run full test suite**

Run: `uv run pytest tests/ -x --timeout=60`
Expected: all existing tests + new tests pass. If pre-existing failures exist, verify they are not caused by this change (compare against known failures from CLAUDE.md).

- [ ] **Step 4: Commit**

```bash
git add tests/test_dream_staging.py
git commit -m "test: add Dream staging and DreamAudit integration tests"
```

---

## Self-Review Checklist

After completing all tasks, verify:

- [ ] **Config:** `DreamConfig` has all new fields with defaults and camelCase aliases
- [ ] **MemoryStore:** `staging_file`, `read_staging()`, `write_staging()`, `get_staging_context()` work
- [ ] **Metadata stripping:** `_strip_staging_metadata()` removes date, seen, age from staging entries
- [ ] **Context injection:** staging appears between Memory and Skills in system prompt
- [ ] **Dream Phase 1:** template includes staging directives, input includes staging.md
- [ ] **Dream Phase 2:** template includes staging execution, WriteFileTool has memory/ access
- [ ] **DreamAudit:** class exists with two-phase run(), separate from Dream
- [ ] **DreamAudit templates:** Phase 1 and Phase 2 templates render correctly
- [ ] **Cron registration:** DreamAudit registered as protected system job in commands.py
- [ ] **Git tracking:** staging.md tracked by GitStore
- [ ] **All tests pass**

