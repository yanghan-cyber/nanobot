"""Path boundary validation for hook files.

Aligned with OpenClaw's isPathInsideWithRealpath() security check.
"""

from __future__ import annotations

from pathlib import Path


def validate_hook_path(hook_dir: Path, file_path: Path) -> bool:
    """Validate that file_path resolves within hook_dir."""
    try:
        resolved = file_path.resolve()
        root = hook_dir.resolve()
        root_str = str(root)
        resolved_str = str(resolved)
        if resolved_str == root_str:
            return True
        return resolved_str.startswith(root_str + "/") or resolved_str.startswith(root_str + "\\")
    except (OSError, ValueError):
        return False
