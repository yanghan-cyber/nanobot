# Runner Length Handling Fix Design

**Date**: 2026-04-23
**Scope**: `nanobot/agent/runner.py`

## Problem

When `finish_reason='length'` and the LLM was generating tool calls (not text),
the runner discards incomplete tool calls, finds `response.content` empty,
and falls into the wrong recovery branch (empty retry instead of length recovery).

This causes:
1. No "output too long" signal sent back to the LLM — it retries the same long prompt and hits the same limit
2. `on_stream_end(resuming=False)` prematurely closes streaming channels (Feishu adds Done reactions before the agent finishes)
3. After 2 empty retries, finalization retry may also fail silently — user gets no reply
4. Subagents hit the same bug: stuck in retry loops writing large `write_file` tool calls, eventually timing out

## Current Code Flow (runner.py:366-424)

```
if has_tool_calls:           # fires even when finish_reason='length'
    warn + ignore tool calls
    clean = ""               # tool call JSON, not text
    is_blank_text(clean)?
        → empty retry        # WRONG: this is a length issue, not empty
        → finalization retry
        → EMPTY_FINAL_RESPONSE_MESSAGE or silent failure

if is_blank_text(clean):     # general empty case
    → empty retry / finalization

if finish_reason=='length' and not is_blank_text(clean):  # ONLY path that works
    → length recovery (append continuation prompt)
```

## Design

### Change: reorder — check `finish_reason` before `has_tool_calls`

```
if finish_reason == 'length':                    # NEW: intercept ALL length cases first
    discard incomplete tool calls
    if clean has text → append as assistant message
    if clean is empty → skip assistant message (same as existing length recovery for blank text)
    append LENGTH_RECOVERY_PROMPT as user message
    on_stream_end(resuming=True)                 # keep channel alive
    continue (up to _MAX_LENGTH_RECOVERIES=3)

if has_tool_calls:                                # only reached when finish_reason is normal
    execute tools normally

if is_blank_text(clean):                          # genuine empty response
    → empty retry / finalization (with resuming=True during retries)
```

### Behavioral details

| Scenario | Before | After |
|----------|--------|-------|
| length + tool calls (LLM generating `write_file`) | empty retry → silent failure | length recovery with "Output limit reached" prompt |
| length + partial text | length recovery (only path that worked) | same behavior, unchanged |
| length + empty content (no tool calls) | empty retry | length recovery |
| length recovery exhausted (3x) | N/A (never reached) | falls through to finalization path |

### Additional fix: empty retry `resuming` flag

`on_stream_end(context, resuming=False)` during empty retries → change to `resuming=True`.
Only use `resuming=False` after finalization completes. This prevents Feishu from adding
Done reactions while the runner is still retrying.

### Files changed

- `nanobot/agent/runner.py` — ~30 lines changed in the main `run()` loop (lines 365-424 area)

### What is NOT changed

- `LENGTH_RECOVERY_PROMPT` text — keep as-is
- `_MAX_LENGTH_RECOVERIES = 3` — keep as-is
- finalization retry logic — keep as-is
- subagent code — no changes needed (shares the same runner)
- any channel-specific code — no changes needed
