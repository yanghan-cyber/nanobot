from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.session.title import auto_title, clean_title


class TestCleanTitle:
    def test_strips_quotes(self) -> None:
        assert clean_title('"Debug API"') == "Debug API"

    def test_strips_single_quotes(self) -> None:
        assert clean_title("'Debug API'") == "Debug API"

    def test_removes_trailing_punctuation(self) -> None:
        assert clean_title("Debug API.") == "Debug API"
        assert clean_title("Debug API!") == "Debug API"
        assert clean_title("Debug API?") == "Debug API"

    def test_truncates_long_title(self) -> None:
        long_title = "A" * 100
        result = clean_title(long_title)
        assert len(result) == 80
        assert result.endswith("...")

    def test_returns_empty_for_empty(self) -> None:
        assert clean_title("") == ""
        assert clean_title("   ") == ""

    def test_preserves_normal_title(self) -> None:
        assert clean_title("Deploy Docker Containers") == "Deploy Docker Containers"


class TestAutoTitle:
    @pytest.mark.asyncio
    async def test_auto_title_returns_cleaned(self) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(
            return_value=MagicMock(content='"Fix authentication bug"')
        )
        result = await auto_title(provider, "gpt-4", "Fix the login bug", "I found the issue")
        assert result == "Fix authentication bug"

    @pytest.mark.asyncio
    async def test_auto_title_truncates_long_input(self) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(
            return_value=MagicMock(content="Some title")
        )
        long_content = "x" * 1000
        await auto_title(provider, "gpt-4", long_content, long_content)
        assert provider.chat_with_retry.called

    @pytest.mark.asyncio
    async def test_auto_title_handles_list_content(self) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(
            return_value=MagicMock(content="Test Title")
        )
        user_content = [{"type": "text", "text": "Hello world"}]
        result = await auto_title(provider, "gpt-4", user_content, "Hi")
        assert result == "Test Title"

    @pytest.mark.asyncio
    async def test_auto_title_returns_none_on_failure(self) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(side_effect=Exception("API error"))
        result = await auto_title(provider, "gpt-4", "Hello", "Hi")
        assert result is None

    @pytest.mark.asyncio
    async def test_auto_title_returns_none_on_empty_response(self) -> None:
        provider = MagicMock()
        provider.chat_with_retry = AsyncMock(
            return_value=MagicMock(content="")
        )
        result = await auto_title(provider, "gpt-4", "Hello", "Hi")
        assert result is None
