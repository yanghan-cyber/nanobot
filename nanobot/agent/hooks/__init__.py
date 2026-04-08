"""OpenClaw-compatible event-driven hook system.

Usage::

    from nanobot.agent.hooks import (
        InternalHookEvent,
        register_internal_hook,
        trigger_internal_hook,
        load_hooks,
    )

    # Programmatic registration
    async def my_handler(event: InternalHookEvent) -> None:
        event.context["added"] = True

    register_internal_hook("agent:bootstrap", my_handler)

    # Directory-based discovery
    from pathlib import Path
    await load_hooks(Path("workspace/hooks"))
"""

from nanobot.agent.hooks.discovery import load_hooks
from nanobot.agent.hooks.events import (
    AFTER_CALL,
    AGENT,
    AGENT_BOOTSTRAP,
    BEFORE_CALL,
    BOOTSTRAP,
    MESSAGE,
    MESSAGE_RECEIVED,
    MESSAGE_SENT,
    RECEIVED,
    SENT,
    TOOL,
    TOOL_AFTER_CALL,
    TOOL_BEFORE_CALL,
    AgentBootstrapContext,
    InternalHookEvent,
    MessageReceivedContext,
    MessageSentContext,
    ToolCallContext,
)
from nanobot.agent.hooks.guards import (
    is_agent_bootstrap_event,
    is_message_received_event,
    is_message_sent_event,
    is_tool_after_call_event,
    is_tool_before_call_event,
)
from nanobot.agent.hooks.registry import (
    clear_internal_hooks,
    has_listeners,
    register_internal_hook,
    trigger_internal_hook,
    unregister_internal_hook,
)

__all__ = [
    # Constants
    "AGENT",
    "AGENT_BOOTSTRAP",
    "AFTER_CALL",
    "BEFORE_CALL",
    "BOOTSTRAP",
    "MESSAGE",
    "MESSAGE_RECEIVED",
    "MESSAGE_SENT",
    "RECEIVED",
    "SENT",
    "TOOL",
    "TOOL_AFTER_CALL",
    "TOOL_BEFORE_CALL",
    # Types
    "InternalHookEvent",
    "AgentBootstrapContext",
    "MessageReceivedContext",
    "MessageSentContext",
    "ToolCallContext",
    # Registry
    "register_internal_hook",
    "unregister_internal_hook",
    "trigger_internal_hook",
    "clear_internal_hooks",
    "has_listeners",
    "load_hooks",
    # Guards
    "is_agent_bootstrap_event",
    "is_message_received_event",
    "is_message_sent_event",
    "is_tool_before_call_event",
    "is_tool_after_call_event",
]
