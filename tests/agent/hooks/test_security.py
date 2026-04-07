"""Tests for hook path security."""

from pathlib import Path

from nanobot.agent.hooks.security import validate_hook_path


def test_valid_path():
    hook_dir = Path("/workspace/hooks/my-hook")
    handler = Path("/workspace/hooks/my-hook/handler.py")
    assert validate_hook_path(hook_dir, handler)


def test_nested_valid_path():
    hook_dir = Path("/workspace/hooks/my-hook")
    handler = Path("/workspace/hooks/my-hook/src/deep/handler.py")
    assert validate_hook_path(hook_dir, handler)


def test_escape_attempt():
    hook_dir = Path("/workspace/hooks/my-hook")
    handler = Path("/workspace/hooks/my-hook/../../etc/passwd")
    assert not validate_hook_path(hook_dir, handler)


def test_absolute_escape():
    hook_dir = Path("/workspace/hooks/my-hook")
    handler = Path("/etc/passwd")
    assert not validate_hook_path(hook_dir, handler)


def test_same_directory():
    hook_dir = Path("/workspace/hooks/my-hook")
    handler = Path("/workspace/hooks/my-hook")
    assert validate_hook_path(hook_dir, handler)
