"""SessionSearchTool: search and browse past conversation sessions."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.providers.base import LLMProvider
from nanobot.session.db import SessionDB

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Search keywords for full-text search across all stored messages. "
                "Supports FTS5 syntax: quoted phrases (\"exact match\"), AND/OR operators. "
                "Omit to list recent sessions instead."
            ),
        },
        "role_filter": {
            "type": "string",
            "description": (
                "Filter messages by role before searching. "
                "Comma-separated: 'user', 'assistant', 'tool'. "
                "Example: 'user' to only search user messages."
            ),
        },
        "limit": {
            "type": "integer",
            "description": "Max sessions to return (1-20, default 5).",
            "default": 5,
        },
    },
}

_MAX_SESSION_CHARS = 100_000
_PREVIEW_MAX_LENGTH = 80

_RUNTIME_CTX_START = "[Runtime Context"
_RUNTIME_CTX_END = "[/Runtime Context]"


def _strip_runtime_context(text: str) -> str:
    """Remove [Runtime Context]...\n[/Runtime Context] blocks from message content."""
    result = text
    while True:
        start = result.find(_RUNTIME_CTX_START)
        if start == -1:
            break
        end = result.find(_RUNTIME_CTX_END, start)
        if end == -1:
            break
        result = result[:start] + result[end + len(_RUNTIME_CTX_END):]
    return result.strip()


def _make_preview(content: str | None, max_len: int = _PREVIEW_MAX_LENGTH) -> str:
    """Generate a clean preview from user message content."""
    if not content:
        return ""
    cleaned = _strip_runtime_context(content)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        return cleaned[: max_len - 1] + "…"
    return cleaned


def _format_conversation(messages: list[dict[str, Any]]) -> str:
    """Format session messages into a readable transcript for summarization."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content") or ""
        tool_name = msg.get("tool_name")

        if role == "TOOL" and tool_name:
            if len(content) > 500:
                content = content[:250] + "\n...[truncated]...\n" + content[-250:]
            parts.append(f"[TOOL:{tool_name}]: {content}")
        elif role == "ASSISTANT":
            tool_calls = msg.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                tc_names = []
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        name = tc.get("name") or tc.get("function", {}).get("name", "?")
                        tc_names.append(name)
                if tc_names:
                    parts.append(f"[ASSISTANT]: [Called: {', '.join(tc_names)}]")
                if content:
                    parts.append(f"[ASSISTANT]: {content}")
            else:
                parts.append(f"[ASSISTANT]: {content}")
        else:
            parts.append(f"[{role}]: {content}")

    return "\n\n".join(parts)


