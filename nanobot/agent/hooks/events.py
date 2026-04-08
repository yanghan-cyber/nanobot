"""Event definitions for the InternalHook system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Event type / action constants (single source of truth)
# ---------------------------------------------------------------------------

# Types
AGENT: str = "agent"
MESSAGE: str = "message"
TOOL: str = "tool"

# Actions
BOOTSTRAP: str = "bootstrap"
RECEIVED: str = "received"
SENT: str = "sent"
BEFORE_CALL: str = "before_call"
AFTER_CALL: str = "after_call"

# Compound keys
AGENT_BOOTSTRAP: str = "agent:bootstrap"
MESSAGE_RECEIVED: str = "message:received"
MESSAGE_SENT: str = "message:sent"
TOOL_BEFORE_CALL: str = "tool:before_call"
TOOL_AFTER_CALL: str = "tool:after_call"


@dataclass
class InternalHookEvent:
    """Event object passed to hook handlers.

    Aligned with OpenClaw's InternalHookEvent interface.
    Handlers receive this and may mutate context/messages in place.
    """

    type: str
    action: str
    session_key: str
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    messages: list[str] = field(default_factory=list)

    @staticmethod
    def create(
        type_: str,
        action: str,
        session_key: str,
        context: dict[str, Any] | None = None,
    ) -> InternalHookEvent:
        """Create a new event with sensible defaults."""
        return InternalHookEvent(
            type=type_,
            action=action,
            session_key=session_key,
            context=context if context is not None else {},
        )


# ---------------------------------------------------------------------------
# Type-specific context structures (aligned with OpenClaw spec §3.4)
# ---------------------------------------------------------------------------


@dataclass
class AgentBootstrapContext:
    """Context for ``agent:bootstrap`` events.

    Aligned with OpenClaw's ``AgentBootstrapHookContext``.
    """

    workspace_dir: str
    bootstrap_files: list[dict[str, Any]] = field(default_factory=list)
    session_key: str | None = None
    session_id: str | None = None
    agent_id: str | None = None


@dataclass
class MessageReceivedContext:
    """Context for ``message:received`` events.

    Aligned with OpenClaw's ``MessageReceivedHookContext``.
    """

    from_: str
    content: str
    channel_id: str
    conversation_id: str | None = None
    message_id: str | None = None
    account_id: str | None = None
    metadata: dict[str, Any] | None = None
    timestamp: int | None = None


@dataclass
class MessageSentContext:
    """Context for ``message:sent`` events.

    Aligned with OpenClaw's ``MessageSentHookContext``.
    """

    to: str
    content: str
    success: bool
    channel_id: str
    conversation_id: str | None = None
    message_id: str | None = None
    error: str | None = None


@dataclass
class ToolCallContext:
    """Context for ``tool:before_call`` / ``tool:after_call`` events.

    Nanobot-specific extension (not in OpenClaw).
    """

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any | None = None
    error: str | None = None
    session_key: str | None = None
