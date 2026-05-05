from __future__ import annotations

from nanobot.session.title import clean_title


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
