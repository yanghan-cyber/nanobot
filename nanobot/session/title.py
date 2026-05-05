"""Auto-title generation for conversation sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

from loguru import logger

_SYSTEM_PROMPT = (
    "Generate a short descriptive title (3-7 words) for a conversation "
    "starting with the following exchange. Return ONLY the title, "
    "no quotes, no punctuation at the end."
)

_MAX_TITLE_LEN = 80


def clean_title(raw: str) -> str:
    """Sanitize a raw LLM title output."""
    title = raw.strip().strip('"').strip("'").strip()
    if not title:
        return ""
    while title.endswith((".", "!", "?")):
        title = title[:-1].strip()
    if len(title) > _MAX_TITLE_LEN:
        title = title[:_MAX_TITLE_LEN - 3] + "..."
    return title


def _extract_text(content: object, max_len: int = 500) -> str:
    """Extract a text string from various content formats."""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        text = " ".join(parts)
    else:
        text = str(content)
    return text[:max_len]


async def auto_title(
    provider: LLMProvider,
    model: str,
    user_content: object,
    assistant_content: object,
) -> str | None:
    """Generate a session title from the first user-assistant exchange."""
    user_text = _extract_text(user_content)
    assistant_text = _extract_text(assistant_content)
    if not user_text:
        return None

    try:
        response = await provider.chat_with_retry(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"User: {user_text}\nAssistant: {assistant_text}"},
            ],
        )
        content = response.content if response else None
        if not content:
            return None
        return clean_title(content) or None
    except Exception:
        logger.debug("Auto-title generation failed", exc_info=True)
        return None
