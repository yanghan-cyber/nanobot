"""Event type guard functions for the InternalHook system.

Aligned with OpenClaw's ``isAgentBootstrapEvent()``, ``isMessageReceivedEvent()``, etc.
"""

from __future__ import annotations

from nanobot.agent.hooks.events import InternalHookEvent


def is_agent_bootstrap_event(event: InternalHookEvent) -> bool:
    """Check if event is a valid ``agent:bootstrap`` with required context."""
    if event.type != "agent" or event.action != "bootstrap":
        return False
    ctx = event.context
    return isinstance(ctx.get("workspace_dir"), str) and isinstance(
        ctx.get("bootstrap_files"), list
    )


def is_message_received_event(event: InternalHookEvent) -> bool:
    """Check if event is a valid ``message:received`` with required context."""
    if event.type != "message" or event.action != "received":
        return False
    ctx = event.context
    return (
        isinstance(ctx.get("from_"), str)
        and isinstance(ctx.get("content"), str)
        and isinstance(ctx.get("channel_id"), str)
    )


def is_message_sent_event(event: InternalHookEvent) -> bool:
    """Check if event is a valid ``message:sent`` with required context."""
    if event.type != "message" or event.action != "sent":
        return False
    ctx = event.context
    return (
        isinstance(ctx.get("to"), str)
        and isinstance(ctx.get("content"), str)
        and isinstance(ctx.get("success"), bool)
        and isinstance(ctx.get("channel_id"), str)
    )


def is_tool_before_call_event(event: InternalHookEvent) -> bool:
    """Check if event is a valid ``tool:before_call``."""
    if event.type != "tool" or event.action != "before_call":
        return False
    return isinstance(event.context.get("tool_name"), str)


def is_tool_after_call_event(event: InternalHookEvent) -> bool:
    """Check if event is a valid ``tool:after_call``."""
    if event.type != "tool" or event.action != "after_call":
        return False
    return isinstance(event.context.get("tool_name"), str)
