"""Shell execution tool — BashTool + background task support."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from contextlib import suppress
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from loguru import logger

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.sandbox import wrap_command
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.bus.events import InboundMessage
from nanobot.config.paths import get_data_dir, get_media_dir
from nanobot.utils.helpers import ensure_dir
from nanobot.utils.prompt_templates import render_template

# ---------------------------------------------------------------------------
# Shell resolution
# ---------------------------------------------------------------------------

# Common Git Bash install paths on Windows (checked in order)
_WINDOWS_GIT_BASH_PATHS = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
    os.path.expandvars(r"%USERPROFILE%\scoop\apps\git\current\bin\bash.exe"),
]


def _resolve_shell() -> str:
    """Return the best available shell path for ``create_subprocess_exec``.

    Priority:
    - Linux/macOS: ``/bin/bash`` (fallback ``/bin/sh``)
    - Windows:
      1. Derive from ``git.exe`` path (walk parents → ``Git/bin/bash.exe``)
      2. Common install paths
      3. ``shutil.which("pwsh")`` (PowerShell Core)
      4. ``shutil.which("powershell")`` (Windows PowerShell)
    """
    if sys.platform != "win32":
        bash = shutil.which("bash")
        return bash or "/bin/bash"

    # --- Windows: Git Bash detection ---

    # Priority 1: derive from git.exe location
    git_path = shutil.which("git")
    if git_path:
        git_exe = Path(git_path).resolve()
        for parent in git_exe.parents:
            candidate = parent / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)

    # Priority 2: common install paths
    for p in _WINDOWS_GIT_BASH_PATHS:
        if os.path.exists(p):
            return p

    # Priority 3: PowerShell Core
    pwsh = shutil.which("pwsh")
    if pwsh:
        return pwsh

    # Priority 4: Windows PowerShell
    powershell = shutil.which("powershell")
    if powershell:
        return powershell

    # Last resort
    return shutil.which("cmd") or "cmd.exe"


# ---------------------------------------------------------------------------
# Background task state
# ---------------------------------------------------------------------------

_bg_processes: dict[str, asyncio.subprocess.Process] = {}
_bg_meta: dict[str, dict] = {}
_bg_file_handles: dict[str, BinaryIO] = {}
_last_cleanup: float = 0.0  # monotonic timestamp of last cleanup run
_CLEANUP_INTERVAL = 300.0  # minimum seconds between cleanup sweeps (5 min)
_bg_lock = asyncio.Lock()  # protect concurrent access to bg state
_MAX_ENTRIES = 128  # maximum number of completed tasks to keep


def _bg_output_path(bg_id: str) -> Path:
    return ensure_dir(get_data_dir() / "tool-output" / "shell") / f"{bg_id}_output.log"


def _close_file_handle(fh: BinaryIO | None) -> None:
    """Safely close a file handle, ignoring errors."""
    if fh:
        try:
            fh.close()
        except Exception:
            pass


def _should_cleanup() -> bool:
    """Check if cleanup should run (time-based or threshold-based).

    Note: Reads shared state without lock for performance. The threshold
    check provides a safety net if time-based check races.
    """
    now = time.monotonic()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        # Force cleanup if too many entries accumulated
        return len(_bg_meta) > _MAX_ENTRIES * 0.8
    return True


async def _cleanup_bg_meta(ttl_minutes: int = 120, max_entries: int = 128) -> None:
    """Remove expired or excess completed bg task metadata and their log files.

    Rules:
    1. Running tasks are never removed.
    2. Completed/failed/killed tasks older than ``ttl_minutes`` are evicted.
    3. If still over ``max_entries`` after TTL eviction, remove the oldest.
    """
    global _last_cleanup

    if not _should_cleanup():
        return

    async with _bg_lock:
        if not _bg_meta:
            _last_cleanup = time.monotonic()
            return

        now = datetime.now()
        to_remove: list[str] = []

        # Phase 1: TTL eviction
        for bg_id, m in _bg_meta.items():
            if m.get("status") == "running":
                continue
            end_time_str = m.get("end_time")
            if not end_time_str:
                continue
            try:
                end_time = datetime.fromisoformat(end_time_str)
                if (now - end_time).total_seconds() > ttl_minutes * 60:
                    to_remove.append(bg_id)
            except (ValueError, TypeError):
                pass

        for bg_id in to_remove:
            _remove_bg_entry(bg_id)

        # Phase 2: cap eviction — if still over max, remove oldest first
        completed = [
            (bg_id, m.get("end_time", ""))
            for bg_id, m in _bg_meta.items()
            if m.get("status") != "running"
        ]
        excess = len(completed) - max_entries
        if excess > 0:
            completed.sort(key=lambda x: x[1])
            for bg_id, _ in completed[:excess]:
                _remove_bg_entry(bg_id)

        _last_cleanup = time.monotonic()


def _remove_bg_entry(bg_id: str) -> None:
    """Remove a bg task's metadata and its output log file."""
    meta = _bg_meta.pop(bg_id, None)
    if meta:
        try:
            Path(meta["output_file"]).unlink(missing_ok=True)
        except Exception:
            pass
    _bg_processes.pop(bg_id, None)
    _close_file_handle(_bg_file_handles.pop(bg_id, None))