def _truncate_around_matches(full_text: str, query: str, max_chars: int = _MAX_SESSION_CHARS) -> str:
    """Truncate transcript to max_chars, centering the window on query matches."""
    if len(full_text) <= max_chars:
        return full_text

    text_lower = full_text.lower()
    query_lower = query.lower().strip()
    match_positions: list[int] = []

    # 1. Full-phrase search
    phrase_pat = re.compile(re.escape(query_lower))
    match_positions = [m.start() for m in phrase_pat.finditer(text_lower)]

    # 2. Proximity co-occurrence of all terms (within 200 chars)
    if not match_positions:
        terms = query_lower.split()
        if len(terms) > 1:
            from bisect import bisect_left, bisect_right
            term_positions: dict[str, list[int]] = {}
            for t in terms:
                term_positions[t] = sorted(
                    m.start() for m in re.finditer(re.escape(t), text_lower)
                )
            rarest = min(terms, key=lambda t: len(term_positions.get(t, [])))
            for pos in term_positions.get(rarest, []):
                if all(
                    bisect_left(term_positions.get(t, []), pos - 200)
                    < bisect_right(term_positions.get(t, []), pos + 200)
                    for t in terms
                    if t != rarest
                ):
                    match_positions.append(pos)

    # 3. Individual term positions (last resort)
    if not match_positions:
        terms = query_lower.split()
        for t in terms:
            for m in re.finditer(re.escape(t), text_lower):
                match_positions.append(m.start())

    if not match_positions:
        truncated = full_text[:max_chars]
        suffix = "\n\n...[later conversation truncated]..." if max_chars < len(full_text) else ""
        return truncated + suffix

    # Pick window that covers the most match positions — O(n) sliding window
    match_positions.sort()
    from bisect import bisect_left, bisect_right
    best_start = 0
    best_count = 0
    n = len(match_positions)
    right = 0
    for left in range(n):
        anchor = match_positions[left]
        ws = max(0, anchor - max_chars // 4)
        we = ws + max_chars
        if we > len(full_text):
            ws = max(0, len(full_text) - max_chars)
            we = len(full_text)
        right = bisect_right(match_positions, we - 1, lo=left)
        count = right - left
        if count > best_count:
            best_count = count
            best_start = ws

    start = best_start
    end = min(len(full_text), start + max_chars)

    truncated = full_text[start:end]
    prefix = "...[earlier conversation truncated]...\n\n" if start > 0 else ""
    suffix = "\n\n...[later conversation truncated]..." if end < len(full_text) else ""
    return prefix + truncated + suffix


class SessionSearchTool(Tool):
    """Search past sessions by keyword (FTS5) or list recent ones."""

    def __init__(
        self, db: SessionDB, provider: LLMProvider, model: str
    ) -> None:
        self._db = db
        self._provider = provider
        self._model = model

    @property
    def name(self) -> str:
        return "session_search"

    @property
    def description(self) -> str:
        return (
            "Search past conversation sessions to recall topics, decisions, or context.\n"
            "Modes:\n"
            "1. No query — list recent sessions with previews.\n"
            "2. With query — full-text search across messages, "
            "then summarize each matching session.\n"
            "Use this when the user asks about past conversations, "
            "wants to recall previous discussions, or needs context from earlier sessions."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _SCHEMA.copy()

    @property
    def schema(self) -> dict[str, Any]:
        return self.to_schema()["function"]

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        *,
        query: str | None = None,
        role_filter: str | None = None,
        limit: int = 5,
        **_kwargs: Any,
    ) -> str:
        limit = max(1, min(20, limit))
        from nanobot.agent.loop import _current_session_id
        current_session_id = _current_session_id.get(None)

        if not query or not query.strip():
            return self._format_recent(limit, current_session_id)

        return await self._search(
            query.strip(), limit, role_filter, current_session_id
        )

    # ------------------------------------------------------------------
    # Recent sessions
    # ------------------------------------------------------------------

    def _format_recent(self, limit: int, current_session_id: str | None = None) -> str:
        exclude_ids: set[str] = set()
        if current_session_id:
            exclude_ids = self._get_lineage_ids(current_session_id)

        sessions = self._db.list_recent_sessions(limit=limit + len(exclude_ids))
        if not sessions:
            return "No sessions found."
        results: list[dict[str, Any]] = []
        for s in sessions:
            sid = s.get("id", "?")
            if sid in exclude_ids:
                continue
            last_active = s.get("last_active_at") or s.get("started_at", 0)
            started = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(s.get("started_at", 0))
            )
            active = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(last_active)
            )
            title = s.get("title") or "(untitled)"
            msg_count = s.get("message_count", 0)
            preview = _make_preview(s.get("preview"))
            sk = s.get("session_key", "")
            entry: dict[str, Any] = {
                "session_id": sid,
                "title": title,
                "preview": preview,
                "messages": msg_count,
                "started": started,
                "last_active": active,
                "session_key": sk,
                "channel": sk.split(":")[0] if sk and ":" in sk else "",
            }
            results.append(entry)
            if len(results) >= limit:
                break
        if not results:
            return "No sessions found."
        return json.dumps({"sessions": results}, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Keyword search + LLM summarisation
    # ------------------------------------------------------------------

    def _get_lineage_ids(self, session_id: str) -> set[str]:
        """Collect all session IDs in the lineage of session_id."""
        ids: set[str] = set()
        current = session_id
        for _ in range(20):  # safety limit
            ids.add(current)
            row = self._db.get_session(current)
            if not row or not row.get("parent_session_id"):
                break
            current = row["parent_session_id"]
        return ids

    async def _search(
        self,
        query: str,
        limit: int,
        role_filter: str | None,
        current_session_id: str | None,
    ) -> str:
        exclude_ids: set[str] = set()
        if current_session_id:
            exclude_ids = self._get_lineage_ids(current_session_id)

        parsed_roles = None
        if role_filter:
            parsed_roles = [r.strip() for r in role_filter.split(",") if r.strip()]

        rows = self._db.search_messages(
            query,
            role_filter=parsed_roles,
            exclude_sources=["subagent"],
            limit=limit * 10,
        )

        # Group by session, exclude current lineage
        by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            sid = row["session_id"]
            if sid not in exclude_ids:
                by_session[sid].append(row)

        top_ids = list(by_session.keys())[:limit]

        if not top_ids:
            return json.dumps({
                "success": True,
                "query": query,
                "count": 0,
                "results": [],
            }, ensure_ascii=False)

        # Parallel summarize with concurrency limit
        _sem = asyncio.Semaphore(5)

        async def _limited_summarize(sid: str, q: str) -> tuple[str, str]:
            async with _sem:
                summary = await self._summarize_session(sid, q)
                return sid, summary

        coros = [_limited_summarize(sid, query) for sid in top_ids]
        raw_results = await asyncio.gather(*coros, return_exceptions=True)

        summary_map: dict[str, str] = {}
        for r in raw_results:
            if isinstance(r, Exception):
                logger.warning("session_search parallel summarize failed: {}", r)
                continue
            sid, summary = r
            summary_map[sid] = summary

        results: list[dict[str, Any]] = []
        for sid in top_ids:
            session_info = self._db.get_session(sid)
            started = time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime((session_info or {}).get("started_at", 0)),
            )
            sk = (session_info or {}).get("session_key", "")
            entry = {
                "session_id": sid,
                "when": started,
                "source": (session_info or {}).get("source", "unknown"),
                "model": (session_info or {}).get("model") or "?",
                "title": (session_info or {}).get("title") or "(untitled)",
                "summary": summary_map.get(sid, "(summary unavailable)"),
                "session_key": sk,
                "channel": sk.split(":")[0] if sk and ":" in sk else "",
            }
            results.append(entry)

        return json.dumps({
            "success": True,
            "query": query,
            "count": len(results),
            "results": results,
        }, ensure_ascii=False)

    async def _summarize_session(
        self, session_id: str, query: str
    ) -> str:
        messages = self._db.get_messages(session_id)
        if not messages:
            return "(no messages)"

        conversation_text = _format_conversation(messages)
        conversation_text = _truncate_around_matches(conversation_text, query)

        system_prompt = (
            "You are reviewing a past conversation transcript to help recall what happened. "
            "Summarize the conversation with a focus on the search topic. Include:\n"
            "1. What the user asked about or wanted to accomplish\n"
            "2. What actions were taken and what the outcomes were\n"
            "3. Key decisions, solutions found, or conclusions reached\n"
            "4. Any specific commands, files, URLs, or technical details that were important\n"
            "5. Anything left unresolved or notable\n\n"
            "Be thorough but concise. Preserve specific details (commands, paths, error messages) "
            "that would be useful to recall. Write in past tense as a factual recap."
        )
        user_prompt = (
            f"Search topic: {query}\n\n"
            f"CONVERSATION TRANSCRIPT:\n{conversation_text}\n\n"
            f"Summarize this conversation with focus on: {query}"
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await self._provider.chat_with_retry(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                content = resp.content
                if content:
                    return content.strip()
                if attempt < max_retries - 1:
                    await asyncio.sleep(attempt + 1)
                    continue
            except Exception as exc:
                if attempt < max_retries - 1:
                    await asyncio.sleep(attempt + 1)
                else:
                    logger.warning(
                        "session_search summarisation failed after {} retries: {}",
                        max_retries, exc,
                    )

        # Fallback: raw preview when summarization unavailable
        preview = (
            conversation_text[:500] + "\n...[truncated]"
            if len(conversation_text) > 500
            else conversation_text
        )
        return f"[Raw preview — summarization unavailable]\n{preview}"
