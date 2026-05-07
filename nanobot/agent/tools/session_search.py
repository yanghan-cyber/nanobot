"""SessionSearchTool: search and browse past conversation sessions."""

from __future__ import annotations

import json
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
                "Search keyword(s). Supports FTS5 syntax. "
                "Omit to list recent sessions."
            ),
        },
        "role_filter": {
            "type": "string",
            "description": (
                "Comma-separated roles to include (e.g. 'user,assistant')"
            ),
        },
        "limit": {
            "type": "integer",
            "description": "Max sessions to return (1-20, default 5).",
            "default": 5,
        },
    },
}

_SUMMARY_SYSTEM_PROMPT = (
    "You are a helpful assistant that summarizes conversations. "
    "Given messages from a past conversation and a search query, "
    "produce a concise 1-3 sentence summary of what was discussed "
    "relevant to the query. Focus on key topics, decisions, and outcomes."
)

_MAX_TRANSCRIPT_CHARS = 3000


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
            "Search and browse past conversation sessions.\n"
            "Two modes:\n"
            "1. No query: list the most recent sessions.\n"
            "2. With query: full-text search across all stored messages, "
            "then summarise each matching session.\n"
            "Use this to recall past conversations or find previously "
            "discussed topics."
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
        current_session_id: str | None = None,
        **_kwargs: Any,
    ) -> str:
        limit = max(1, min(20, limit))

        if not query or not query.strip():
            return self._format_recent(limit)

        return await self._search(
            query.strip(), limit, role_filter, current_session_id
        )

    # ------------------------------------------------------------------
    # Recent sessions
    # ------------------------------------------------------------------

    def _format_recent(self, limit: int) -> str:
        sessions = self._db.list_recent_sessions(limit=limit)
        if not sessions:
            return "No sessions found."
        lines: list[str] = []
        for s in sessions:
            started = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(s.get("started_at", 0))
            )
            title = s.get("title") or "(untitled)"
            sid = s.get("id", "?")
            model = s.get("model", "?")
            msg_count = s.get("message_count", 0)
            lines.append(
                f"- [{sid}] {title}  "
                f"(model: {model}, messages: {msg_count}, started: {started})"
            )
        return "\n".join(lines)

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
            exclude_sources=None,
            limit=limit * 10,
        )

        # Group by session, exclude current lineage
        by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            sid = row["session_id"]
            if sid not in exclude_ids:
                by_session[sid].append(row)

        top_ids = list(by_session.keys())[:limit]

        results: list[dict[str, Any]] = []
        for sid in top_ids:
            session_info = self._db.get_session(sid)
            summary = await self._summarize_session(sid, query)
            started = time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime((session_info or {}).get("started_at", 0)),
            )
            results.append({
                "session_id": sid,
                "when": started,
                "source": (session_info or {}).get("source", "unknown"),
                "model": (session_info or {}).get("model", "unknown"),
                "title": (session_info or {}).get("title") or "(untitled)",
                "summary": summary,
            })

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

        parts: list[str] = []
        total_len = 0
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content") or ""
            line = f"{role}: {content}"
            if total_len + len(line) > _MAX_TRANSCRIPT_CHARS:
                remaining = _MAX_TRANSCRIPT_CHARS - total_len
                if remaining > 20:
                    parts.append(line[:remaining] + "...")
                break
            parts.append(line)
            total_len += len(line)

        transcript = "\n".join(parts)
        try:
            resp = await self._provider.chat_with_retry(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Search query: {query}\n\nTranscript:\n{transcript}"
                        ),
                    },
                ],
            )
            content = resp.content
            return content.strip() if content else "(no summary)"
        except Exception as exc:
            logger.warning("session_search summarisation failed: {}", exc)
            return f"(summary unavailable: {exc})"
