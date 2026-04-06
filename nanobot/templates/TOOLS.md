# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## bash — Shell Execution

- Commands have a configurable timeout (default 60s, max 600s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters (head + tail preserved)
- `restrictToWorkspace` config can limit file access to the workspace
- Use `run_in_background=true` for long-running commands (builds, servers)
- Background tasks: use `shell_bg` tool to list / read output / kill

## shell_bg — Background Task Manager

- `action="list"` — show all background tasks
- `action="output"` — read last 50 lines of a task's output
- `action="kill"` — terminate a running task

## glob — File Discovery

- Powered by ripgrep. Returns file paths sorted by modification time (newest first)
- Use `glob` to find files by pattern before falling back to shell commands
- Supports recursive patterns like `**/*.py`, `src/**/*.ts`, `tests/**/test_*.py`
- Use `head_limit` to cap results (default 250, max 1000; pass 0 for unlimited)
- Use `offset` to skip the first N results for pagination (max 100000)
- Prefer this over `bash` when you only need file paths
- When doing open-ended searches needing multiple rounds, use subagent instead

## grep — Content Search

- Powered by ripgrep. ALWAYS prefer `grep` over running `grep`/`rg` in bash
- Supports full regex syntax (e.g., `log.*Error`, `function\s+\w+`)
- Pattern syntax follows ripgrep: literal braces need escaping (`interface\{\}`)
- Default mode returns only matching file paths (`output_mode="files_with_matches"`)
- Output modes:
  - `files_with_matches` — deduplicated file paths (default)
  - `content` — matching lines in `path:line: content` format; context lines use `path:line- content`
  - `count` — per-file match counts with total summary
- Use `glob` to filter by filename (e.g., `"*.py"`, `"*.{ts,tsx}"`)
- Use `type` for file-type shorthand (e.g., `"py"`, `"js"`, `"rust"`); more efficient than `glob`
- Use `case_insensitive=true` for case-insensitive search
- Use `fixed_strings=true` to treat pattern as plain text instead of regex
- Use `context_before` / `context_after` for surrounding context lines (content mode only, max 20)
- Use `multiline=true` for cross-line patterns where `.` matches newlines
- Use `head_limit` to cap results (default 250, max 1000; pass 0 for unlimited)
- Use `offset` to skip the first N matches for pagination (max 100000)
- Output is truncated at 50K characters to protect context window
- Prefer this over `bash` for code and history searches

## cron — Scheduled Reminders

- Please refer to cron skill for usage.
