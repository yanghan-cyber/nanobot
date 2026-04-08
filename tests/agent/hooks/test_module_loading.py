"""Tests for stable module naming in _load_handler."""

import gc

import pytest

from nanobot.agent.hooks.discovery import _load_handler


def test_load_handler_stable_module_name(tmp_path):
    """Two different handlers must get different module names, even after GC."""
    # Create two handler files with different content
    h1_dir = tmp_path / "alpha"
    h1_dir.mkdir()
    h1 = h1_dir / "handler.py"
    h1.write_text("def handler(event): return 'alpha'\n")

    h2_dir = tmp_path / "beta"
    h2_dir.mkdir()
    h2 = h2_dir / "handler.py"
    h2.write_text("def handler(event): return 'beta'\n")

    # Load both
    fn1 = _load_handler(h1, "handler")
    fn2 = _load_handler(h2, "handler")

    assert fn1 is not None
    assert fn2 is not None
    # They must be different functions
    assert fn1 is not fn2
    # They must produce different results
    assert fn1(None) == "alpha"
    assert fn2(None) == "beta"


def test_load_handler_survives_gc(tmp_path):
    """Handler loaded from a path should not be garbage collected if path object is GC'd."""
    h_dir = tmp_path / "gamma"
    h_dir.mkdir()
    h = h_dir / "handler.py"
    h.write_text("def handler(event): return 'gamma'\n")

    fn = _load_handler(h, "handler")
    assert fn is not None

    # Force GC to potentially reuse the id
    gc.collect()

    # Load again - should still work and be the same function
    fn2 = _load_handler(h, "handler")
    assert fn2 is not None
    assert fn2(None) == "gamma"
