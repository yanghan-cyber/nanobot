"""Session title utilities."""

from __future__ import annotations

_MAX_TITLE_LEN = 80


def clean_title(raw: str) -> str:
    """Sanitize a raw LLM title output."""
    if not isinstance(raw, str):
        return ""
    title = raw.strip().strip('"').strip("'").strip()
    if not title:
        return ""
    for _ in range(20):
        if not title.endswith((".", "!", "?")):
            break
        title = title[:-1].strip()
    if len(title) > _MAX_TITLE_LEN:
        title = title[:_MAX_TITLE_LEN - 3] + "..."
    return title
