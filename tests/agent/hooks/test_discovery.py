"""Tests for hook discovery and loading."""

import pytest
from pathlib import Path

from nanobot.agent.hooks.discovery import discover_hooks, load_hooks
from nanobot.agent.hooks.registry import clear_internal_hooks, has_listeners
from nanobot.agent.hooks.events import InternalHookEvent
from nanobot.config.schema import HooksConfig


@pytest.fixture(autouse=True)
def clean_registry():
    clear_internal_hooks()
    yield
    clear_internal_hooks()


def test_discover_empty_dir(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    assert discover_hooks(hooks_dir) == []


def test_discover_no_hook_md(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hook_dir = hooks_dir / "my-hook"
    hook_dir.mkdir(parents=True)
    (hook_dir / "handler.py").write_text("def handler(event): pass")
    assert discover_hooks(hooks_dir) == []


def test_discover_valid_hook(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hook_dir = hooks_dir / "test-hook"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text(
        '---\nname: test-hook\nmetadata: {"events": ["agent:bootstrap"]}\n---\n'
    )
    (hook_dir / "handler.py").write_text(
        "async def handler(event): pass\n"
    )
    entries = discover_hooks(hooks_dir)
    assert len(entries) == 1
    assert entries[0]["name"] == "test-hook"
    assert entries[0]["handler_path"] == hook_dir / "handler.py"


def test_discover_prefers_handler_py_over_index(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hook_dir = hooks_dir / "hook1"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text('---\nname: hook1\nmetadata: {"events": ["agent:bootstrap"]}\n---\n')
    (hook_dir / "handler.py").write_text("def handler(event): pass\n")
    (hook_dir / "index.py").write_text("def handler(event): pass\n")
    entries = discover_hooks(hooks_dir)
    assert entries[0]["handler_path"].name == "handler.py"


def test_discover_fallback_index_py(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hook_dir = hooks_dir / "hook2"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text('---\nname: hook2\nmetadata: {"events": ["agent:bootstrap"]}\n---\n')
    (hook_dir / "index.py").write_text("def handler(event): pass\n")
    entries = discover_hooks(hooks_dir)
    assert entries[0]["handler_path"].name == "index.py"


def test_discover_skips_dotdirs(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hidden = hooks_dir / ".hidden"
    hidden.mkdir(parents=True)
    (hidden / "HOOK.md").write_text('---\nname: hidden\nmetadata: {"events": ["agent:bootstrap"]}\n---\n')
    (hidden / "handler.py").write_text("def handler(event): pass\n")
    assert discover_hooks(hooks_dir) == []


def test_discover_nonexistent_dir(tmp_path):
    assert discover_hooks(tmp_path / "nope") == []


@pytest.mark.asyncio
async def test_load_hooks_registers_handlers(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hook_dir = hooks_dir / "greeter"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text(
        '---\nname: greeter\nmetadata: {"events": ["agent:bootstrap"]}\n---\n'
    )
    (hook_dir / "handler.py").write_text(
        "async def handler(event):\n    event.context['greeted'] = True\n"
    )
    count = await load_hooks(hooks_dir)
    assert count == 1
    assert has_listeners("agent", "bootstrap")


@pytest.mark.asyncio
async def test_load_hooks_handler_is_called(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hook_dir = hooks_dir / "adder"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text(
        '---\nname: adder\nmetadata: {"events": ["agent:bootstrap"]}\n---\n'
    )
    (hook_dir / "handler.py").write_text(
        "async def handler(event):\n    event.context.setdefault('files', []).append('added')\n"
    )
    await load_hooks(hooks_dir)

    from nanobot.agent.hooks.registry import trigger_internal_hook
    event = InternalHookEvent.create("agent", "bootstrap", "s1")
    await trigger_internal_hook(event)
    assert event.context["files"] == ["added"]


@pytest.mark.asyncio
async def test_load_hooks_multiple_events(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hook_dir = hooks_dir / "multi"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text(
        '---\nname: multi\nmetadata: {"events": ["agent:bootstrap", "message:received"]}\n---\n'
    )
    (hook_dir / "handler.py").write_text(
        "async def handler(event):\n    pass\n"
    )
    count = await load_hooks(hooks_dir)
    assert count == 1
    assert has_listeners("agent", "bootstrap")
    assert has_listeners("message", "received")


@pytest.mark.asyncio
async def test_load_hooks_bad_handler_skipped(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hook_dir = hooks_dir / "bad"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text(
        '---\nname: bad\nmetadata: {"events": ["agent:bootstrap"]}\n---\n'
    )
    (hook_dir / "handler.py").write_text("not valid python {{{")
    count = await load_hooks(hooks_dir)
    assert count == 0


@pytest.mark.asyncio
async def test_load_hooks_no_events_skipped(tmp_path):
    hooks_dir = tmp_path / "hooks"
    hook_dir = hooks_dir / "noevents"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.md").write_text('---\nname: noevents\n---\n')
    (hook_dir / "handler.py").write_text("def handler(event): pass\n")
    count = await load_hooks(hooks_dir)
    assert count == 0


# --- HooksConfig filtering tests ---


def _make_hook(hooks_dir: Path, name: str, events: list[str] | None = None) -> Path:
    """Helper to create a minimal hook directory."""
    hook_dir = hooks_dir / name
    hook_dir.mkdir(parents=True, exist_ok=True)
    ev_json = str(events or ["agent:bootstrap"]).replace("'", '"')
    (hook_dir / "HOOK.md").write_text(
        f'---\nname: {name}\nmetadata: {{"events": {ev_json}}}\n---\n'
    )
    (hook_dir / "handler.py").write_text("async def handler(event): pass\n")
    return hook_dir


@pytest.mark.asyncio
async def test_load_hooks_enabled_false(tmp_path):
    """When cfg.enabled=False, no hooks should load."""
    hooks_dir = tmp_path / "hooks"
    _make_hook(hooks_dir, "hook-a")
    cfg = HooksConfig(enabled=False)
    count = await load_hooks(hooks_dir, cfg=cfg)
    assert count == 0
    assert not has_listeners("agent", "bootstrap")


@pytest.mark.asyncio
async def test_load_hooks_allow_filter(tmp_path):
    """When cfg.allow is set, only listed hooks load."""
    hooks_dir = tmp_path / "hooks"
    _make_hook(hooks_dir, "hook-a")
    _make_hook(hooks_dir, "hook-b")
    cfg = HooksConfig(allow=["hook-a"])
    count = await load_hooks(hooks_dir, cfg=cfg)
    assert count == 1


@pytest.mark.asyncio
async def test_load_hooks_deny_filter(tmp_path):
    """When cfg.deny is set, denied hooks are skipped."""
    hooks_dir = tmp_path / "hooks"
    _make_hook(hooks_dir, "hook-a")
    _make_hook(hooks_dir, "hook-b")
    cfg = HooksConfig(deny=["hook-a"])
    count = await load_hooks(hooks_dir, cfg=cfg)
    assert count == 1


@pytest.mark.asyncio
async def test_load_hooks_allow_empty_loads_all(tmp_path):
    """When cfg.allow is empty (default), all hooks load."""
    hooks_dir = tmp_path / "hooks"
    _make_hook(hooks_dir, "hook-a")
    _make_hook(hooks_dir, "hook-b")
    cfg = HooksConfig(allow=[])
    count = await load_hooks(hooks_dir, cfg=cfg)
    assert count == 2


@pytest.mark.asyncio
async def test_load_hooks_dirs_scans_extra_directories(tmp_path):
    """When cfg.dirs contains extra paths, those directories are also scanned."""
    # Primary hooks dir
    hooks_dir = tmp_path / "hooks"
    _make_hook(hooks_dir, "primary-hook")
    # Extra hooks dir
    extra_dir = tmp_path / "extra-hooks"
    _make_hook(extra_dir, "extra-hook")
    cfg = HooksConfig(dirs=[str(extra_dir)])
    count = await load_hooks(hooks_dir, cfg=cfg)
    assert count == 2
    assert has_listeners("agent", "bootstrap")


@pytest.mark.asyncio
async def test_load_hooks_deny_overrides_allow(tmp_path):
    """When a hook is in both allow and deny, deny wins."""
    hooks_dir = tmp_path / "hooks"
    _make_hook(hooks_dir, "hook-a")
    _make_hook(hooks_dir, "hook-b")
    cfg = HooksConfig(allow=["hook-a", "hook-b"], deny=["hook-a"])
    count = await load_hooks(hooks_dir, cfg=cfg)
    assert count == 1
