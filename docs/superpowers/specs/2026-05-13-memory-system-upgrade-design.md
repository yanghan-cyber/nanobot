# Memory System Upgrade: Three-Layer Model — Design Spec

## Context

Nanobot's current memory system has a single long-term layer (MEMORY.md / SOUL.md / USER.md) and a transient Recent History layer. Dream processes history entries every 2h, directly writing facts into long-term files. There is no intermediate layer — information not selected for long-term storage is permanently lost.

This spec adds a **Staging** layer between Recent History and Long-term Memory. Staging holds structured short-term observations with frequency tracking (seen count) and age metadata. Facts graduate to long-term memory through a dual gate (frequency + semantic quality). Low-frequency or stale observations are automatically forgotten.

The existing Consolidator, autocompact, and history.jsonl pipeline are completely untouched.

## Architecture Overview

```
对话消息
  │
  ▼
Consolidator 压缩 → history.jsonl（不变）
  │
  ├─→ Recent History 注入上下文（不变，几小时寿命）
  │
  ▼
Dream 每 2h 触发（改造）
  │
  ├─ Phase 1（无工具，LLM 分析）
  │   ├─ 读取 staging.md + 新 history 条目
  │   ├─ 提取关键点 → [STAGING-NEW]
  │   ├─ 对比匹配 → [STAGING-UPDATE] seen+1, 优化表述
  │   ├─ 晋升判断 → [PROMOTE]（seen≥N + 语义确认）
  │   ├─ 遗忘判断 → [FORGET]（低频规则 + 语义过期）
  │   ├─ 冲突检测 → [FILE-REMOVE] + [FILE]（不变）
  │   └─ 现有 [FILE]/[FILE-REMOVE]/[SKILL] 功能不变
  │
  ▼
Phase 2（有工具，执行）
  ├─ EditFileTool: MEMORY.md / SOUL.md / USER.md
  ├─ WriteFileTool: memory/staging.md（全量重写）+ skills/
  ├─ 执行所有 [STAGING-*] / [PROMOTE] / [FORGET] 指令
  └─ 执行现有 [FILE]/[FILE-REMOVE]/[SKILL] 指令
  │
  ▼
DreamAudit（新增，每日独立 cron）
  ├─ 跨文件去重
  ├─ 死条目清理
  ├─ 结构重组
  └─ 精简压缩
```

### Three-Layer Model

```
┌─────────────────────────────────────────────────┐
│  Layer 1: Recent History（不变）                  │
│  来源：autocompact → history.jsonl                │
│  寿命：几小时（Dream 处理后消失）                   │
│  注入：# Recent History 区块（不变）               │
├─────────────────────────────────────────────────┤
│  Layer 2: Staging（新增）                         │
│  来源：Dream 从 history 提取的关键点               │
│  寿命：7-14 天                                    │
│  注入：# Short-term Memory 区块（剥离元数据）       │
├─────────────────────────────────────────────────┤
│  Layer 3: Long-term Memory（不变）                │
│  来源：Staging 晋升                                │
│  寿命：永久                                        │
│  注入：# Memory 区块（不变）                       │
└─────────────────────────────────────────────────┘
```

### System Prompt Injection Order

```
1. Identity (AGENTS.md)
2. SOUL.md
3. USER.md
4. TOOLS.md
5. Long-term Memory (MEMORY.md)        ← 不变
6. Short-term Memory (staging.md)      ← 新增
7. Active Skills / Available Skills
8. Recent History                      ← 不变
```

Arranged from most permanent to most transient.

## Files Changed

| File | Action | Description |
|------|--------|-------------|
| `nanobot/agent/memory.py` | Modify | MemoryStore: staging read/write; Dream: Phase 1/2 expansion; new DreamAudit class |
| `nanobot/agent/context.py` | Modify | New `get_staging_context()` method, inject into system prompt |
| `nanobot/config/schema.py` | Modify | DreamConfig: `staging_promotion_threshold`, `audit_cron`, `audit_model_override` |
| `nanobot/cli/commands.py` | Modify | Register DreamAudit as protected system cron job |
| `nanobot/templates/agent/dream_phase1.md` | Modify | Add staging maintenance, seen increment, promotion/forget judgments |
| `nanobot/templates/agent/dream_phase2.md` | Modify | Add staging write, promotion execution instructions |
| `nanobot/templates/agent/dream_audit_phase1.md` | **New** | Audit Phase 1 analysis template |
| `nanobot/templates/agent/dream_audit_phase2.md` | **New** | Audit Phase 2 execution template |
| `memory/staging.md` | **New** | Staging short-term memory file (starts empty) |
| `tests/test_staging.py` | **New** | Staging read/write, metadata stripping, age calculation |
| `tests/test_dream_staging.py` | **New** | Dream Phase 1/2 staging integration tests |
| `tests/test_dream_audit.py` | **New** | DreamAudit tests |

