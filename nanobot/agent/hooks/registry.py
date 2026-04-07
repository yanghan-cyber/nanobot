"""Event registry for the InternalHook system.

Aligned with OpenClaw's internal-hooks.ts: registerInternalHook / triggerInternalHook.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from nanobot.agent.hooks.events import InternalHookEvent

HookHandler = Callable[[InternalHookEvent], Awaitable[None] | None]

# Module-level singleton (Python equivalent of OpenClaw's Symbol.for() global).
_handlers: dict[str, list[HookHandler]] = {}


def register_internal_hook(event_key: str, handler: HookHandler) -> None:
    """Register a handler for an event key (e.g. "agent:bootstrap")."""
    if event_key not in _handlers:
        _handlers[event_key] = []
    _handlers[event_key].append(handler)


def unregister_internal_hook(event_key: str, handler: HookHandler) -> None:
    """Remove a specific handler registration."""
    if event_key not in _handlers:
        return
    try:
        _handlers[event_key].remove(handler)
    except ValueError:
        pass
    if not _handlers[event_key]:
        del _handlers[event_key]


def clear_internal_hooks() -> None:
    """Remove all handlers. Intended for testing."""
    _handlers.clear()


def has_listeners(type_: str, action: str) -> bool:
    """Check if any handlers exist for a type or type:action combination."""
    return bool(_handlers.get(type_)) or bool(_handlers.get(f"{type_}:{action}"))


async def trigger_internal_hook(event: InternalHookEvent) -> None:
    """Trigger an event, calling all registered handlers.

    General handlers (e.g. "agent") run first, then specific (e.g. "agent:bootstrap").
    Each handler is wrapped in try-catch for error isolation.
    """
    if not has_listeners(event.type, event.action):
        return

    type_handlers = _handlers.get(event.type, [])
    specific_handlers = _handlers.get(f"{event.type}:{event.action}", [])
    all_handlers = [*type_handlers, *specific_handlers]

    for handler in all_handlers:
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception(
                "Hook error [{}:{}]",
                event.type,
                event.action,
            )
