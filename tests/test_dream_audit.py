"""Tests for DreamAudit class."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.memory import DreamAudit, MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path)


@pytest.fixture
def mock_provider() -> MagicMock:
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock()
    provider.generation = MagicMock(max_tokens=4096)
    return provider


def test_dream_audit_instantiation(store: MemoryStore, mock_provider: MagicMock):
    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    assert audit.store is store
    assert audit.model == "test-model"


def test_dream_audit_build_tools(store: MemoryStore, mock_provider: MagicMock):
    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    tools = audit._tools
    tool_names = set(tools.tool_names)
    assert "read_file" in tool_names
    assert "edit_file" in tool_names
    # Audit only does targeted edits, no write_file
    assert "write_file" not in tool_names


@pytest.mark.asyncio
async def test_dream_audit_run_no_changes(store: MemoryStore, mock_provider: MagicMock):
    """Audit with empty files does nothing and returns False."""
    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    result = await audit.run()
    assert result is False


def test_dream_audit_set_provider(store: MemoryStore, mock_provider: MagicMock):
    audit = DreamAudit(store=store, provider=mock_provider, model="test-model")
    new_provider = MagicMock()
    audit.set_provider(new_provider, "new-model")
    assert audit.provider is new_provider
    assert audit.model == "new-model"
