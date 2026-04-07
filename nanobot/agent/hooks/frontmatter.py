"""HOOK.md YAML frontmatter parser.

Aligned with OpenClaw's frontmatter.ts. Uses a simple regex-based approach
instead of a full YAML parser to keep dependencies minimal.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def parse_hook_frontmatter(content: str) -> dict[str, Any]:
    """Parse HOOK.md frontmatter into a dict.

    Returns keys: name, description, metadata, etc.
    The ``metadata`` value is parsed as JSON; all other values are strings.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}

    raw = match.group(1)
    result: dict[str, Any] = {}

    for line in raw.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        # Strip surrounding quotes
        if len(value) >= 2:
            if (value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'"):
                value = value[1:-1]

        if key == "metadata":
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                result[key] = {}
        else:
            result[key] = value

    return result
