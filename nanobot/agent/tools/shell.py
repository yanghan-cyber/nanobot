"""Shell execution tool — BashTool + background task support."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.sandbox import wrap_command
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.config.paths import get_data_dir, get_media_dir
from nanobot.utils.helpers import ensure_dir

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
_bg_file_handles: dict[str, Any] = {}
_last_cleanup: float = 0.0  # monotonic timestamp of last cleanup run
_CLEANUP_INTERVAL = 300.0  # minimum seconds between cleanup sweeps (5 min)


def _bg_output_path(bg_id: str) -> Path:
    return ensure_dir(get_data_dir() / "tool-output" / "shell") / f"{bg_id}_output.log"


def _cleanup_bg_meta(ttl_minutes: int = 120, max_entries: int = 128) -> None:
    """Remove expired or excess completed bg task metadata and their log files.

    Rules:
    1. Running tasks are never removed.
    2. Completed/failed/killed tasks older than ``ttl_minutes`` are evicted.
    3. If still over ``max_entries`` after TTL eviction, remove the oldest.
    """
    global _last_cleanup

    now_monotonic = time.monotonic()
    if now_monotonic - _last_cleanup < _CLEANUP_INTERVAL:
        return

    if not _bg_meta:
        _last_cleanup = now_monotonic
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

    _last_cleanup = now_monotonic


def _remove_bg_entry(bg_id: str) -> None:
    """Remove a bg task's metadata and its output log file."""
    meta = _bg_meta.pop(bg_id, None)
    if meta:
        try:
            Path(meta["output_file"]).unlink(missing_ok=True)
        except Exception:
            pass
    _bg_processes.pop(bg_id, None)
    fh = _bg_file_handles.pop(bg_id, None)
    if fh:
        fh.close()


async def _monitor_process(bg_id: str) -> None:
    """Monitor a background process; update meta on completion."""
    process = _bg_processes.get(bg_id)
    meta = _bg_meta.get(bg_id)
    if not process or not meta:
        return

    try:
        exit_code = await process.wait()
        # Don't overwrite if _kill() already set status to "killed"
        if meta.get("status") != "killed":
            meta["status"] = "completed" if exit_code == 0 else "failed"
        meta["exit_code"] = exit_code
        meta["end_time"] = datetime.now().isoformat()
    finally:
        fh = _bg_file_handles.pop(bg_id, None)
        if fh:
            fh.close()
        _bg_processes.pop(bg_id, None)


