"""End-to-end integration test for the hooks system."""

import pytest
from pathlib import Path

from nanobot.agent.hooks import (
    InternalHookEvent,
    clear_internal_hooks,
    has_listeners,
    load_hooks,
    register_internal_hook,
    trigger_internal_hook,
)


@pytest.fixture(autouse=True)
def clean():
    clear_internal_hooks()
    yield
    clear_internal_hooks()


@pytest.mark.asyncio
async def test_full_lifecycle(tmp_path):
    """Simulate: discover hook from disk -> register -> trigger -> verify mutation."""

    # 1. Create hook on disk
    hook_dir = tmp_path / "hooks" / "test-hook"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text(
        '---\nname: test-hook\n'
        'metadata: {"events": ["agent:bootstrap"]}\n---\n'
    )
    (hook_dir / "handler.py").write_text(
        "async def handler(event):\n"
        "    files = event.context.setdefault('bootstrap_files', [])\n"
        "    files.append({'path': 'REMINDER.md', 'content': 'Remember!', 'virtual': True})\n"
    )

    # 2. Load from directory
    count = await load_hooks(tmp_path / "hooks")
    assert count == 1
    assert has_listeners("agent", "bootstrap")

    # 3. Trigger event
    event = InternalHookEvent.create("agent", "bootstrap", "cli:direct")
    await trigger_internal_hook(event)

    # 4. Verify handler mutated context
    assert len(event.context["bootstrap_files"]) == 1
    assert event.context["bootstrap_files"][0]["path"] == "REMINDER.md"


@pytest.mark.asyncio
async def test_programmatic_and_discovered_hooks_coexist(tmp_path):
    """Both programmatic and discovered hooks should run."""

    # Programmatic hook
    prog_calls = []
    async def prog_handler(event):
        prog_calls.append("prog")

    register_internal_hook("message:received", prog_handler)

    # Discovered hook
    hook_dir = tmp_path / "hooks" / "disk-hook"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text(
        '---\nname: disk-hook\n'
        'metadata: {"events": ["message:received"]}\n---\n'
    )
    (hook_dir / "handler.py").write_text(
        "async def handler(event):\n"
        "    event.messages.append('from disk')\n"
    )
    await load_hooks(tmp_path / "hooks")

    # Trigger
    event = InternalHookEvent.create("message", "received", "s1", {"content": "hi"})
    await trigger_internal_hook(event)

    assert prog_calls == ["prog"]
    assert event.messages == ["from disk"]


@pytest.mark.asyncio
async def test_hook_with_openclaw_metadata_format(tmp_path):
    """Test compatibility with OpenClaw-style metadata.openclaw.events format."""

    hook_dir = tmp_path / "hooks" / "oc-hook"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text(
        '---\nname: oc-hook\n'
        'metadata: {"openclaw":{"events":["agent:bootstrap"]}}\n---\n'
    )
    (hook_dir / "handler.py").write_text(
        "async def handler(event):\n"
        "    event.context['oc'] = True\n"
    )
    count = await load_hooks(tmp_path / "hooks")
    assert count == 1
    assert has_listeners("agent", "bootstrap")

    event = InternalHookEvent.create("agent", "bootstrap", "s1")
    await trigger_internal_hook(event)
    assert event.context.get("oc") is True
