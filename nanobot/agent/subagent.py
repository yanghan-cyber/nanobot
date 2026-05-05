"""Subagent manager for background task execution."""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.hook import AgentHook, AgentHookContext
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SkillsLoader
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.search import GlobTool, GrepTool
from nanobot.agent.tools.shell import BashTool, ShellBgTool
from nanobot.agent.tools.skill import LoadSkillTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.paths import get_data_dir
from nanobot.config.schema import AgentDefaults, BashToolConfig, WebToolsConfig
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import ensure_dir
from nanobot.utils.prompt_templates import render_template

_TAIL_LINES = 50


def _subagent_output_path(task_id: str) -> Path:
    return ensure_dir(get_data_dir() / "tool-output" / "subagent") / f"{task_id}.log"


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running subagent."""

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    phase: str = "initializing"  # initializing | awaiting_tools | tools_completed | final_response | done | error
    iteration: int = 0
    tool_events: list = field(default_factory=list)   # [{name, status, detail}, ...]
    usage: dict = field(default_factory=dict)          # token usage
    stop_reason: str | None = None
    error: str | None = None


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls and updates status."""

    def __init__(self, task_id: str, status: SubagentStatus | None = None) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id,
                tool_call.name,
                args_str,
            )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)


class SubagentManager:
    """Manages background subagent execution."""

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
        max_iterations: int | None = None,
        session_manager: SessionManager | None = None,
    ):
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.web_config = web_config or WebToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.bash_config = bash_config or BashToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.sessions = session_manager
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else AgentDefaults().max_tool_iterations
        )
        self.runner = AgentRunner(provider)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
        )
        self._task_statuses[task_id] = status

        bg_task = asyncio.create_task(
            self._run_subagent(
                task_id, task, display_label, origin, status,
                origin_message_id, session_key=session_key,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            self._task_statuses.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = ToolRegistry()
            allowed_dir = self.workspace if (self.restrict_to_workspace or self.bash_config.sandbox) else None
            extra_read = [BUILTIN_SKILLS_DIR] if allowed_dir else None
            # Subagent gets its own FileStates so its read-dedup cache is
            # isolated from the parent loop's sessions (issue #3571).
            from nanobot.agent.tools.file_state import FileStates
            file_states = FileStates()
            tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir, extra_allowed_dirs=extra_read, file_states=file_states))
            tools.register(WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir, file_states=file_states))
            tools.register(EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir, file_states=file_states))
            tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir, file_states=file_states))
            tools.register(GlobTool(workspace=self.workspace, allowed_dir=allowed_dir, file_states=file_states))
            tools.register(GrepTool(workspace=self.workspace, allowed_dir=allowed_dir, file_states=file_states))
            if self.bash_config.enable:
                tools.register(BashTool(
                    working_dir=str(self.workspace),
                    timeout=self.bash_config.timeout,
                    restrict_to_workspace=self.restrict_to_workspace,
                    sandbox=self.bash_config.sandbox,
                    path_append=self.bash_config.path_append,
                    allowed_env_keys=self.bash_config.allowed_env_keys,
                    allow_patterns=self.bash_config.allow_patterns,
                    deny_patterns=self.bash_config.deny_patterns,
                ))
                tools.register(ShellBgTool(
                    bg_ttl_minutes=self.bash_config.bg_ttl_minutes(),
                    bg_max_entries=self.bash_config.bg_max_entries,
                ))
            if self.web_config.enable:
                tools.register(
                    WebSearchTool(
                        config=self.web_config.search,
                        proxy=self.web_config.proxy,
                        user_agent=self.web_config.user_agent,
                    )
                )
                tools.register(
                    WebFetchTool(
                        config=self.web_config.fetch,
                        proxy=self.web_config.proxy,
                        user_agent=self.web_config.user_agent,
                    )
                )
            skills_loader = SkillsLoader(
                self.workspace,
                disabled_skills=self.disabled_skills,
            )
            tools.register(LoadSkillTool(skills_loader=skills_loader))
            system_prompt = self._build_subagent_prompt(skills_loader=skills_loader)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            result = await self.runner.run(
                AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    model=self.model,
                    max_iterations=self.max_iterations,
                    max_tool_result_chars=self.max_tool_result_chars,
                    hook=_SubagentHook(task_id, status),
                    max_iterations_message="You have reached the maximum number of iterations. Summarize what you have found so far and provide your best answer based on the research completed.",
                    error_message=None,
                    fail_on_tool_error=False,
                    checkpoint_callback=_on_checkpoint,
                )
            )
            status.phase = "done"
            status.stop_reason = result.stop_reason

            # Persist full subagent conversation to SQLite
            if self.sessions and result.messages:
                import json as _json

                from nanobot.session.db import generate_session_id
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

            if result.stop_reason == "tool_error":
                status.tool_events = list(result.tool_events)
                await self._announce_result(
                    task_id,
                    label,
                    self._format_partial_progress(result),
                    origin, "error", origin_message_id,
                )
            elif result.stop_reason == "error":
                await self._announce_result(
                    task_id,
                    label,
                    result.error or "Error: subagent execution failed.",
                    origin, "error", origin_message_id,
                )
            else:
                final_result = (
                    result.final_content or "Task completed but no final response was generated."
                )

                logger.info("Subagent [{}] completed successfully", task_id)
                await self._announce_result(task_id, label, final_result, origin, "ok", origin_message_id)

        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, label, f"Error: {e}", origin, "error", origin_message_id)

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        output_path = _subagent_output_path(task_id)
        saved = False
        try:
            output_path.write_text(result, encoding="utf-8")
            saved = True
        except OSError as exc:
            logger.warning("Failed to persist subagent output to {}: {}", output_path, exc)

        lines = result.splitlines()
        if len(lines) <= _TAIL_LINES:
            display = result
        else:
            tail = "\n".join(lines[-_TAIL_LINES:])
            display = tail
            if saved:
                display += (
                    f"\n\n... (last {_TAIL_LINES} of {len(lines)} lines,"
                    f" full output saved to: {output_path})"
                )
            else:
                display += f"\n\n... (last {_TAIL_LINES} of {len(lines)} lines)"

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            result=display,
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (which accounts for unified sessions) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata=metadata,
        )

        await self.bus.publish_inbound(msg)
        logger.debug(
            "Subagent [{}] announced result to {}:{}", task_id, origin["channel"], origin["chat_id"]
        )

    @staticmethod
    def _format_partial_progress(result) -> str:
        completed = [e for e in result.tool_events if e["status"] == "ok"]
        failure = next((e for e in reversed(result.tool_events) if e["status"] == "error"), None)
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (result.error or "Error: subagent execution failed.")

    def _build_subagent_prompt(self, skills_loader: SkillsLoader | None = None) -> str:
        """Build a focused system prompt for the subagent."""
        from nanobot.agent.context import ContextBuilder

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        if skills_loader is None:
            skills_loader = SkillsLoader(
                self.workspace,
                disabled_skills=self.disabled_skills,
            )
        skills_summary = skills_loader.build_skills_summary()
        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(self.workspace),
            skills_summary=skills_summary or "",
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [
            self._running_tasks[tid]
            for tid in self._session_tasks.get(session_key, [])
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )
