# CLAUDE.md — nanobot fork (yanghan-main branch)

This is a fork of [HKUDS/nanobot](https://github.com/HKUDS/nanobot) with custom enhancements.

## Merge policy

When merging upstream `main` into `yanghan-main`:
- **Preserve fork behavior** unless upstream has a clearly superior rewrite (e.g. major refactor, perf improvement, bug fix that supersedes ours).
- If upstream refactors a file we also modified, audit the diff carefully — prefer keeping our design intent while adopting upstream's structural improvements.
- When a conflict touches any item below, resolve in favor of the fork version unless there's a compelling reason not to.

## Fork-specific features to preserve during upstream merges

### 1. Naming conventions
- **Bash, not Exec**: `BashTool`, `BashToolConfig`, `bash_config` everywhere. Upstream names these `Exec*`. Always keep `Bash*` naming.
- Applies to: `tools/shell.py`, `config/schema.py`, `config/loader.py`, `agent/loop.py`, `agent/subagent.py`, all templates, all tests.

### 2. New tools (upstream does not have these at all)
- **LoadSkillTool** (`nanobot/agent/tools/skill.py`): on-demand skill loading by name. Returns path + content (stripped frontmatter). Registered in both main agent loop and subagents.
- **ShellBgTool** (inside `nanobot/agent/tools/shell.py`): background shell tasks with `list`/`output`/`kill` actions. TTL-based auto-cleanup (default 2h). Registered alongside BashTool when `bash_config.enable=true`.
- **Ripgrep-based search** (`nanobot/utils/ripgrep.py` + rewritten `nanobot/agent/tools/search.py`): `GrepTool`/`GlobTool` use a managed ripgrep binary (auto-download, cross-platform) instead of pure Python grep/glob. Upstream uses `fnmatch`/`re` Python implementations.

### 3. Agent Loop behavioral differences (`nanobot/agent/loop.py`)
- **`drop_runtime=False`** in `_save_turn`: user messages are persisted with `[Runtime Context]` block intact. Upstream strips it (`drop_runtime=True`). **Why**: keeping runtime context in history improves prompt cache hit rate on subsequent turns.
- **Runtime context appended after user content**: `build_messages`, early persist, and pending queue injection all place `[Runtime Context]` **after** the user's text (i.e. `"{user_content}\n\n{runtime_ctx}"`). Upstream prepends it before user content (`"{runtime_ctx}\n\n{user_content}"`). **Why**: user content first is more natural; runtime metadata as a suffix doesn't interfere with the model reading the actual input.
- **Frozen system prompt**: `_get_frozen_prompt()` / `invalidate_frozen_prompt()` cache the system prompt per session, rebuilt only when session object changes. Upstream rebuilds every turn. **Why**: avoids redundant prompt reconstruction.
- **Pending message queue**: `_pending_queues` + `_active_sessions` for mid-turn message injection. Messages arriving during an active agent turn are queued and injected via `pending_message_callback` in `AgentRunSpec`. Upstream drops or races these.
- **Session switch detection**: when session object identity changes between turns, `invalidate_frozen_prompt()` is called to force rebuild.
- **`current_role` always `"user"` for pending queue injection**: when subagent results arrive via the pending message queue, `current_role` is set to `"user"`. Upstream uses `"assistant" if is_subagent else "user"`. **Why**: subagent results should be presented to the model as user-side input, keeping the conversation flow natural.
- **`Message Time:` not `Current Time:`**: `_build_runtime_context` uses `Message Time:` instead of upstream's `Current Time:`. **Why**: semantically correct — the label appears on both live and historical messages, "message time" is accurate for both; "current time" is misleading in history.
- **No `include_timestamps`**: fork does not pass `include_timestamps=True` to `get_history`. Upstream enables it to add `[Message Time: ...]` prefix on user messages. **Why**: fork already preserves the full `[Runtime Context]` block (with `Message Time:`) via `drop_runtime=False`, so the separate timestamp prefix is redundant.
- **`session_summary` stays in `_build_runtime_context`**: upstream moved session_summary from `_build_runtime_context` into `build_system_prompt` (commit `a6e993df`) for KV cache stability. Fork keeps it in runtime context. **Why**: session_summary only appears after autocompact (rare event, most sessions never trigger it); putting it in system prompt would require frozen prompt invalidation on compaction for minimal cache benefit. Fork's `drop_runtime=False` already persists the summary via runtime context block. Applies to: `nanobot/agent/context.py` (`_build_runtime_context` and `build_messages` signatures), `nanobot/agent/loop.py` (passes `session_summary=pending_summary` to `build_messages`, not to `build_system_prompt`).

### 4. Subagent differences (`nanobot/agent/subagent.py`)
- **`subagent_max_iterations` configurable** (default 50, upstream: inherits main loop's 200 via `_sync_subagent_runtime_limits`). Fork adds `subagentMaxIterations` to `AgentDefaults` config, `subagent_max_iterations` param to `AgentLoop` constructor, and threads it through `commands.py`. **Why**: subagents need more iterations than upstream's 15 but fewer than the main loop's 200; making it configurable avoids hardcoding.
- **`fail_on_tool_error=False`** (upstream: True). **Why**: transient tool errors should not abort the entire subagent run.
- **`_announce_result` has no `task` parameter** (upstream includes one). **Why**: task description is already captured elsewhere; omitting it keeps session history compact.
- **Output storage + tail in `_announce_result`** (upstream: head+tail 1000-char truncation): full output is always written to `data/tool-output/subagent/{task_id}.log`. Only the last 50 lines (`_TAIL_LINES`) are returned to the main agent, with a file path hint appended when truncated. If file write fails (OSError), the announcement still proceeds with the tail but without the file hint. **Why**: the old truncation permanently lost the middle content; file storage lets the main agent read the full output on demand.
- **`_subagent_output_path()` module-level helper**: resolves output file path under `data/tool-output/subagent/`. Used by `_announce_result` and testable independently.
- **`SubagentStatus` dataclass**: real-time status tracking per subagent (phase, iteration, tool events, usage).
- **Registers `LoadSkillTool` + `ShellBgTool`** in subagent tool registry (upstream registers neither).

### 5. Runner differences (`nanobot/agent/runner.py`)
- When `max_iterations` is reached, fork makes an **extra LLM summary call** to produce a coherent final response. Upstream uses the prompt text directly as final content. **Why**: LLM-generated summaries are more useful than raw prompt fallbacks.

### 6. Read File differences (`nanobot/agent/tools/filesystem.py`)
- **Image handling**: returns metadata-only message `"Image file detected: ... Use a vision-capable skill or tool to analyze."` (upstream returns visual content blocks via `build_image_content_blocks`). **Why**: fork channels may not support vision.
- **Read dedup rewritten**: simplified dedup logic with external-modification detection and content-hash verification. Fixes a double-read bug present upstream.
- **`_PROC_FD_RE` pre-compiled regex** for device path blocking (upstream recompiles per call).

### 7. Skills format differences (`nanobot/agent/skills.py`)
Upstream already has `_get_skill_meta()`, `disabled_skills`, `get_always_skills()`, `build_skills_summary()`, `yaml.safe_load`. Our differences are:
- **`build_skills_summary()` output format**: compact `"- name: description"` with `_indent_description()` for multi-line. Upstream uses bold markdown `"- **name** — desc  \`path\`"`. **Why**: more compact, no markdown bold overhead in system prompt.
- **`unavailable` text**: `needs CLI 'x'` / `needs ENV 'x'` (upstream: `CLI: x` / `ENV: x`).
- **`strip_frontmatter()` is public** (upstream: `_strip_frontmatter` private). Used by LoadSkillTool.
- **`get_skill_path()`** added: resolves SKILL.md path by name (upstream doesn't have this). Used by LoadSkillTool.
- **Skills section template** (`templates/agent/skills_section.md`): references `load_skill` for on-demand loading instead of inlining all skill content (upstream inlines full text).

### 8. Config schema additions (`nanobot/config/schema.py`)
- `ToolsConfig.bash` (not `exec`) with `BashToolConfig`: adds `bg_ttl`, `bg_max_entries`, `bg_ttl_minutes()` — upstream `ExecToolConfig` lacks these.
- `AgentDefaults.subagent_max_iterations` (camelCase: `subagentMaxIterations`): separate iteration limit for subagents, default 50. Upstream has no such field; subagents inherit main loop's `max_tool_iterations`.
- `ToolsConfig.my`: `MyToolConfig` with `enable` and `allow_set`. **Note**: upstream also has `MyToolConfig`, but uses `exec` field name.

### 9. Provider fixes (upstream does not have these)
- **HTTP timeout**: explicit timeout in `anthropic_provider`, `azure_openai_provider`, `openapi_compat_provider` to prevent 10-minute hangs on stalled API calls.
- **Tool message serialization**: list content items serialized to string for OpenAI API compatibility.
- **Image strip in-place**: on retry, images are stripped in-place to prevent repeated error-retry cycles.
- **Preserve assistant content with tool_calls**: `_sanitize_messages()` in `openai_compat_provider.py` only strips content for assistant+tool_calls when content is empty/whitespace. Upstream unconditionally sets `content = None` for all assistant+tool_calls messages, which causes the model to lose its own previous text replies in multi-turn tool-use loops. **Why**: GLM (and potentially other models) sometimes generates both text content and tool_calls in the same response; stripping the content means the next iteration's context is incomplete.

### 10. Channel fixes (upstream does not have these)
- **Feishu**: per-chat reaction tracking (`_reactions_per_chat`), proper cleanup on stream end, skip reaction cleanup during mid-turn resuming pauses.
- **WebSocket**: deduplicated send/parse logic in multiplex code.

### 11. BashTool `purpose` parameter required
- `purpose` is a required parameter alongside `command` in BashTool. Upstream ExecTool has no such field. **Why**: purpose gives the LLM a hint about why the command is run, improving tool hint display.

### 12. Test file differences
- `test_shell_tool.py` (not `test_exec_*`): BashTool + ShellBgTool tests
- `test_load_skill_tool.py`: LoadSkillTool tests
- `test_ripgrep_util.py`: ripgrep utility tests
- `test_loop_message_queue.py`: mid-turn injection e2e tests
- `test_subagent_output_storage.py`: subagent output file-write, tail logic, write-failure fallback, empty result
- `test_skills_loader.py`: extended with `get_skill_path`, `build_skills_summary` format assertions, `disabled_skills` tests
- `test_bash_security.py` (renamed from `test_exec_security.py`)

## Development

```bash
# Install (use uv only)
uv sync --extra dev

# Run tests
uv run pytest tests/ -x

# Pre-existing known issues
# - N806 warnings in ruff (uppercase function names in tests)
```

## Project Overview (from upstream)

nanobot is a lightweight, open-source AI agent framework written in Python with a React/TypeScript WebUI. It centers around a small agent loop that receives messages from chat channels, invokes an LLM provider, executes tools, and manages session memory.

### Development Commands

```bash
# Python: run single test / lint
pytest tests/test_openai_api.py::test_function -v
ruff check nanobot/

# WebUI: dev server (proxies API/WS to gateway :8765), build, test
# Build outputs to ../nanobot/web/dist (bundled into the Python wheel)
cd webui && bun run dev      # or NANOBOT_API_URL=... bun run dev
cd webui && bun run build
cd webui && bun run test

# Gateway
nanobot gateway
```

### High-Level Architecture

#### Core Data Flow

Messages flow through an async `MessageBus` (`nanobot/bus/queue.py`) that decouples chat channels from the agent core:

1. **Channels** (`nanobot/channels/`) receive messages from external platforms and publish `InboundMessage` events to the bus.
2. **`AgentLoop`** (`nanobot/agent/loop.py`) consumes inbound messages, builds context, and coordinates the turn.
3. **`AgentRunner`** (`nanobot/agent/runner.py`) handles the actual LLM conversation loop: send messages to the provider, receive tool calls, execute tools, and stream responses.
4. Responses are published as `OutboundMessage` events back to the appropriate channel.

#### Key Subsystems

- **Agent Loop** (`nanobot/agent/loop.py`, `runner.py`): The core processing engine. `AgentLoop` manages session keys, hooks, and context building. `AgentRunner` executes the multi-turn LLM conversation with tool execution.
- **LLM Providers** (`nanobot/providers/`): Provider implementations (Anthropic, OpenAI-compatible, Azure, GitHub Copilot, etc.) built on a common base (`base.py`). `factory.py` and `registry.py` handle instantiation and model discovery.
- **Channels** (`nanobot/channels/`): Platform integrations (Telegram, Discord, Slack, Feishu, Matrix, WhatsApp, QQ, WeChat, WebSocket, etc.). `manager.py` discovers and coordinates them. Channels are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Tools** (`nanobot/agent/tools/`): Agent capabilities exposed to the LLM: filesystem (read/write/edit/list), shell execution, web search/fetch, MCP servers, cron, notebook editing, subagent spawning, and `MyTool` for self-modification.
- **Memory** (`nanobot/agent/memory.py`): Session history persistence with Dream two-phase memory consolidation. Uses atomic writes with fsync for durability.
- **Session Management** (`nanobot/session/manager.py`): Per-session history, context compaction, and TTL-based auto-compaction.
- **Config** (`nanobot/config/schema.py`, `loader.py`): Pydantic-based configuration loaded from `~/.nanobot/config.json`. Supports camelCase aliases for JSON compatibility.
- **Bridge** (`bridge/`): TypeScript services (e.g. WhatsApp bridge) bundled into the wheel via `pyproject.toml` `force-include`.
- **WebUI** (`webui/`): Vite-based React SPA that talks to the gateway over a WebSocket multiplex protocol. The dev server proxies `/api`, `/webui`, `/auth`, and WebSocket traffic to the gateway.

#### Entry Points

- **CLI**: `nanobot/cli/commands.py`
- **Python SDK**: `nanobot/nanobot.py`

### Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

### Branching Strategy

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full two-branch model (`main` vs `nightly`) and PR guidelines.

### Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored).
- pytest with `asyncio_mode = "auto"`.

### Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Channel base / new channel template: `nanobot/channels/base.py`
- Tool registry: `nanobot/agent/tools/registry.py`
- WebUI dev proxy config: `webui/vite.config.ts`
- Tests mirror the `nanobot/` package structure.