## Part 1: Staging Data Structure

### 1.1 File Format — `memory/staging.md`

Dream reads and writes this file. Contains full metadata.

```markdown
# Staging

### MaxKB RAG
- [2026-05-13] blend 模式搜索效果不佳，考虑加 rerank 模型 | seen:2 | age:1d
- [2026-05-12] 决定先用 blend 模式，后续再对比效果 | seen:3 | age:2d

### Dream 记忆系统
- [2026-05-13] 讨论了三层记忆模型 | seen:1 | age:0d
```

**Rules:**
- `### topic` — Dream freely induces topics, no predefined categories.
- Each entry: `- [date] content | seen:N | age:Nd`
- `age` is derived from the entry's date at read time. Phase 2 rewrites the file with current `age` values, but these are always recalculated from `[date]` — never trusted from a previous write.
- New entries append to the end of the matching topic section.
- No matching topic → Dream creates a new `###` section.

### 1.2 Context Injection Format

`ContextBuilder.get_staging_context()` reads `staging.md` and strips metadata for injection:

```markdown
# Short-term Memory

### MaxKB RAG
- blend 模式搜索效果不佳，考虑加 rerank 模型
- 决定先用 blend 模式，后续再对比效果

### Dream 记忆系统
- 讨论了三层记忆模型
```

**Stripped:** date, `seen:N`, `age:Nd` — irrelevant to the conversation agent, saves tokens.

### 1.3 Initialization

`memory/staging.md` starts as an empty file. No migration from existing MEMORY.md.

## Part 2: MemoryStore Staging Methods

### 2.1 New Methods on MemoryStore

```python
class MemoryStore:
    # Existing attributes:
    #   self.memory_dir = workspace / "memory"
    #   self.memory_file = self.memory_dir / "MEMORY.md"

    # New:
    staging_file: Path  # self.memory_dir / "staging.md"

    def read_staging(self) -> str:
        """Read staging.md content (empty string if missing)."""

    def write_staging(self, content: str) -> None:
        """Write staging.md (atomic write)."""

    def get_staging_context(self) -> str:
        """Read staging.md and strip metadata for context injection.
        Returns empty string if file is empty/missing.
        Format: '# Short-term Memory\\n\\n{stripped content}'
        """
```

### 2.2 Metadata Stripping Logic

`get_staging_context()` strips per-entry metadata using regex:

- Input: `- [2026-05-13] content | seen:2 | age:1d`
- Output: `- content`
- Section headers (`### topic`) preserved as-is.
- Entries without metadata pipe separators preserved as-is (backward compatible).

### 2.3 Age Calculation

Age is calculated at read time from the entry's date vs. current date:

```python
from datetime import datetime, date

def _compute_entry_age(entry_date_str: str) -> int:
    """Days between entry date and today."""
    entry_date = date.fromisoformat(entry_date_str)
    return (date.today() - entry_date).days
```

This runs each time staging.md is loaded for Dream input. The `age:Nd` suffix in the file is rewritten by Phase 2 (via WriteFileTool full rewrite).

## Part 3: Promotion and Forgetting Rules

### 3.1 Staging → Long-term Promotion

**Dual gate: frequency + semantic quality.**

```
Condition 1 (necessary): seen >= N  (N = DreamConfig.staging_promotion_threshold, default 3)
  AND
Condition 2 (sufficient): Dream Phase 1 LLM judges "worth permanent retention"
  → Promote to target file (MEMORY.md / USER.md / SOUL.md)
```