async def _monitor_process(bg_id: str) -> None:
    """Monitor a background process; update meta on completion and notify origin."""
    process = _bg_processes.get(bg_id)
    meta = _bg_meta.get(bg_id)
    if not process or not meta:
        _close_file_handle(_bg_file_handles.pop(bg_id, None))
        return

    try:
        exit_code = await process.wait()
        async with _bg_lock:
            # Don't overwrite if _kill() already set status to "killed"
            if meta.get("status") != "killed":
                meta["status"] = "completed" if exit_code == 0 else "failed"
            meta["exit_code"] = exit_code
            meta["end_time"] = datetime.now().isoformat()
    except Exception as e:
        logger.error(f"Error monitoring process {bg_id}: {e}")
        async with _bg_lock:
            meta["status"] = "failed"
            meta["error"] = str(e)
            meta["end_time"] = datetime.now().isoformat()
    finally:
        _close_file_handle(_bg_file_handles.pop(bg_id, None))
        _bg_processes.pop(bg_id, None)

    # Send completion notification via message bus
    bus = meta.get("bus")
    if bus is None:
        logger.debug("No bus set for bg task [{}], skipping notification", bg_id)
        return
    channel = meta.get("channel")
    chat_id = meta.get("chat_id")
    session_key = meta.get("session_key")
    if not channel or not chat_id or not session_key:
        logger.debug(
            "Missing origin info for bg task [{}] (channel={}, chat_id={}, session_key={}), "
            "skipping notification",
            bg_id, channel, chat_id, session_key,
        )
        return

    try:
        announce_content = render_template(
            "agent/bg_task_announce.md",
            bg_id=bg_id,
            exit_code=meta.get("exit_code", -1),
            command=meta.get("command", ""),
        )
        msg = InboundMessage(
            channel="system",
            sender_id="bg_task",
            chat_id=f"{channel}:{chat_id}",
            content=announce_content,
            session_key_override=session_key,
            metadata={
                "injected_event": "bg_task_result",
                "bg_id": bg_id,
            },
        )
        await bus.publish_inbound(msg)
        logger.debug(
            "Background task [{}] notified {}:{}", bg_id, channel, chat_id,
        )
    except Exception as e:
        logger.warning("Failed to send bg task notification for {}: {}", bg_id, e)


# ---------------------------------------------------------------------------
# BashTool
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"


# Policy note appended to recoverable workspace-boundary guard errors.
_WORKSPACE_BOUNDARY_NOTE = (
    "\n\nNote: this is a hard policy boundary, not a transient failure. "
    "Do NOT retry with shell tricks (symlinks, base64 piping, alternative "
    "tools, working_dir overrides). If the user genuinely needs this "
    "resource, tell them you cannot reach it under the current "
    "restrict_to_workspace policy and ask how to proceed."
)


