"""Tests for InternalHook registry."""

import pytest

from nanobot.agent.hooks.events import InternalHookEvent
from nanobot.agent.hooks.registry import (
    clear_internal_hooks,
    has_listeners,
    register_internal_hook,
    trigger_internal_hook,
    unregister_internal_hook,
)


@pytest.fixture(autouse=True)
def clean_registry():
    clear_internal_hooks()
    yield
    clear_internal_hooks()


def test_register_and_check():
    async def handler(event): ...

    register_internal_hook("agent:bootstrap", handler)
    assert has_listeners("agent", "bootstrap")


def test_no_listeners():
    assert not has_listeners("agent", "bootstrap")


def test_unregister():
    async def handler(event): ...

    register_internal_hook("agent:bootstrap", handler)
    unregister_internal_hook("agent:bootstrap", handler)
    assert not has_listeners("agent", "bootstrap")


def test_unregister_nonexistent():
    async def handler(event): ...

    unregister_internal_hook("agent:bootstrap", handler)  # should not raise


@pytest.mark.asyncio
async def test_trigger_calls_handler():
    calls = []
    async def handler(event):
        calls.append(event.session_key)

    register_internal_hook("agent:bootstrap", handler)
    event = InternalHookEvent.create("agent", "bootstrap", "s1")
    await trigger_internal_hook(event)
    assert calls == ["s1"]


@pytest.mark.asyncio
async def test_trigger_general_then_specific():
    order = []
    async def general(event):
        order.append("general")
    async def specific(event):
        order.append("specific")

    register_internal_hook("agent", general)
    register_internal_hook("agent:bootstrap", specific)
    event = InternalHookEvent.create("agent", "bootstrap", "s1")
    await trigger_internal_hook(event)
    assert order == ["general", "specific"]


@pytest.mark.asyncio
async def test_trigger_error_isolation():
    """One handler failure does not block others."""
    calls = []
    async def bad(event):
        raise RuntimeError("boom")
    async def good(event):
        calls.append("ok")

    register_internal_hook("agent:bootstrap", bad)
    register_internal_hook("agent:bootstrap", good)
    event = InternalHookEvent.create("agent", "bootstrap", "s1")
    await trigger_internal_hook(event)
    assert calls == ["ok"]


@pytest.mark.asyncio
async def test_trigger_no_listeners_is_noop():
    event = InternalHookEvent.create("agent", "bootstrap", "s1")
    await trigger_internal_hook(event)  # should not raise


@pytest.mark.asyncio
async def test_trigger_sync_handler():
    calls = []
    def handler(event):
        calls.append("sync")

    register_internal_hook("agent:bootstrap", handler)
    event = InternalHookEvent.create("agent", "bootstrap", "s1")
    await trigger_internal_hook(event)
    assert calls == ["sync"]


@pytest.mark.asyncio
async def test_trigger_mutates_context():
    async def handler(event):
        event.context["added"] = True

    register_internal_hook("agent:bootstrap", handler)
    event = InternalHookEvent.create("agent", "bootstrap", "s1", {"original": 1})
    await trigger_internal_hook(event)
    assert event.context == {"original": 1, "added": True}
