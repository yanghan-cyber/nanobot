"""Tests for agent:bootstrap trigger point context."""

import pytest

from nanobot.agent.hooks.events import InternalHookEvent
from nanobot.agent.hooks.registry import (
    clear_internal_hooks,
    register_internal_hook,
    trigger_internal_hook,
)


@pytest.fixture(autouse=True)
def clean_registry():
    clear_internal_hooks()
    yield
    clear_internal_hooks()


@pytest.mark.asyncio
async def test_bootstrap_event_includes_bootstrap_files():
    """agent:bootstrap event context should include bootstrap_files list."""
    captured = []

    async def capture_handler(event: InternalHookEvent):
        captured.append(event)

    register_internal_hook("agent:bootstrap", capture_handler)

    event = InternalHookEvent.create(
        "agent",
        "bootstrap",
        "test-session",
        {
            "workspace_dir": "/tmp/workspace",
            "session_key": "test-session",
            "bootstrap_files": [
                {"path": "AGENTS.md", "content": "Be helpful"},
            ],
        },
    )
    await trigger_internal_hook(event)

    assert len(captured) == 1
    assert "bootstrap_files" in captured[0].context
    files = captured[0].context["bootstrap_files"]
    assert len(files) == 1
    assert files[0]["path"] == "AGENTS.md"


@pytest.mark.asyncio
async def test_bootstrap_handler_can_inject_files():
    """Handler should be able to append to bootstrap_files."""
    async def inject_handler(event: InternalHookEvent):
        files = event.context.setdefault("bootstrap_files", [])
        files.append({"path": "REMINDER.md", "content": "Remember to be concise", "virtual": True})

    register_internal_hook("agent:bootstrap", inject_handler)

    event = InternalHookEvent.create(
        "agent",
        "bootstrap",
        "test-session",
        {
            "workspace_dir": "/tmp/workspace",
            "session_key": "test-session",
            "bootstrap_files": [],
        },
    )
    await trigger_internal_hook(event)

    files = event.context["bootstrap_files"]
    assert len(files) == 1
    assert files[0]["path"] == "REMINDER.md"
    assert files[0]["virtual"] is True


@pytest.mark.asyncio
async def test_bootstrap_handler_can_modify_existing_files():
    """Handler should be able to modify existing bootstrap file entries."""
    async def modify_handler(event: InternalHookEvent):
        files = event.context.get("bootstrap_files", [])
        for f in files:
            if f["path"] == "AGENTS.md":
                f["content"] = f["content"] + "\n\n## Extra\n- Be safe"

    register_internal_hook("agent:bootstrap", modify_handler)

    event = InternalHookEvent.create(
        "agent",
        "bootstrap",
        "test-session",
        {
            "workspace_dir": "/tmp/workspace",
            "session_key": "test-session",
            "bootstrap_files": [{"path": "AGENTS.md", "content": "Be helpful"}],
        },
    )
    await trigger_internal_hook(event)

    files = event.context["bootstrap_files"]
    assert "Extra" in files[0]["content"]