@tool_parameters(
    tool_parameters_schema(
        purpose=StringSchema(
            "Clear, concise description of what this command does in 5-10 words, "
            "in active voice. Examples:\n"
            "  Input: ls → Output: List files in current directory\n"
            "  Input: git status → Output: Show working tree status\n"
            "  Input: npm install → Output: Install package dependencies\n"
            "  Input: mkdir foo → Output: Create directory 'foo'",
        ),
        command=StringSchema("The shell command to execute"),
        working_dir=StringSchema("Optional working directory for the command"),
        timeout=IntegerSchema(
            60,
            description=(
                "Timeout in seconds for foreground commands only "
                "(default 60, max 600). "
                "Ignored when run_in_background=True — background tasks "
                "run until completion with no execution timeout."
            ),
            minimum=1,
            maximum=600,
        ),
        run_in_background=BooleanSchema(
            description=(
                "Run command in background. Returns a bg_id for tracking. "
                "Background tasks have no execution timeout — they run until "
                "completion. Completed task metadata is cleaned up after TTL "
                "(default 2h)."
            ),
            default=False,
        ),
        required=["command", "purpose"],
    )
)
class BashTool(Tool):
    """Tool to execute shell commands using bash."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        sandbox: str = "",
        path_append: str = "",
        allowed_env_keys: list[str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.sandbox = sandbox
        self.deny_patterns = (deny_patterns or []) + [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
            # Block writes to nanobot internal state files (#2989).
            # history.jsonl / .dream_cursor are managed by append_history();
            # direct writes corrupt the cursor format and crash /dream.
            r">>?\s*\S*(?:history\.jsonl|\.dream_cursor)",            # > / >> redirect
            r"\btee\b[^|;&<>]*(?:history\.jsonl|\.dream_cursor)",     # tee / tee -a
            r"\b(?:cp|mv)\b(?:\s+[^\s|;&<>]+)+\s+\S*(?:history\.jsonl|\.dream_cursor)",  # cp/mv target
            r"\bdd\b[^|;&<>]*\bof=\S*(?:history\.jsonl|\.dream_cursor)",  # dd of=
            r"\bsed\s+-i[^|;&<>]*(?:history\.jsonl|\.dream_cursor)",  # sed -i
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self.allowed_env_keys = allowed_env_keys or []

        # Origin tracking — set by the agent loop so background tasks know
        # which channel/chat they belong to.
        self._origin_channel: ContextVar[str] = ContextVar("bash_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("bash_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("bash_session_key", default="cli:direct")
        self._bus: Any | None = None

    def set_context(
        self,
        bus: Any | None = None,
        channel: str = "cli",
        chat_id: str = "direct",
        session_key: str = "cli:direct",
    ) -> None:
        self._bus = bus
        self._origin_channel.set(channel)
        self._origin_chat_id.set(chat_id)
        self._session_key.set(session_key)

    @property
    def name(self) -> str:
        return "bash"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    # Kernel device files safe as stdio redirect targets (#3599).
    _BENIGN_DEVICE_PATHS: frozenset[str] = frozenset({
        "/dev/null",
        "/dev/zero",
        "/dev/full",
        "/dev/random",
        "/dev/urandom",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/tty",
    })

    @property
    def description(self) -> str:
        return (
            "Executes a given bash command and returns its output.\n"
            "\n"
            "The working directory persists between commands, but shell state does not. "
            "The shell environment is initialized from the user's profile (login shell).\n"
            "\n"
            "IMPORTANT: Avoid using this tool to run `find`, `grep`, `cat`, `head`, "
            "`tail`, `sed`, `awk`, or `echo` commands, unless explicitly instructed or "
            "after you have verified that a dedicated tool cannot accomplish your task. "
            "Instead, use the appropriate dedicated tool as this will provide a much "
            "better experience for the user:\n"
            "\n"
            " - File search: Use glob (NOT find or ls)\n"
            " - Content search: Use grep (NOT shell grep or rg)\n"
            " - Read files: Use read_file (NOT cat/head/tail)\n"
            " - Edit files: Use edit_file (NOT sed/awk)\n"
            " - Write files: Use write_file (NOT echo >/cat <<EOF)\n"
            " - List directory: Use list_dir (NOT ls)\n"
            "\n"
            "Only use this tool for system commands and terminal operations that require "
            "shell execution. If you are unsure and there is a relevant dedicated tool, "
            "default to using the dedicated tool and only fallback on using this tool "
            "when it is absolutely necessary.\n"
            "\n"
            "Output is truncated at 10,000 chars. Timeout defaults to 60s (max 600s). "
            "Use run_in_background=true for long-running commands like servers, builds, "
            "or watches — returns a bg_id for tracking with shell_bg."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        purpose: str = "",
        command: str = "",
        working_dir: str | None = None,
        timeout: int | None = None,
        run_in_background: bool = False,
        **kwargs: Any,
    ) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()

        # Prevent an LLM-supplied working_dir from escaping the configured
        # workspace when restrict_to_workspace is enabled (#2826). Without
        # this, a caller can pass working_dir="/etc" and then all absolute
        # paths under /etc would pass the _guard_command check that anchors
        # on cwd.
        if self.restrict_to_workspace and self.working_dir:
            try:
                requested = Path(cwd).expanduser().resolve()
                workspace_root = Path(self.working_dir).expanduser().resolve()
            except Exception:
                return (
                    "Error: working_dir could not be resolved"
                    + _WORKSPACE_BOUNDARY_NOTE
                )
            if requested != workspace_root and workspace_root not in requested.parents:
                return (
                    "Error: working_dir is outside the configured workspace"
                    + _WORKSPACE_BOUNDARY_NOTE
                )

        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        if self.sandbox:
            if _IS_WINDOWS:
                logger.warning(
                    "Sandbox '{}' is not supported on Windows; running unsandboxed",
                    self.sandbox,
                )
            else:
                workspace = self.working_dir or cwd
                command = wrap_command(self.sandbox, command, workspace, cwd)
                cwd = str(Path(workspace).resolve())

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)
        env = self._build_env()

        shell = self._resolve_shell()

        if self.path_append:
            if _IS_WINDOWS:
                env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append
            else:
                env["NANOBOT_PATH_APPEND"] = self.path_append
                command = f'export PATH="$NANOBOT_PATH_APPEND{os.pathsep}$PATH"; {command}'

        if run_in_background:
            return await self._run_background(command, cwd, env, shell, purpose)

        return await self._run_foreground(command, cwd, env, shell, effective_timeout)

    async def _run_foreground(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
        shell: str,
        effective_timeout: int,
    ) -> str:
        try:
            is_bash = "bash" in Path(shell).name.lower()
            shell_args = ["-l", "-c", command] if is_bash else ["-c", command]

            # On Unix, create new session for proper process group management
            extra_kwargs = {}
            if not _IS_WINDOWS:
                extra_kwargs["preexec_fn"] = os.setsid

            process = await asyncio.create_subprocess_exec(
                shell,
                *shell_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                **extra_kwargs,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                await self._kill_process(process)
                return f"Error: Command timed out after {effective_timeout} seconds"
            except asyncio.CancelledError:
                await self._kill_process(process)
                raise

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            max_len = self._MAX_OUTPUT
            if len(result) > max_len:
                # For errors, preserve more of the tail (stack traces are at the end)
                if process.returncode != 0:
                    head = max_len // 4
                    tail = (max_len * 3) // 4
                    result = (
                        result[:head]
                        + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                        + result[-tail:]
                    )
                else:
                    # For success, split evenly
                    half = max_len // 2
                    result = (
                        result[:half]
                        + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                        + result[-half:]
                    )

            return result

        except Exception as e:
            return f"Error executing command: {str(e)}"

    async def _run_background(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
        shell: str,
        purpose: str,
    ) -> str:
        bg_id = f"bash_bg_{uuid.uuid4().hex[:6]}"
        output_file = _bg_output_path(bg_id)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        fh = None
        try:
            fh = open(output_file, "wb")

            is_bash = "bash" in Path(shell).name.lower()
            shell_args = ["-l", "-c", command] if is_bash else ["-c", command]

            # On Unix, create new session for proper process group management
            extra_kwargs = {}
            if not _IS_WINDOWS:
                extra_kwargs["preexec_fn"] = os.setsid

            process = await asyncio.create_subprocess_exec(
                shell,
                *shell_args,
                stdout=fh,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=env,
                **extra_kwargs,
            )

            async with _bg_lock:
                _bg_processes[bg_id] = process
                _bg_meta[bg_id] = {
                    "command": command,
                    "purpose": purpose,
                    "start_time": datetime.now().isoformat(),
                    "status": "running",
                    "output_file": str(output_file),
                    "channel": self._origin_channel.get(),
                    "chat_id": self._origin_chat_id.get(),
                    "session_key": self._session_key.get(),
                    "bus": self._bus,
                }
                _bg_file_handles[bg_id] = fh

            asyncio.create_task(_monitor_process(bg_id))

            return (
                f"Background task started.\n"
                f"bash_bg_id: {bg_id}\n"
                f"Command: {command}\n\n"
                f"You will be notified when the task completes. No need to poll.\n"
                f"Use `shell_bg(action='output', bash_bg_id='{bg_id}')` to check output at any time.\n"
                f"Use `shell_bg(action='kill', bash_bg_id='{bg_id}')` to terminate."
            )

        except Exception as e:
            _close_file_handle(fh)
            try:
                output_file.unlink(missing_ok=True)
            except Exception:
                pass
            return f"Error starting background task: {str(e)}"

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        """Kill a subprocess tree and reap to prevent zombies."""
        pid = process.pid
        if sys.platform == "win32":
            # taskkill /T kills the whole tree, /F forces it — use async to
            # avoid blocking the event loop.
            proc = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        else:
            try:
                # Kill the process group (created via preexec_fn=os.setsid)
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                # Fallback: kill just the process if it's not in a group
                try:
                    process.kill()
                except (ProcessLookupError, PermissionError):
                    pass
        # reap
        try:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)
        finally:
            if not _IS_WINDOWS:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ProcessLookupError, ChildProcessError):
                    pass

    def _build_env(self) -> dict[str, str]:
        """Build a minimal environment for subprocess execution.

        On Unix, only HOME/USER/SHELL/LANG/TERM are passed; ``bash -l`` sources the
        user's profile which sets PATH and other essentials.

        On Windows, ``cmd.exe`` has no login-profile mechanism, so a curated
        set of system variables (including PATH) is forwarded.  API keys and
        other secrets are still excluded.
        """
        if _IS_WINDOWS:
            sr = os.environ.get("SYSTEMROOT", r"C:\Windows")
            env = {
                "SYSTEMROOT": sr,
                "COMSPEC": os.environ.get("COMSPEC", f"{sr}\\system32\\cmd.exe"),
                "USERPROFILE": os.environ.get("USERPROFILE", ""),
                "HOMEDRIVE": os.environ.get("HOMEDRIVE", "C:"),
                "HOMEPATH": os.environ.get("HOMEPATH", "\\"),
                "TEMP": os.environ.get("TEMP", f"{sr}\\Temp"),
                "TMP": os.environ.get("TMP", f"{sr}\\Temp"),
                "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                "PATH": os.environ.get("PATH", f"{sr}\\system32;{sr}"),
                "APPDATA": os.environ.get("APPDATA", ""),
                "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
                "ProgramData": os.environ.get("ProgramData", ""),
                "ProgramFiles": os.environ.get("ProgramFiles", ""),
                "ProgramFiles(x86)": os.environ.get("ProgramFiles(x86)", ""),
                "ProgramW6432": os.environ.get("ProgramW6432", ""),
            }
            for key in self.allowed_env_keys:
                val = os.environ.get(key)
                if val is not None:
                    env[key] = val
            return env
        home = os.environ.get("HOME", "/tmp")
        env = {
            "HOME": home,
            "USER": os.environ.get("USER", "unknown"),
            "SHELL": os.environ.get("SHELL", "/bin/bash"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "dumb"),
        }
        for key in self.allowed_env_keys:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        return env

    @staticmethod
    def _resolve_shell() -> str:
        return _resolve_shell()

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        # allow_patterns take priority over deny_patterns so that users can
        # exempt specific commands (e.g. "rm -rf" inside a build directory)
        # from the hardcoded deny list via configuration.
        explicitly_allowed = bool(self.allow_patterns) and any(
            re.search(p, lower) for p in self.allow_patterns
        )
        if not explicitly_allowed:
            for pattern in self.deny_patterns:
                if re.search(pattern, lower):
                    return "Error: Command blocked by deny pattern filter"

            if self.allow_patterns:
                return "Error: Command blocked by allowlist filter (not in allowlist)"

        from nanobot.security.network import contains_internal_url

        if contains_internal_url(cmd):
            # The runner turns this marker into a non-retryable security hint.
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return (
                    "Error: Command blocked by safety guard (path traversal detected)"
                    + _WORKSPACE_BOUNDARY_NOTE
                )

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    # Match against the un-resolved path first.  On Linux,
                    # /dev/stderr is a symlink to /proc/self/fd/2 and
                    # ``Path.resolve()`` would mask the device-file intent.
                    if self._is_benign_device_path(expanded):
                        continue
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue

                if self._is_benign_device_path(str(p)):
                    continue

                media_path = get_media_dir().resolve()
                if (p.is_absolute()
                    and cwd_path not in p.parents
                    and p != cwd_path
                    and media_path not in p.parents
                    and p != media_path
                ):
                    return (
                        "Error: Command blocked by safety guard (path outside working dir)"
                        + _WORKSPACE_BOUNDARY_NOTE
                    )

        return None

    @classmethod
    def _is_benign_device_path(cls, path: str) -> bool:
        """Return True for kernel device files that should never be workspace-blocked."""
        if path in cls._BENIGN_DEVICE_PATHS:
            return True
        return path.startswith("/dev/fd/")

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        # Windows: match drive-root paths like `C:\` as well as `C:\path\to\file`
        # NOTE: `*` is required so `C:\` (nothing after the slash) is still extracted.
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]*", command)
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"(?:^|[\s>'\"])(~[^\s\"'>;|<]*)", command) # POSIX/Windows home shortcut: ~
        return win_paths + posix_paths + home_paths


# ---------------------------------------------------------------------------
# ShellBgTool — unified background task manager (list / output / kill)
# ---------------------------------------------------------------------------


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "Action to perform: 'list' all tasks, read 'output' of a task, "
            "or 'kill' a running task.",
            enum=["list", "output", "kill"],
        ),
        bash_bg_id=StringSchema(
            "The background task ID (required for 'output' and 'kill' actions)."
        ),
        required=["action"],
    )
)
class ShellBgTool(Tool):
    """Manage background shell tasks: list, read output, or kill."""

    _DEFAULT_TAIL = 50
    _session_key: ContextVar[str] = ContextVar("shell_bg_session_key", default="cli:direct")

    def __init__(self, bg_ttl_minutes: int = 120, bg_max_entries: int = 128):
        self._ttl_minutes = bg_ttl_minutes
        self._max_entries = bg_max_entries

    def set_context(
        self,
        bus: Any = None,
        channel: str = "cli",
        chat_id: str = "direct",
        session_key: str = "cli:direct",
    ) -> None:
        self._session_key.set(session_key)

    @property
    def name(self) -> str:
        return "shell_bg"

    @property
    def description(self) -> str:
        return (
            "Manage background shell tasks. Actions: "
            "'list' — show all tasks; "
            "'output' — read last 50 lines of a task's output; "
            "'kill' — terminate a running task."
        )

    @property
    def exclusive(self) -> bool:
        return False

    async def execute(
        self,
        action: str = "list",
        bash_bg_id: str = "",
        **kwargs: Any,
    ) -> str:
        await _cleanup_bg_meta(self._ttl_minutes, self._max_entries)
        if action == "list":
            return await self._list()
        elif action == "output":
            return await self._output(bash_bg_id)
        elif action == "kill":
            return await self._kill(bash_bg_id)
        else:
            return f"Unknown action '{action}'. Use: list, output, kill."

    # -- list ---------------------------------------------------------------

    async def _list(self) -> str:
        current_session = self._session_key.get()
        async with _bg_lock:
            if not _bg_meta:
                return "No background tasks running."

            rows = []
            for bg_id, m in _bg_meta.items():
                if m.get("session_key", "") != current_session:
                    continue
                rows.append(
                    f"  {bg_id}  status={m['status']}  command={m['command']}  "
                    f"purpose={m.get('purpose', '')}"
                )
            if not rows:
                return "No background tasks running."
            return "\n".join(rows)

    # -- session-aware lookup -----------------------------------------------

    def _get_bg_meta(self, bg_id: str) -> dict | None:
        """Return bg meta if it exists and belongs to the current session."""
        meta = _bg_meta.get(bg_id)
        if meta and meta.get("session_key", "") == self._session_key.get():
            return meta
        return None

    # -- output -------------------------------------------------------------

    async def _output(self, bash_bg_id: str) -> str:
        async with _bg_lock:
            meta = self._get_bg_meta(bash_bg_id)
            if not meta:
                return f"Error: Task '{bash_bg_id}' not found"
            # Copy values we need before releasing lock
            output_file_str = meta["output_file"]
            status = meta.get("status", "unknown")

        output_file = Path(output_file_str)
        if not output_file.exists():
            return "No output yet."

        try:
            tail = ShellBgTool._DEFAULT_TAIL

            # Single-pass: count all lines and keep last N in memory
            with open(output_file, "r", encoding="utf-8", errors="replace") as f:
                last_lines: deque[str] = deque(maxlen=tail)
                total_lines = 0
                for line in f:
                    total_lines += 1
                    last_lines.append(line)

            if total_lines == 0:
                return "No output yet."

            start_idx = max(0, total_lines - len(last_lines))

            # Format with line numbers
            _CRLF = "\r\n"
            formatted = []
            for i, line in enumerate(last_lines, start=start_idx + 1):
                formatted.append(f"{i:6}\t{line.rstrip(_CRLF)}")

            parts = ["\n".join(formatted)]

            # Hint about earlier output
            if start_idx > 0:
                parts.append(
                    f"\n\n... {start_idx} earlier line(s) not shown. "
                    f"Read the output file directly to see full output."
                )

            # System reminder about the output file location
            parts.append(
                f"\n\n<system-reminder>\n"
                f"Background task '{bash_bg_id}' (status: {status}).\n"
                f"Full output file: {output_file_str}\n"
                f"Read more with: tail -n 1000 \"{output_file_str}\" "
                f"or use a file-read tool.\n"
                f"</system-reminder>"
            )

            return "\n".join(parts)

        except Exception as e:
            return f"Error reading output: {e}"

    # -- kill ---------------------------------------------------------------

    async def _kill(self, bash_bg_id: str) -> str:
        async with _bg_lock:
            meta = self._get_bg_meta(bash_bg_id)
            if not meta:
                return f"Error: Task '{bash_bg_id}' not found"

            process = _bg_processes.get(bash_bg_id)

            # --- Branch A: process still running, perform kill ---
            if process and process.returncode is None:
                try:
                    await BashTool._kill_process(process)
                    meta["status"] = "killed"
                    meta["exit_code"] = process.returncode
                    meta["end_time"] = datetime.now().isoformat()

                    _close_file_handle(_bg_file_handles.pop(bash_bg_id, None))
                    _bg_processes.pop(bash_bg_id, None)

                    return f"Task '{bash_bg_id}' killed."

                except Exception as e:
                    return f"Error killing task: {e}"

            # --- Branch B: process already dead ---
            if meta.get("status") == "running":
                rc = process.returncode if process else None
                meta["status"] = "completed" if rc == 0 else "failed"
                meta["exit_code"] = rc
                if not meta.get("end_time"):
                    meta["end_time"] = datetime.now().isoformat()
                _bg_processes.pop(bash_bg_id, None)
                _close_file_handle(_bg_file_handles.pop(bash_bg_id, None))

        status = meta.get("status", "unknown")
        return (
            f"Task '{bash_bg_id}' already finished (status: {status}). "
            f"Use `shell_bg(action='output', bash_bg_id='{bash_bg_id}')` to view output."
        )
