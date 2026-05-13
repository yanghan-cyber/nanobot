"""Integration tests for Dream Phase 1/2 staging functionality."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.memory import Dream, DreamAudit, MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(tmp_path)
    # Pre-populate some history entries
    for content in ["user discussed MaxKB RAG", "user mentioned rerank models"]:
        s.append_history(content)
    return s


@pytest.fixture
def mock_provider() -> MagicMock:
    provider = MagicMock()
    provider.generation = MagicMock(max_tokens=4096)
    return provider


@pytest.fixture
def dream(store: MemoryStore, mock_provider: MagicMock) -> Dream:
    return Dream(store=store, provider=mock_provider, model="test-model")


def test_dream_phase1_input_includes_staging(dream: Dream, store: MemoryStore):
    """Verify Phase 1 input contains staging.md content."""
    store.write_staging("# Staging\n\n### Topic\n- [2026-05-13] test | seen:1 | age:0d\n")
    staging_content = store.read_staging()
    assert "### Topic" in staging_content


def test_dream_write_tool_includes_memory_dir(dream: Dream):
    """Verify Dream Phase 2 has write_file tool registered."""
    tool_names = dream._tools.tool_names
    assert "write_file" in tool_names


@pytest.mark.asyncio
async def test_dream_staging_new_entry(store: MemoryStore, mock_provider: MagicMock):
    """Dream Phase 1 produces analysis and Phase 2 writes to staging.md."""
    store.write_staging("")

    # Phase 1 returns analysis
    phase1_response = MagicMock()
    phase1_response.content = "[STAGING-NEW] topic: Test Topic\n- a new observation"
    phase1_response.finish_reason = "stop"

    # Phase 2 agent result
    phase2_result = MagicMock()
    phase2_result.stop_reason = "completed"
    phase2_result.tool_events = [
        {"name": "write_file", "status": "ok", "detail": "wrote memory/staging.md"},
    ]

    mock_provider.chat_with_retry = AsyncMock(return_value=phase1_response)

    dream = Dream(store=store, provider=mock_provider, model="test-model")
    with patch.object(dream._runner, "run", AsyncMock(return_value=phase2_result)):
        result = await dream.run()

    assert result is True


@pytest.mark.asyncio
async def test_dream_returns_false_when_no_history(mock_provider: MagicMock, tmp_path: Path):
    """Dream returns False when there are no unprocessed history entries."""
    store = MemoryStore(tmp_path)
    dream = Dream(store=store, provider=mock_provider, model="test-model")
    result = await dream.run()
    assert result is False


@pytest.mark.asyncio
async def test_dream_audit_run_with_files(store: MemoryStore, mock_provider: MagicMock):
    """DreamAudit processes files when they have content."""
    store.write_memory("# Memory\n- test fact")
    store.write_staging("# Staging\n\n### Topic\n- item | seen:1 | age:0d\n")

    phase1_response = MagicMock()
    phase1_response.content = "[MEMORY-EDIT] - test fact -> - test fact (verified)"

    phase2_result = MagicMock()
    phase2_result.stop_reason = "completed"
    phase2_result.tool_events = [
        {"name": "edit_file", "status": "ok", "detail": "edited memory/MEMORY.md"},
    ]

    mock_provider.chat_with_retry = AsyncMock(return_value=phase1_response)

    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    with patch.object(audit._runner, "run", AsyncMock(return_value=phase2_result)):
        result = await audit.run()

    assert result is True


@pytest.mark.asyncio
async def test_dream_audit_skips_empty_files(store: MemoryStore, mock_provider: MagicMock):
    """DreamAudit returns False when all files are empty."""
    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    result = await audit.run()
    assert result is False


@pytest.mark.asyncio
async def test_dream_audit_returns_false_on_no_tool_events(
    store: MemoryStore, mock_provider: MagicMock,
):
    """DreamAudit returns False when Phase 2 produces no successful tool events."""
    store.write_memory("# Memory\n- test fact")

    phase1_response = MagicMock()
    phase1_response.content = "No changes needed"

    phase2_result = MagicMock()
    phase2_result.stop_reason = "completed"
    phase2_result.tool_events = []

    mock_provider.chat_with_retry = AsyncMock(return_value=phase1_response)

    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    with patch.object(audit._runner, "run", AsyncMock(return_value=phase2_result)):
        result = await audit.run()

    assert result is False


@pytest.mark.asyncio
async def test_dream_staging_content_in_phase1_prompt(store: MemoryStore, mock_provider: MagicMock):
    """Dream Phase 1 prompt includes staging.md content."""
    store.write_staging("# Staging\n\n### Topic\n- [2026-05-13] observation | seen:2 | age:1d\n")

    phase1_response = MagicMock()
    phase1_response.content = "analysis"
    phase1_response.finish_reason = "stop"

    phase2_result = MagicMock()
    phase2_result.stop_reason = "completed"
    phase2_result.tool_events = []

    mock_provider.chat_with_retry = AsyncMock(return_value=phase1_response)

    dream = Dream(store=store, provider=mock_provider, model="test-model")
    with patch.object(dream._runner, "run", AsyncMock(return_value=phase2_result)):
        await dream.run()

    # Verify Phase 1 was called and its user content includes staging content
    call_args = mock_provider.chat_with_retry.call_args
    messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
    user_msg = messages[1]["content"]
    assert "### Topic" in user_msg