- Before promotion, check if target file already has similar content → merge/refine if so.
- Frequent but unimportant content is NOT promoted (LLM semantic filter).
- Important but insufficient-frequency content is NOT promoted (waits for more evidence).
- Promoted entries are REMOVED from staging.md.

### 3.2 Staging Forgetting

Two parallel mechanisms:

| Rule | Condition | Executor |
|------|-----------|----------|
| Low-frequency | `age > 14d AND seen <= 1` | Code rule, Phase 2 deletes directly |
| Semantic staleness | `age > 7d` | Phase 1 LLM judges "is this resolved/complete?" → Phase 2 deletes |

Low-frequency forgetting does not require LLM judgment — pure rule, saves tokens.
Semantic staleness requires Phase 1 attention but captures "project ended" scenarios.

### 3.3 Long-term Forgetting

| Scenario | Trigger | Timing |
|----------|---------|--------|
| Conflict/update | Phase 1 detects new info contradicts long-term memory | Every 2h Dream run |
| Lightweight merge | Redundant entries within same section | Every 2h Dream run |
| Full audit | Cross-file dedup, dead entry cleanup, restructuring | Daily DreamAudit cron |

### 3.4 Seen Increment Mechanism

- Phase 1 input includes staging.md current content alongside new history entries.
- Phase 1 compares each new history entry against existing staging entries.
- One history entry may match **multiple** staging entries — all matched entries get seen+1.
- On match, Phase 1 can **refine/merge** the matched entry's wording (accumulating information from multiple mentions).
- Phase 1 outputs `[STAGING-UPDATE]` / `[STAGING-NEW]` instructions for Phase 2.

## Part 4: Dream Phase 1/2 Expansion

### 4.1 Phase 1 Input Changes

Current Phase 1 input:
```
## Conversation History
{history_text}

## Current MEMORY.md
{current_memory}

## Current SOUL.md
{current_soul}

## Current USER.md
{current_user}
```

New Phase 1 input adds staging:
```
## Conversation History
{history_text}

## Current staging.md
{current_staging}

## Current MEMORY.md
{current_memory}

## Current SOUL.md
{current_soul}

## Current USER.md
{current_user}
```

### 4.2 Phase 1 Output Format

Phase 1 output extends the existing `[FILE]`/`[FILE-REMOVE]`/`[SKILL]` format with new staging directives:

```
[STAGING-NEW] topic: MaxKB RAG
- blend 模式搜索效果不佳，考虑加 rerank 模型

[STAGING-UPDATE] topic: MaxKB RAG
- 决定先用 blend 模式 → 决定先用 blend 模式，后续再对比效果（首选 rerank 评估中） | seen+1

[PROMOTE] topic: 基础设施 → MEMORY.md
- vLLM 占用约 8GB 显存，剩余不足以加载额外 GPU 模型

[FORGET] reason: low-frequency
- [2026-04-28] 某临时调试记录

[FILE] atomic fact about user preference          ← existing, unchanged
[FILE-REMOVE] reason for removal                   ← existing, unchanged
[SKILL] kebab-case-name: description               ← existing, unchanged
```

### 4.3 Phase 2 Tool Permissions

| Tool | Scope | Files |
|------|-------|-------|
| ReadFileTool | workspace | All files (unchanged) |
| EditFileTool | workspace | MEMORY.md, SOUL.md, USER.md (unchanged scope) |
| WriteFileTool | `memory/` + `skills/` | staging.md (new), SKILL.md files (unchanged) |

WriteFileTool's `allowed_dir` is expanded from `skills/` to include `memory/`.

### 4.4 Phase 2 Execution Logic

Phase 2 processes Phase 1's output directives:

1. `[STAGING-NEW]` → Append new entries to staging.md under the specified topic.
2. `[STAGING-UPDATE]` → Update matched entries: increment seen, replace content with refined version.
3. `[PROMOTE]` → Write promoted content to target file (via EditFileTool), remove from staging.md.
4. `[FORGET]` → Remove specified entries from staging.md.
5. `[FILE]` / `[FILE-REMOVE]` / `[SKILL]` → Existing behavior, unchanged.

Since staging.md changes are cumulative (new + update + promote + forget), Phase 2 collects all staging directives and performs a single WriteFileTool call with the fully updated staging.md content.

### 4.5 Template Changes

**`dream_phase1.md`** — Key additions:
- Instruction to read staging.md and compare with new history entries.
- `[STAGING-NEW]`, `[STAGING-UPDATE]`, `[PROMOTE]`, `[FORGET]` output format specification.
- Promotion criteria: seen >= threshold + semantic quality judgment.
- Forgetting criteria: low-frequency rule (age > 14d, seen <= 1) and semantic staleness (age > 7d, LLM judgment).
- Entry refinement guidance: when matching existing entries, merge information and improve wording.

**`dream_phase2.md`** — Key additions:
- Instruction to process `[STAGING-NEW]`/`[STAGING-UPDATE]`/`[PROMOTE]`/`[FORGET]` directives.
- WriteFileTool for staging.md (full rewrite with updated metadata).
- Promotion workflow: write to target file → remove from staging.
- Forgetting workflow: remove from staging.

## Part 5: DreamAudit

### 5.1 Class Design

```python
class DreamAudit:
    """Daily memory audit: cross-file dedup, dead entry cleanup, restructuring."""

    def __init__(self, store: MemoryStore, provider: LLMProvider, model: str):
        self.store = store
        self.provider = provider
        self.model = model
        self._runner = AgentRunner(provider)
        self._tools = self._build_tools()

    def _build_tools(self) -> ToolRegistry:
        """Read + Edit tools for MEMORY.md, SOUL.md, USER.md, staging.md."""
        # ReadFileTool: workspace scope
        # EditFileTool: workspace scope
        # No WriteFileTool needed — audit only does targeted edits.

    async def run(self) -> bool:
        """Run the two-phase audit. Returns True if changes were made."""
```

### 5.2 Audit Phase 1 Input

```
## Current Date
{current_date}

## Current MEMORY.md
{current_memory}

## Current SOUL.md
{current_soul}

## Current USER.md
{current_user}

## Current staging.md
{current_staging}
```

### 5.3 Audit Responsibilities

1. **Cross-file dedup**: Same fact appearing in multiple files → keep in most authoritative location, remove elsewhere.
2. **Dead entry cleanup**: Clearly outdated/completed content → remove.
3. **Structure reorganization**: Adjust heading levels, regroup sections.
4. **Compression**: Verbose entries → concise versions.
5. **Timeliness annotation**: Mark active/completed status where appropriate.

### 5.4 Audit Phase 2 Execution

Same pattern as Dream Phase 2: EditFileTool for targeted edits across all memory files.

### 5.5 Scheduling

DreamAudit runs on its own cron schedule, configured via DreamConfig:

```python
class DreamConfig(Base):
    # Existing fields...
    staging_promotion_threshold: int = Field(default=3, ge=1)
    audit_cron: str = Field(default="0 3 * * *")  # Default: 3am daily
    audit_model_override: str | None = None  # Optional model for audit
```

The audit uses its own model override (if configured) and runs independently from the regular Dream cycle.

### 5.6 System Job Registration

DreamAudit is registered as a **protected system cron job** (same pattern as Dream):

```python
# In commands.py, alongside Dream registration:
cron.register_system_job(CronJob(
    id="dream_audit",
    name="dream_audit",
    schedule=dream_cfg.build_audit_schedule(config.agents.defaults.timezone),
    payload=CronPayload(kind="system_event"),
))
```

Protected via `payload.kind = "system_event"` — cannot be deleted by users through the cron API.

The cron callback handler in `on_cron_job` is extended:

```python
async def on_cron_job(job: CronJob) -> str | None:
    if job.name == "dream":
        await agent.dream.run()
        return None
    if job.name == "dream_audit":
        await agent.dream_audit.run()
        return None
    # ... existing user job handling
```

## Part 6: Context Injection

### 6.1 ContextBuilder Changes

```python
def build_system_prompt(self, ...) -> str:
    parts = [self._get_identity(channel=channel)]
    bootstrap = self._load_bootstrap_files()
    if bootstrap:
        parts.append(bootstrap)

    # Long-term memory (existing)
    memory = self.memory.get_memory_context()
    if memory and not self._is_template_content(...):
        parts.append(f"# Memory\n\n{memory}")

    # Short-term memory (new)
    staging = self.memory.get_staging_context()
    if staging:
        parts.append(staging)

    # Skills + Recent History (existing, unchanged)
    ...
```

