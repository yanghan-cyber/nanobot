"""Tests for staging config fields on DreamConfig."""

from nanobot.config.schema import DreamConfig


def test_default_staging_promotion_threshold():
    cfg = DreamConfig()
    assert cfg.staging_promotion_threshold == 3


def test_default_audit_cron():
    cfg = DreamConfig()
    assert cfg.audit_cron == "0 3 * * *"


def test_default_audit_model_override_is_none():
    cfg = DreamConfig()
    assert cfg.audit_model_override is None


def test_default_audit_max_iterations():
    cfg = DreamConfig()
    assert cfg.audit_max_iterations == 15


def test_build_audit_schedule():
    cfg = DreamConfig()
    schedule = cfg.build_audit_schedule("UTC")
    assert schedule.kind == "cron"
    assert schedule.expr == "0 3 * * *"
    assert schedule.tz == "UTC"


def test_custom_audit_cron():
    cfg = DreamConfig(audit_cron="0 5 * * *")
    schedule = cfg.build_audit_schedule("Asia/Shanghai")
    assert schedule.expr == "0 5 * * *"
    assert schedule.tz == "Asia/Shanghai"


def test_camel_case_aliases():
    """Config JSON uses camelCase; verify aliases resolve."""
    cfg = DreamConfig(stagingPromotionThreshold=5, auditMaxIterations=20)
    assert cfg.staging_promotion_threshold == 5
    assert cfg.audit_max_iterations == 20


# ---------------------------------------------------------------------------
# MemoryStore staging file I/O and metadata stripping (Task 2)
# ---------------------------------------------------------------------------

from pathlib import Path

import pytest

from nanobot.agent.memory import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path)


def test_read_staging_empty(store: MemoryStore):
    assert store.read_staging() == ""


def test_write_and_read_staging(store: MemoryStore):
    store.write_staging("# Staging\n\n### Topic\n- item")
    assert "### Topic" in store.read_staging()


def test_get_staging_context_empty(store: MemoryStore):
    assert store.get_staging_context() == ""


def test_get_staging_context_strips_metadata(store: MemoryStore):
    store.write_staging(
        "# Staging\n\n"
        "### MaxKB RAG\n"
        "- [2026-05-13] blend 模式搜索效果不佳 | seen:2 | age:1d\n"
        "- [2026-05-12] 决定先用 blend 模式 | seen:3 | age:2d\n\n"
        "### Other\n"
        "- [2026-05-10] some fact | seen:1 | age:3d\n"
    )
    ctx = store.get_staging_context()
    assert "# Short-term Memory" in ctx
    assert "### MaxKB RAG" in ctx
    assert "blend 模式搜索效果不佳" in ctx
    # Metadata should be stripped
    assert "seen:" not in ctx
    assert "age:" not in ctx
    assert "[2026-" not in ctx


def test_get_staging_context_preserves_section_headers(store: MemoryStore):
    store.write_staging("# Staging\n\n### Topic A\n- a | seen:1 | age:0d\n\n### Topic B\n- b | seen:2 | age:1d\n")
    ctx = store.get_staging_context()
    assert "### Topic A" in ctx
    assert "### Topic B" in ctx


def test_get_staging_context_plain_entry_no_pipes(store: MemoryStore):
    """Entries without pipe separators are preserved as-is."""
    store.write_staging("# Staging\n\n### Topic\n- plain entry without metadata\n")
    ctx = store.get_staging_context()
    assert "- plain entry without metadata" in ctx


def test_staging_file_attribute(store: MemoryStore):
    assert store.staging_file == store.memory_dir / "staging.md"
