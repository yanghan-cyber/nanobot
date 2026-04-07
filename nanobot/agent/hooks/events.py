"""Event definitions for the InternalHook system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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