### 6.2 Git Tracking

Update GitStore tracked files to include staging.md:

```python
self._git = GitStore(workspace, tracked_files=[
    "SOUL.md", "USER.md", "memory/MEMORY.md", "memory/staging.md",
    "memory/.dream_cursor",
])
```

## Part 7: Configuration

### 7.1 DreamConfig Additions

```python
class DreamConfig(Base):
    # Existing fields (unchanged):
    interval_h: int = 2
    cron: str | None = None
    model_override: str | None = None
    max_batch_size: int = 20
    max_iterations: int = 15
    annotate_line_ages: bool = True

    # New fields:
    staging_promotion_threshold: int = Field(
        default=3,
        ge=1,
        validation_alias=AliasChoices("stagingPromotionThreshold"),
        serialization_alias="stagingPromotionThreshold",
    )  # Seen count required for staging → long-term promotion

    audit_cron: str = Field(
        default="0 3 * * *",
        validation_alias=AliasChoices("auditCron"),
        serialization_alias="auditCron",
    )  # Cron expression for daily DreamAudit (default: 3am)

    audit_model_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("auditModelOverride", "auditModel", "audit_model_override"),
    )  # Optional separate model for audit runs

    audit_max_iterations: int = Field(
        default=15,
        ge=1,
        validation_alias=AliasChoices("auditMaxIterations"),
        serialization_alias="auditMaxIterations",
    )  # Max tool calls per audit Phase 2

    def build_audit_schedule(self, timezone: str) -> CronSchedule:
        return CronSchedule(kind="cron", expr=self.audit_cron, tz=timezone)
```

## Part 8: Test Plan

### 8.1 Unit Tests

| Test | Description |
|------|-------------|
| `test_staging_read_write` | MemoryStore staging file read/write round-trip |
| `test_staging_metadata_strip` | `get_staging_context()` strips date, seen, age metadata |
| `test_staging_age_calculation` | Age computed correctly from entry date |
| `test_staging_empty_file` | Empty/missing staging.md returns empty context |
| `test_staging_malformed_entry` | Entries without pipe separators handled gracefully |

### 8.2 Integration Tests

| Test | Description |
|------|-------------|
| `test_dream_staging_new` | Dream extracts new staging entries from history |
| `test_dream_staging_seen_increment` | Matching entries get seen+1 |
| `test_dream_staging_refine` | Matched entries get wording improvements |
| `test_dream_staging_promote` | High-seen entries promoted to MEMORY.md, removed from staging |
| `test_dream_staging_forget_low_freq` | age>14d + seen<=1 entries deleted |
| `test_dream_staging_forget_semantic` | Phase 1 marks stale entries, Phase 2 removes |
| `test_dream_staging_conflict` | New info contradicts long-term memory → update |
| `test_context_injection_order` | Staging appears between Memory and Skills in system prompt |

### 8.3 Audit Tests

| Test | Description |
|------|-------------|
| `test_audit_cross_file_dedup` | Same fact in MEMORY.md and USER.md → keep in most appropriate |
| `test_audit_dead_entry_cleanup` | Outdated entries removed |
| `test_audit_restructuring` | Heading/section reorganization |
| `test_audit_compression` | Verbose entries compressed |

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Staging write tool | WriteFileTool (full rewrite) | seen/age updates + entry removal = full rewrite is simpler and more reliable than many EditFileTool calls |
| Long-term write tool | EditFileTool (surgical edits) | Consistent with existing Dream behavior, safer |
| Seen matching | Phase 1 LLM semantic judgment | Matches user's requirement for refinement; one history entry can match multiple staging entries |
| Staging initialization | Empty file | Simple, no migration complexity |
| Promotion threshold | Configurable (default 3) | Users can tune based on their Dream frequency and conversation volume |
| Audit scheduling | Separate cron config | Independent from Dream's 2h cycle; audit is expensive and doesn't need to run every 2h |
| Audit class | Reuses AgentRunner + ToolRegistry | Shares infrastructure with Dream, different prompt templates |
| All new facts route through staging first | Simplifies Dream routing logic — promotion decides destination later |
