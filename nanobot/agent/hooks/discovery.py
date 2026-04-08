"""Hook discovery and dynamic handler loading.

Aligned with OpenClaw's loader.ts: scan directories, parse HOOK.md,
import handler modules, register with the event registry.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.agent.hooks.frontmatter import parse_hook_frontmatter
from nanobot.agent.hooks.registry import register_internal_hook
from nanobot.agent.hooks.security import validate_hook_path

if TYPE_CHECKING:
    from nanobot.config.schema import HooksConfig


def discover_hooks(hooks_dir: Path) -> list[dict]:
    """Scan a directory for hook entries.

    Each subdirectory containing ``HOOK.md`` is a hook candidate.
    Returns a list of dicts with keys: name, description, metadata, hook_dir, handler_path.
    """
    if not hooks_dir.is_dir():
        return []

    entries: list[dict] = []
    for child in sorted(hooks_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue

        hook_md = child / "HOOK.md"
        if not hook_md.is_file():
            continue

        frontmatter = _parse_hook_md(hook_md)
        if not frontmatter.get("name"):
            logger.warning("Hook in {} has no name, skipping", child)
            continue

        handler_path = _find_handler(child)
        if handler_path is None:
            logger.warning("Hook '{}' has no handler file in {}", frontmatter["name"], child)
            continue

        entries.append(
            {
                "name": frontmatter["name"],
                "description": frontmatter.get("description", ""),
                "metadata": frontmatter.get("metadata", {}),
                "hook_dir": child,
                "handler_path": handler_path,
            }
        )

    return entries


async def load_hooks(hooks_dir: Path, cfg: HooksConfig | None = None) -> int:
    """Discover, validate, and register all hooks from a directory.

    When *cfg* is provided, applies enabled/allow/deny filtering and
    scans any additional directories listed in ``cfg.dirs``.

    Returns the number of successfully loaded hooks.
    """
    from nanobot.config.schema import HooksConfig

    if cfg is None:
        cfg = HooksConfig()

    if not cfg.enabled:
        return 0

    # Collect directories to scan: primary + extra dirs from config
    dirs_to_scan: list[Path] = [hooks_dir]
    for extra in cfg.dirs:
        p = Path(extra)
        if p.is_dir():
            dirs_to_scan.append(p)

    entries: list[dict] = []
    for d in dirs_to_scan:
        entries.extend(discover_hooks(d))
    loaded = 0

    for entry in entries:
        # Apply allow/deny filters
        name: str = entry["name"]
        if cfg.deny and name in cfg.deny:
            continue
        if cfg.allow and name not in cfg.allow:
            continue
        try:
            handler_path: Path = entry["handler_path"]

            if not validate_hook_path(entry["hook_dir"], handler_path):
                logger.error("Hook '{}' handler path fails boundary check", entry["name"])
                continue

            export_name = "handler"
            metadata = entry.get("metadata", {})
            if isinstance(metadata, dict):
                export_name = metadata.get("export", "handler")

            handler = _load_handler(handler_path, export_name)
            if handler is None:
                logger.error("Hook '{}' handler is not callable", entry["name"])
                continue

            # Extract events from metadata
            events: list[str] = metadata.get("events", [])
            # OpenClaw compatibility: events may be nested under metadata.openclaw
            openclaw_meta = metadata.get("openclaw", {})
            if not events and isinstance(openclaw_meta, dict):
                events = openclaw_meta.get("events", [])

            if not events:
                logger.warning("Hook '{}' has no events defined", entry["name"])
                continue

            for event_key in events:
                register_internal_hook(event_key, handler)

            logger.debug("Registered hook: {} -> {}", entry["name"], ", ".join(events))
            loaded += 1

        except Exception:
            logger.exception("Failed to load hook '{}'", entry["name"])

    return loaded


def _parse_hook_md(path: Path) -> dict:
    """Read and parse HOOK.md frontmatter."""
    content = path.read_text(encoding="utf-8")
    return parse_hook_frontmatter(content)


def _find_handler(hook_dir: Path) -> Path | None:
    """Find handler file in priority order: handler.py, index.py."""
    for candidate in ("handler.py", "index.py"):
        p = hook_dir / candidate
        if p.is_file():
            return p
    return None


def _load_handler(path: Path, export_name: str = "handler"):
    """Dynamically import a Python module and extract the handler function."""
    # Use a stable identifier based on the resolved absolute path to avoid
    # collisions when Python reuses memory addresses after GC.
    resolved = str(path.resolve())
    stable_id = hex(hash(resolved) & 0xFFFFFFFF)[2:]
    module_name = f"nanobot_hook_{path.stem}_{stable_id}"

    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    try:
        spec.loader.exec_module(module)
    except Exception:
        logger.exception("Failed to exec handler module {}", path)
        return None

    handler = getattr(module, export_name, None)
    if handler is None or not callable(handler):
        return None

    return handler