# ---------------------------------------------------------------------------
# BashTool
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"


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
                "Timeout in seconds. Increase for long-running commands "
                "like compilation or installation (default 60, max 600)."
            ),
            minimum=1,
            maximum=600,
        ),
        run_in_background=BooleanSchema(
            description="Run command in background. Returns a bg_id for tracking.",
            default=False,
        ),
        required=["command"],
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
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.sandbox = sandbox
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",  # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",  # del /f, del /q
            r"\brmdir\s+/s\b",  # rmdir /s
            r"(?:^|[;&|]\s*)format\b",  # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",  # disk operations
            r"\bdd\s+if=",  # dd
            r">\s*/dev/sd",  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",  # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append

    @property
    def name(self) -> str:
        return "bash"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        return (
            "Execute a shell command and return its output. "
            "Prefer read_file/write_file/edit_file over cat/echo/sed, "
            "and grep/glob over shell find/grep. "
            "Use -y or --yes flags to avoid interactive prompts. "
            "Output is truncated at 10 000 chars; timeout defaults to 60s."
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

        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        shell = self._resolve_shell()

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
            process = await asyncio.create_subprocess_exec(
                shell,
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
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

        fh = open(output_file, "wb")

        process = await asyncio.create_subprocess_exec(
            shell,
            "-c",
            command,
            stdout=fh,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
        )

        _bg_processes[bg_id] = process
        _bg_meta[bg_id] = {
            "command": command,
            "purpose": purpose,
            "start_time": datetime.now().isoformat(),
            "status": "running",
            "output_file": str(output_file),
        }
        _bg_file_handles[bg_id] = fh

        asyncio.create_task(_monitor_process(bg_id))

        return (
            f"Background task started.\n"
            f"bash_bg_id: {bg_id}\n"
            f"Command: {command}\n\n"
            f"Use `shell_bg(action='output', bash_bg_id='{bg_id}')` to read output.\n"
            f"Use `shell_bg(action='kill', bash_bg_id='{bg_id}')` to terminate.\n"
            f"Use `shell_bg(action='list')` to see all background tasks."
        )

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
                os.killpg(pid, 9)  # kill the whole process group
            except (ProcessLookupError, PermissionError):
                pass
        # reap
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        finally:
            if not _IS_WINDOWS:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ProcessLookupError, ChildProcessError):
                    pass

    def _build_env(self) -> dict[str, str]:
        """Build a minimal environment for subprocess execution.

        On Unix, only HOME/LANG/TERM are passed; ``bash -l`` sources the
        user's profile which sets PATH and other essentials.

        On Windows, ``cmd.exe`` has no login-profile mechanism, so a curated
        set of system variables (including PATH) is forwarded.  API keys and
        other secrets are still excluded.
        """
        if _IS_WINDOWS:
            sr = os.environ.get("SYSTEMROOT", r"C:\Windows")
            return {
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
        home = os.environ.get("HOME", "/tmp")
        return {
            "HOME": home,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.environ.get("PATH", ""),
            "TERM": os.environ.get("TERM", "dumb"),
            "PYTHONIOENCODING": "utf-8",
        }

    @staticmethod
    def _resolve_shell() -> str:
        return _resolve_shell()

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        from nanobot.security.network import contains_internal_url

        if contains_internal_url(cmd):
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue

                media_path = get_media_dir().resolve()
                if (
                    p.is_absolute()
                    and cwd_path not in p.parents
                    and p != cwd_path
                    and media_path not in p.parents
                    and p != media_path
                ):
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        # Windows: match drive-root paths like `C:\` as well as `C:\path\to\file`
        # NOTE: `*` is required so `C:\` (nothing after the slash) is still extracted.
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]*", command)
        posix_paths = re.findall(
            r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command
        )  # POSIX: /absolute only
        home_paths = re.findall(
            r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command
        )  # POSIX/Windows home shortcut: ~
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

    def __init__(self, bg_ttl_minutes: int = 120, bg_max_entries: int = 128):
        self._ttl_minutes = bg_ttl_minutes
        self._max_entries = bg_max_entries

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
        _cleanup_bg_meta(self._ttl_minutes, self._max_entries)
        if action == "list":
            return self._list()
        elif action == "output":
            return self._output(bash_bg_id)
        elif action == "kill":
            return await self._kill(bash_bg_id)
        else:
            return f"Unknown action '{action}'. Use: list, output, kill."

    # -- list ---------------------------------------------------------------

    @staticmethod
    def _list() -> str:
        if not _bg_meta:
            return "No background tasks running."

        rows = []
        for bg_id, m in _bg_meta.items():
            rows.append(
                f"  {bg_id}  status={m['status']}  command={m['command']}  "
                f"purpose={m.get('purpose', '')}"
            )
        return "\n".join(rows)

    # -- output -------------------------------------------------------------

    @staticmethod
    def _output(bash_bg_id: str) -> str:
        meta = _bg_meta.get(bash_bg_id)
        if not meta:
            return f"Error: Task '{bash_bg_id}' not found"

        output_file = Path(meta["output_file"])
        if not output_file.exists():
            return "No output yet."

        try:
            with open(output_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            total_lines = len(all_lines)
            if total_lines == 0:
                return "No output yet."

            # Tail-style: take last N lines (default 50)
            tail = ShellBgTool._DEFAULT_TAIL
            start_idx = max(0, total_lines - tail)
            selected = all_lines[start_idx:]

            # Format with line numbers
            _CRLF = "\r\n"
            formatted = []
            for i, line in enumerate(selected, start=start_idx + 1):
                formatted.append(f"{i:6}\t{line.rstrip(_CRLF)}")

            parts = ["\n".join(formatted)]

            # Hint about earlier output
            if start_idx > 0:
                parts.append(
                    f"\n\n... {start_idx} earlier line(s) not shown. "
                    f"Read the output file directly to see full output."
                )

            # System reminder about the output file location
            status = meta.get("status", "unknown")
            parts.append(
                f"\n\n<system-reminder>\n"
                f"Background task '{bash_bg_id}' (status: {status}).\n"
                f"Full output file: {meta['output_file']}\n"
                f"Read more with: tail -n 1000 \"{meta['output_file']}\" "
                f"or use a file-read tool.\n"
                f"</system-reminder>"
            )

            return "\n".join(parts)

        except Exception as e:
            return f"Error reading output: {e}"

    # -- kill ---------------------------------------------------------------

    @staticmethod
    async def _kill(bash_bg_id: str) -> str:
        meta = _bg_meta.get(bash_bg_id)
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

                fh = _bg_file_handles.pop(bash_bg_id, None)
                if fh:
                    fh.close()
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
            fh = _bg_file_handles.pop(bash_bg_id, None)
            if fh:
                fh.close()

        status = meta.get("status", "unknown")
        return (
            f"Task '{bash_bg_id}' already finished (status: {status}). "
            f"Use `shell_bg(action='output', bash_bg_id='{bash_bg_id}')` to view output."
        )
