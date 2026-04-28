# Subagent Output Storage Design

**Date**: 2026-04-28
**Status**: Approved

## Problem

Subagent results are truncated to 1000 characters (head 500 + tail 500) in
`_announce_result`. The middle content is lost — neither persisted nor
accessible. For long-running subagents (e.g. research tasks), this drops
valuable output.

## Design

Store full subagent output to a file, return only the tail to the main agent
with a file path hint.

### Scope

Single method change: `_announce_result` in `nanobot/agent/subagent.py`.

### File Storage

- **Path**: `{data_dir}/tool-output/subagent/{task_id}.log`
- **Write**: always, regardless of output length
- **Helper**:

```python
def _subagent_output_path(task_id: str) -> Path:
    return ensure_dir(get_data_dir() / "tool-output" / "subagent") / f"{task_id}.log"
```

### Tail Extraction

- **Unit**: lines (not characters)
- **Default**: last 50 lines
- **Threshold**: if total lines > 50, append file path hint; otherwise return
  full content unchanged

### File Path Hint Format

```
... (last 50 of N lines, full output saved to: /path/to/{task_id}.log)
```

Appended after the tail content when truncation occurs. The main agent sees
this and can use read_file or bash to access the full output.

### New Imports

```python
from nanobot.config.paths import get_data_dir
from nanobot.utils.helpers import ensure_dir
```

### Removed Code

The old head+tail truncation block in `_announce_result`:

```python
_MAX_ANNOUNCE_RESULT = 1000
if len(result) > _MAX_ANNOUNCE_RESULT:
    half = _MAX_ANNOUNCE_RESULT // 2
    result = result[:half] + "\n\n... (truncated) ...\n\n" + result[-half:]
```

Replaced entirely by the file-write + tail logic.

### Data Flow

```
subagent completes
  → _announce_result(task_id, label, result, origin, status)
  → write full result to data/tool-output/subagent/{task_id}.log
  → take last 50 lines as tail
  → if total_lines > 50: append file path hint
  → render template(result=processed_tail)
  → publish to message bus → inject into main loop
```

### Not In Scope

- No cleanup/TTL mechanism — subagent output files are small; add later if needed
- No changes to `_persist_subagent_followup` or the announce template
- No cross-tool coupling with ShellBgTool
