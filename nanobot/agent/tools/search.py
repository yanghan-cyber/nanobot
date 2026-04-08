"""Search tools: grep and glob powered by ripgrep."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.filesystem import _FsTool
from nanobot.utils.ripgrep import ensure_rg

_DEFAULT_HEAD_LIMIT = 250
_MAX_RESULT_CHARS = 50_000


def _resolve_limit(head_limit: int | None) -> int | None:
    return None if head_limit == 0 else (head_limit or _DEFAULT_HEAD_LIMIT)


def _paginate(items: list, limit: int | None, offset: int) -> tuple[list, bool]:
    if limit is None:
        return items[offset:], False
    sliced = items[offset: offset + limit]
    return sliced, len(items) > offset + limit


def _pagination_note(limit: int | None, offset: int, truncated: bool) -> str | None:
    if truncated:
        if limit is None:
            return f"(pagination: offset={offset})"
        return f"(pagination: limit={limit}, offset={offset})"
    if offset > 0:
        return f"(pagination: offset={offset})"
    return None


class _SearchTool(_FsTool):
    """Base class for ripgrep-powered search tools."""

    def _display_path(self, abs_path: Path) -> str:
        """Convert an absolute path to a workspace-relative display path."""
        if self._workspace:
            try:
                return abs_path.relative_to(self._workspace).as_posix()
            except ValueError:
                pass
        return abs_path.name


class GlobTool(_SearchTool):
    """Find files matching a glob pattern using ripgrep."""

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "Fast file pattern matching tool powered by ripgrep. "
            "Supports glob patterns like '**/*.js' or 'src/**/*.ts'. "
            "Returns matching file paths sorted by modification time (newest first). "
            "Skips .git, node_modules, __pycache__, and other noise directories. "
            "Use this tool when you need to find files by name patterns. "
            "When doing open-ended searches needing multiple rounds of "
            "globbing and grepping, use subagent instead."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match, e.g. '*.py' or 'tests/**/test_*.py'",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Directory to search from. "
                        "If not specified, the current working directory will be used."
                    ),
                },
                "head_limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of matches to return (default 250, max 1000). "
                        "Pass 0 for unlimited."
                    ),
                    "minimum": 0,
                    "maximum": 1000,
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "Skip the first N matching entries before returning results "
                        "(equivalent to 'tail -n +N'). Useful for pagination."
                    ),
                    "minimum": 0,
                    "maximum": 100000,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        head_limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> str:
        try:
            root = self._resolve(path or ".")
            limit = _resolve_limit(head_limit)

            try:
                rg_path = ensure_rg()
            except FileNotFoundError as e:
                return f"Error: {e}"

            cmd = [rg_path, "--files", "--sortr", "modified", "--glob", pattern, str(root)]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 2:
                error_msg = stderr.decode("utf-8", errors="replace")
                return f"Error: {error_msg.strip()}"

            files = stdout.decode("utf-8", errors="replace").splitlines()
            files = [f.strip() for f in files if f.strip()]

            if not files:
                return f"No paths matched pattern '{pattern}' in {path}"

            display_files = [self._display_path(Path(f)) for f in files]

            total = len(display_files)
            if offset > 0:
                display_files = display_files[offset:]
            if limit is not None:
                display_files = display_files[:limit]
                truncated = total > offset + limit
            else:
                truncated = False

            result = "\n".join(display_files)
            if note := _pagination_note(limit, offset, truncated):
                result += f"\n\n{note}"

            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error finding files: {e}"


class GrepTool(_SearchTool):
    """Search file contents using ripgrep with JSON streaming output."""

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "A powerful search tool built on ripgrep. "
            "ALWAYS use this tool for search tasks instead of running "
            "'grep' or 'rg' in bash. "
            "Supports full regex syntax (e.g., 'log.*Error', 'function\\s+\\w+'). "
            "Pattern syntax follows ripgrep: literal braces need escaping "
            "(use 'interface\\{\\}' to find 'interface{}'). "
            "Output modes: 'content' shows matching lines with context; "
            "'files_with_matches' shows only file paths (default); "
            "'count' shows per-file match counts. "
            "Skips binary and files >2 MB. Supports glob/type filtering. "
            "Multiline matching: by default patterns match within single lines only. "
            "For cross-line patterns, use multiline=true."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "The regular expression pattern to search for in file contents"
                    ),
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": (
                        "File or directory to search in. "
                        "If not specified, the current working directory will be used."
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Glob pattern to filter files (e.g. '*.py', '**/*.tsx'). "
                        "Maps to rg --glob."
                    ),
                },
                "type": {
                    "type": "string",
                    "description": (
                        "File type to search (rg --type). Common types: "
                        "js, py, rust, go, java, etc. "
                        "More efficient than glob filtering for standard file types."
                    ),
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default false)",
                },
                "fixed_strings": {
                    "type": "boolean",
                    "description": (
                        "Treat pattern as plain text instead of regex (default false). "
                        "Useful when searching for literal strings containing "
                        "regex metacharacters."
                    ),
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        "Output mode: 'files_with_matches' shows only file paths "
                        "(default); 'content' shows matching lines with context "
                        "(supports context_before/after and head_limit); "
                        "'count' shows per-file match counts."
                    ),
                },
                "context_before": {
                    "type": "integer",
                    "description": (
                        "Number of lines of context before each match. "
                        "Only applies when output_mode='content'."
                    ),
                    "minimum": 0,
                    "maximum": 20,
                },
                "context_after": {
                    "type": "integer",
                    "description": (
                        "Number of lines of context after each match. "
                        "Only applies when output_mode='content'."
                    ),
                    "minimum": 0,
                    "maximum": 20,
                },
                "multiline": {
                    "type": "boolean",
                    "description": (
                        "Enable multiline mode where . matches newlines and "
                        "patterns can span lines (rg -U --multiline-dotall). "
                        "Default: false."
                    ),
                },
                "head_limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of results to return (default 250, max 1000). "
                        "Pass 0 for unlimited. Equivalent to '| head -N'. "
                        "Works across all output modes."
                    ),
                    "minimum": 0,
                    "maximum": 1000,
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "Skip the first N results before applying head_limit. "
                        "Equivalent to '| tail -n +N | head -N'. "
                        "Works across all output modes."
                    ),
                    "minimum": 0,
                    "maximum": 100000,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        type: str | None = None,
        case_insensitive: bool = False,
        fixed_strings: bool = False,
        output_mode: str = "files_with_matches",
        context_before: int = 0,
        context_after: int = 0,
        multiline: bool = False,
        head_limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> str:
        try:
            target = self._resolve(path or ".")
            limit = _resolve_limit(head_limit)

            try:
                rg_path = ensure_rg()
            except FileNotFoundError as e:
                return f"Error: {e}"

            cmd = [rg_path, "--json"]
            if case_insensitive:
                cmd.append("-i")
            if fixed_strings:
                cmd.append("--fixed-strings")
            if glob:
                cmd.extend(["--glob", glob])
            if type:
                cmd.extend(["--type", type])
            if multiline:
                cmd.extend(["--multiline", "--multiline-dotall"])
            if context_before > 0:
                cmd.extend(["-B", str(context_before)])
            if context_after > 0:
                cmd.extend(["-A", str(context_after)])

            cmd.append(pattern)
            cmd.append(str(target))

            return await self._run_rg(cmd, target, output_mode, limit, offset)

        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error("Grep error: {}", e)
            return f"Error searching files: {e}"

    async def _run_rg(
        self,
        cmd: list[str],
        target: Path,
        output_mode: str,
        limit: int | None,
        offset: int,
    ) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        matching_files: list[str] = []
        seen_files: set[str] = set()
        counts: dict[str, int] = {}
        content_blocks: list[str] = []
        result_chars = 0
        seen_matches = 0
        truncated = False
        size_truncated = False
        early_exit = False

        # Context state
        buffered_context: list[str] = []
        current_match_accepted = False

        if not proc.stdout:
            return "Error: Failed to open stdout pipe for ripgrep"

        async for line_bytes in proc.stdout:
            if truncated or size_truncated:
                early_exit = True
                break
            try:
                data = json.loads(line_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.debug("Failed to parse rg JSON line: {}", line_bytes[:100])
                continue

            event_type = data.get("type")

            if event_type not in ("match", "context"):
                continue

            path_data = data["data"]["path"]["text"]
            line_num = data["data"]["line_number"]
            content = data["data"]["lines"]["text"].rstrip("\n\r")
            display_path = self._display_path(Path(path_data))

            if event_type == "match":
                if output_mode == "files_with_matches":
                    if display_path not in seen_files:
                        seen_files.add(display_path)
                        matching_files.append(display_path)
                        if limit is not None and len(matching_files) > offset + limit:
                            early_exit = True
                            break
                    continue

                if output_mode == "count":
                    counts[display_path] = counts.get(display_path, 0) + 1
                    if display_path not in seen_files:
                        seen_files.add(display_path)
                        matching_files.append(display_path)
                    continue

                # Content mode
                seen_matches += 1
                if seen_matches <= offset:
                    current_match_accepted = False
                    buffered_context.clear()
                    continue

                current_match_accepted = True

                # Flush buffered before-context
                for b_line in buffered_context:
                    if limit is not None and len(content_blocks) >= limit:
                        truncated = True
                        break
                    extra = 2 if content_blocks else 0
                    if result_chars + extra + len(b_line) > _MAX_RESULT_CHARS:
                        size_truncated = True
                        break
                    content_blocks.append(b_line)
                    result_chars += extra + len(b_line)
                buffered_context.clear()

                if truncated or size_truncated:
                    break

                formatted = f"{display_path}:{line_num}: {content}"
                extra = 2 if content_blocks else 0
                if limit is not None and len(content_blocks) >= limit:
                    truncated = True
                    break
                if result_chars + extra + len(formatted) > _MAX_RESULT_CHARS:
                    size_truncated = True
                    break
                content_blocks.append(formatted)
                result_chars += extra + len(formatted)

            elif event_type == "context" and output_mode == "content":
                formatted = f"{display_path}:{line_num}- {content}"
                if current_match_accepted:
                    if limit is not None and len(content_blocks) >= limit:
                        truncated = True
                        break
                    extra = 2 if content_blocks else 0
                    if result_chars + extra + len(formatted) > _MAX_RESULT_CHARS:
                        size_truncated = True
                        break
                    content_blocks.append(formatted)
                    result_chars += extra + len(formatted)
                else:
                    buffered_context.append(formatted)

        # Clean up process — only terminate if we exited early
        if early_exit:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        else:
            await proc.wait()

        # Check for rg errors (exit code 2 = error, e.g. invalid regex)
        if proc.returncode == 2:
            stderr_bytes = await proc.stderr.read()
            error_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
            return f"Error: {error_msg}"

        # Build output
        if output_mode == "files_with_matches":
            if not matching_files:
                return f"No matches found for pattern in {target}"
            paged, is_trunc = _paginate(matching_files, limit, offset)
            result = "\n".join(paged)
            if note := _pagination_note(limit, offset, is_trunc):
                result += f"\n\n{note}"
            return result

        if output_mode == "count":
            if not counts:
                return f"No matches found for pattern in {target}"
            paged, is_trunc = _paginate(matching_files, limit, offset)
            lines = [f"{n}: {counts[n]}" for n in paged]
            result = "\n".join(lines)
            notes = []
            if note := _pagination_note(limit, offset, is_trunc):
                notes.append(note)
            notes.append(f"(total matches: {sum(counts.values())} in {len(counts)} files)")
            result += "\n\n" + "\n".join(notes)
            return result

        # Content mode
        if not content_blocks:
            return f"No matches found for pattern in {target}"
        result = "\n\n".join(content_blocks)
        notes = []
        if truncated:
            notes.append(f"(pagination: limit={limit}, offset={offset})")
        elif size_truncated:
            notes.append("(output truncated due to size)")
        elif offset > 0:
            notes.append(f"(pagination: offset={offset})")
        if notes:
            result += "\n\n" + "\n".join(notes)
        return result
