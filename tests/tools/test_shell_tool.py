"""Tests for BashTool (shell tool optimization).

Covers:
- _resolve_shell() platform detection
- Rename from ExecTool to BashTool
- create_subprocess_exec with shell -c
- UTF-8 encoding env vars
- Windows taskkill process termination
- Background task lifecycle (spawn → output → kill)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.tools.shell import BashTool


# ---------------------------------------------------------------------------
# _resolve_shell() — Linux/macOS
# ---------------------------------------------------------------------------


class TestResolveShellLinux:
    """_resolve_shell() on non-Windows platforms."""

    @patch("nanobot.agent.tools.shell.sys")
    @patch("nanobot.agent.tools.shell.shutil")
    def test_returns_bin_bash_when_found(self, mock_shutil, mock_sys):
        mock_sys.platform = "linux"
        mock_shutil.which.return_value = "/bin/bash"
        result = BashTool._resolve_shell()
        assert result == "/bin/bash"

    @patch("nanobot.agent.tools.shell.sys")
    @patch("nanobot.agent.tools.shell.shutil")
    def test_falls_back_to_bin_sh(self, mock_shutil, mock_sys):
        mock_sys.platform = "linux"
        mock_shutil.which.side_effect = lambda x: "/bin/sh" if x == "bash" else None
        # _resolve_shell checks "bash" first via which
        result = BashTool._resolve_shell()
        # Should return some shell (either /bin/bash from which or /bin/sh)
        assert result is not None
        assert "bash" in result or "sh" in result


# ---------------------------------------------------------------------------
# _resolve_shell() — Windows
# ---------------------------------------------------------------------------


class TestResolveShellWindows:
    """_resolve_shell() on Windows: Git Bash detection and fallback chain."""

    @patch("nanobot.agent.tools.shell.sys")
    @patch("nanobot.agent.tools.shell.shutil")
    def test_derives_from_git_exe_path(self, mock_shutil, mock_sys):
        """Priority 1: derive bash.exe from git.exe location."""
        mock_sys.platform = "win32"
        # git.exe found, bash.exe derived from it
        git_path = r"C:\Program Files\Git\cmd\git.exe"
        bash_path = r"C:\Program Files\Git\bin\bash.exe"
        mock_shutil.which.side_effect = lambda x: git_path if x == "git" else None
        with patch("nanobot.agent.tools.shell.Path") as mock_path_cls:
            mock_git = MagicMock()
            mock_parent = MagicMock()
            mock_bash = MagicMock()
            mock_bash.exists.return_value = True
            mock_parent.__truediv__ = MagicMock(
                side_effect=lambda p: mock_bash if p == "bin/bash.exe" else MagicMock()
            )
            mock_git.resolve.return_value.parents = [mock_parent]
            mock_path_cls.return_value = mock_git
            result = BashTool._resolve_shell()
        assert result is not None

    @patch("nanobot.agent.tools.shell.sys")
    @patch("nanobot.agent.tools.shell.shutil")
    def test_checks_common_install_paths(self, mock_shutil, mock_sys):
        """Priority 2: check common Git Bash install locations."""
        mock_sys.platform = "win32"
        # No git.exe found
        mock_shutil.which.return_value = None
        with patch("nanobot.agent.tools.shell.os.path.exists") as mock_exists:
            # First common path exists
            mock_exists.side_effect = lambda p: p == r"C:\Program Files\Git\bin\bash.exe"
            result = BashTool._resolve_shell()
        assert result == r"C:\Program Files\Git\bin\bash.exe"

    @patch("nanobot.agent.tools.shell.sys")
    @patch("nanobot.agent.tools.shell.shutil")
    def test_falls_back_to_pwsh(self, mock_shutil, mock_sys):
        """Priority 3: fallback to PowerShell Core."""
        mock_sys.platform = "win32"
        mock_shutil.which.side_effect = lambda x: {
            "git": None,
            "pwsh": r"C:\Program Files\PowerShell\7\pwsh.exe",
        }.get(x)
        with patch("nanobot.agent.tools.shell.os.path.exists", return_value=False):
            result = BashTool._resolve_shell()
        assert result == r"C:\Program Files\PowerShell\7\pwsh.exe"

    @patch("nanobot.agent.tools.shell.sys")
    @patch("nanobot.agent.tools.shell.shutil")
    def test_falls_back_to_powershell(self, mock_shutil, mock_sys):
        """Priority 4: fallback to Windows PowerShell."""
        mock_sys.platform = "win32"
        mock_shutil.which.side_effect = lambda x: {
            "git": None,
            "pwsh": None,
            "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        }.get(x)
        with patch("nanobot.agent.tools.shell.os.path.exists", return_value=False):
            result = BashTool._resolve_shell()
        assert "powershell" in result.lower()


# ---------------------------------------------------------------------------
# Rename: ExecTool → BashTool
# ---------------------------------------------------------------------------


class TestBashToolRename:
    """Verify the class and tool name have been renamed."""

    def test_class_name_is_bash_tool(self):
        assert BashTool.__name__ == "BashTool"

    def test_tool_name_is_bash(self):
        tool = BashTool()
        assert tool.name == "bash"

    def test_exclusive_remains_true(self):
        tool = BashTool()
        assert tool.exclusive is True


# ---------------------------------------------------------------------------
# Execution: create_subprocess_exec + UTF-8 env vars
# ---------------------------------------------------------------------------


class TestBashToolExecution:
    """Test that BashTool uses exec-style invocation and UTF-8 encoding."""

    @pytest.mark.asyncio
    async def test_uses_create_subprocess_exec(self):
        """Should use create_subprocess_exec, not create_subprocess_shell."""
        tool = BashTool()
        with patch("nanobot.agent.tools.shell.asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.communicate.return_value = (b"hello\n", b"")
            mock_process.returncode = 0
            mock_exec.return_value = mock_process
            with patch.object(tool, "_resolve_shell", return_value="/bin/bash"):
                await tool.execute(command="echo hello")
            mock_exec.assert_called_once()
            # First arg should be the shell path
            call_args = mock_exec.call_args
            assert call_args[0][0] == "/bin/bash"
            assert call_args[0][1] == "-l"
            assert call_args[0][2] == "-c"

    @pytest.mark.asyncio
    async def test_build_env_is_minimal(self):
        """Should build a minimal env with HOME, LANG, TERM."""
        tool = BashTool()
        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", False),
            patch("nanobot.agent.tools.shell.asyncio.create_subprocess_exec") as mock_exec,
        ):
            mock_process = AsyncMock()
            mock_process.communicate.return_value = (b"hello\n", b"")
            mock_process.returncode = 0
            mock_exec.return_value = mock_process
            with patch.object(tool, "_resolve_shell", return_value="/bin/bash"):
                await tool.execute(command="echo hello")
                call_kwargs = mock_exec.call_args
                env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
                assert env is not None
                assert env.get("LANG") == "C.UTF-8"
                assert "HOME" in env
                assert "TERM" in env


# ---------------------------------------------------------------------------
# Windows process termination: taskkill
# ---------------------------------------------------------------------------


class TestWindowsTaskkill:
    """On Windows, timeout should use async taskkill /F /T /PID to kill process tree."""

    @pytest.mark.asyncio
    async def test_windows_timeout_uses_taskkill(self):
        tool = BashTool(timeout=1)
        with patch("nanobot.agent.tools.shell.asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.communicate.side_effect = asyncio.TimeoutError()
            mock_process.pid = 12345
            mock_process.wait = AsyncMock()
            mock_exec.return_value = mock_process

            mock_taskkill = AsyncMock()
            mock_taskkill.wait = AsyncMock()

            def fake_create_subprocess_exec(*args, **kwargs):
                if args[0] == "taskkill":
                    return mock_taskkill
                return mock_process

            mock_exec.side_effect = fake_create_subprocess_exec

            with patch("nanobot.agent.tools.shell.sys") as mock_sys:
                mock_sys.platform = "win32"
                with patch.object(tool, "_resolve_shell", return_value="/bin/bash"):
                    result = await tool.execute(command="sleep 999", timeout=1)
                # Verify taskkill was spawned with correct args
                taskkill_calls = [
                    c for c in mock_exec.call_args_list if c[0][0] == "taskkill"
                ]
                assert len(taskkill_calls) == 1
                args = taskkill_calls[0][0]
                assert args[0] == "taskkill"
                assert "/F" in args
                assert "/T" in args
                assert "12345" in args
            assert "timed out" in result.lower()


# ---------------------------------------------------------------------------
# Background tasks: lifecycle tests
# ---------------------------------------------------------------------------


class TestBackgroundTasks:
    """Test background task spawn, output reading, and killing."""

    @pytest.mark.asyncio
    async def test_background_spawn_returns_bg_id(self):
        """Spawning a background task should return a bg_id."""
        tool = BashTool()
        with patch("nanobot.agent.tools.shell.asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.pid = 99999
            mock_exec.return_value = mock_process
            with patch.object(tool, "_resolve_shell", return_value="/bin/bash"):
                with patch("nanobot.agent.tools.shell.get_data_dir") as mock_dir:
                    mock_dir.return_value = Path("/tmp/nanobot-test")
                    with patch("builtins.open", MagicMock()):
                        result = await tool.execute(
                            command="sleep 100",
                            run_in_background=True,
                            purpose="test sleep",
                        )
        assert "bash_bg_" in result
        assert "shell_bg" in result


# ---------------------------------------------------------------------------
# ShellBgTool — merged background task manager
# ---------------------------------------------------------------------------


class TestShellBgTool:
    """Test the unified ShellBgTool (list, output, kill actions)."""

    # -- basic properties ----------------------------------------------------

    def test_tool_name(self):
        from nanobot.agent.tools.shell import ShellBgTool

        assert ShellBgTool().name == "shell_bg"

    def test_exclusive_is_false(self):
        from nanobot.agent.tools.shell import ShellBgTool

        assert ShellBgTool().exclusive is False

    # -- session isolation --------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_filters_by_session(self):
        """list() only returns tasks matching current session_key."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        saved = _bg_meta.copy()
        _bg_meta.clear()
        _bg_meta["bash_bg_s1"] = {
            "command": "task1", "purpose": "a",
            "start_time": "", "status": "running", "output_file": "/tmp/s1.log",
            "session_key": "feishu:chat_111",
        }
        _bg_meta["bash_bg_s2"] = {
            "command": "task2", "purpose": "b",
            "start_time": "", "status": "completed", "output_file": "/tmp/s2.log",
            "session_key": "feishu:chat_222",
        }
        try:
            tool.set_context(session_key="feishu:chat_111")
            result_a = await tool.execute(action="list")
            assert "bash_bg_s1" in result_a
            assert "bash_bg_s2" not in result_a

            tool.set_context(session_key="feishu:chat_222")
            result_b = await tool.execute(action="list")
            assert "bash_bg_s2" in result_b
            assert "bash_bg_s1" not in result_b
        finally:
            _bg_meta.clear()
            _bg_meta.update(saved)

    @pytest.mark.asyncio
    async def test_output_rejects_other_session(self):
        """output() returns 'not found' for tasks from another session."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        tool.set_context(session_key="feishu:chat_111")
        saved = _bg_meta.copy()
        _bg_meta.clear()
        _bg_meta["bash_bg_other"] = {
            "command": "other", "purpose": "test",
            "start_time": "", "status": "completed", "output_file": "/tmp/o.log",
            "session_key": "feishu:chat_999",
        }
        try:
            result = await tool.execute(action="output", bash_bg_id="bash_bg_other")
            assert "not found" in result.lower()
        finally:
            _bg_meta.clear()
            _bg_meta.update(saved)

    @pytest.mark.asyncio
    async def test_kill_rejects_other_session(self):
        """kill() returns 'not found' for tasks from another session."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        tool.set_context(session_key="feishu:chat_111")
        saved = _bg_meta.copy()
        _bg_meta.clear()
        _bg_meta["bash_bg_other_kill"] = {
            "command": "other", "purpose": "test",
            "start_time": "", "status": "completed", "output_file": "/tmp/ok.log",
            "session_key": "feishu:chat_999",
        }
        try:
            result = await tool.execute(action="kill", bash_bg_id="bash_bg_other_kill")
            assert "not found" in result.lower()
        finally:
            _bg_meta.clear()
            _bg_meta.update(saved)

    # -- action=list ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_returns_running_tasks(self):
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        bg_id = "bash_bg_listest"
        _bg_meta[bg_id] = {
            "command": "sleep 10",
            "purpose": "test list",
            "start_time": "2026-01-01T00:00:00",
            "status": "running",
            "output_file": "/tmp/test_output.log",
            "session_key": "cli:direct",
        }
        try:
            result = await tool.execute(action="list")
            assert bg_id in result
            assert "running" in result
            assert "sleep 10" in result
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_list_empty(self):
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        saved = _bg_meta.copy()
        _bg_meta.clear()
        try:
            result = await tool.execute(action="list")
            assert "No background tasks" in result
        finally:
            _bg_meta.update(saved)

    @pytest.mark.asyncio
    async def test_list_multiple_tasks(self):
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        saved = _bg_meta.copy()
        _bg_meta.clear()
        _bg_meta["bash_bg_aaa"] = {
            "command": "cmd_a", "purpose": "a",
            "start_time": "", "status": "running", "output_file": "/tmp/a.log",
            "session_key": "cli:direct",
        }
        _bg_meta["bash_bg_bbb"] = {
            "command": "cmd_b", "purpose": "b",
            "start_time": "", "status": "completed", "output_file": "/tmp/b.log",
            "session_key": "cli:direct",
        }
        try:
            result = await tool.execute(action="list")
            assert "bash_bg_aaa" in result
            assert "bash_bg_bbb" in result
            assert "cmd_a" in result
            assert "cmd_b" in result
        finally:
            _bg_meta.clear()
            _bg_meta.update(saved)

    # -- action=output -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_output_shows_last_50_lines(self, tmp_path):
        """Default: show last 50 lines of output."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        bg_id = "bash_bg_tail50"
        log_file = tmp_path / "output.log"
        lines = [f"line {i}" for i in range(80)]
        log_file.write_text("\n".join(lines), encoding="utf-8")

        _bg_meta[bg_id] = {
            "command": "echo test", "purpose": "test",
            "start_time": "", "status": "completed",
            "output_file": str(log_file),
            "session_key": "cli:direct",
        }
        try:
            result = await tool.execute(action="output", bash_bg_id=bg_id)
            assert "line 79" in result
            assert "line 30" in result
            assert "line 29" not in result
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_output_fewer_than_50_lines(self, tmp_path):
        """< 50 lines: show all."""
        """If file has fewer than 50 lines, show all of them."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        bg_id = "bash_bg_short"
        log_file = tmp_path / "output.log"
        log_file.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        _bg_meta[bg_id] = {
            "command": "echo", "purpose": "test",
            "start_time": "", "status": "completed",
            "output_file": str(log_file),
            "session_key": "cli:direct",
        }
        try:
            result = await tool.execute(action="output", bash_bg_id=bg_id)
            assert "alpha" in result
            assert "beta" in result
            assert "gamma" in result
            assert "earlier" not in result.lower()
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_output_unknown_task(self):
        from nanobot.agent.tools.shell import ShellBgTool

        tool = ShellBgTool()
        result = await tool.execute(action="output", bash_bg_id="bash_bg_nope")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_output_file_not_yet_exists(self, tmp_path):
        """Task exists but output file hasn't been created yet."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        bg_id = "bash_bg_no_file"
        _bg_meta[bg_id] = {
            "command": "sleep 999", "purpose": "test",
            "start_time": "", "status": "running",
            "output_file": str(tmp_path / "not_yet.log"),
            "session_key": "cli:direct",
        }
        try:
            result = await tool.execute(action="output", bash_bg_id=bg_id)
            assert "No output yet" in result
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_output_empty_file(self, tmp_path):
        """Output file exists but is empty."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        bg_id = "bash_bg_empty"
        log_file = tmp_path / "output.log"
        log_file.write_text("", encoding="utf-8")

        _bg_meta[bg_id] = {
            "command": "true", "purpose": "test",
            "start_time": "", "status": "completed",
            "output_file": str(log_file),
            "session_key": "cli:direct",
        }
        try:
            result = await tool.execute(action="output", bash_bg_id=bg_id)
            assert "No output yet" in result
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_output_system_reminder_contains_status_and_path(self, tmp_path):
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        bg_id = "bash_bg_remind"
        log_file = tmp_path / "output.log"
        log_file.write_text("data\n", encoding="utf-8")

        _bg_meta[bg_id] = {
            "command": "echo", "purpose": "test",
            "start_time": "", "status": "completed",
            "output_file": str(log_file),
            "session_key": "cli:direct",
        }
        try:
            result = await tool.execute(action="output", bash_bg_id=bg_id)
            assert "<system-reminder>" in result
            assert "completed" in result
            assert str(log_file) in result
            assert "tail" in result.lower()
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_output_earlier_lines_hint(self, tmp_path):
        """When >50 lines, show 'N earlier line(s)' hint."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        bg_id = "bash_bg_earlier"
        log_file = tmp_path / "output.log"
        lines = [f"line {i}" for i in range(60)]
        log_file.write_text("\n".join(lines), encoding="utf-8")

        _bg_meta[bg_id] = {
            "command": "test", "purpose": "test",
            "start_time": "", "status": "completed",
            "output_file": str(log_file),
            "session_key": "cli:direct",
        }
        try:
            result = await tool.execute(action="output", bash_bg_id=bg_id)
            assert "10 earlier" in result
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_output_line_numbers_start_correctly(self, tmp_path):
        """Line numbers should reflect actual position in file, not start at 1."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta

        tool = ShellBgTool()
        bg_id = "bash_bg_lnum"
        log_file = tmp_path / "output.log"
        lines = [f"line {i}" for i in range(80)]
        log_file.write_text("\n".join(lines), encoding="utf-8")

        _bg_meta[bg_id] = {
            "command": "test", "purpose": "test",
            "start_time": "", "status": "completed",
            "output_file": str(log_file),
            "session_key": "cli:direct",
        }
        try:
            result = await tool.execute(action="output", bash_bg_id=bg_id)
            # First displayed line should be line 31 (1-indexed), not line 1
            assert "31\tline 30" in result
        finally:
            _bg_meta.pop(bg_id, None)

    # -- action=kill ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_kill_running_task(self):
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta, _bg_processes, _bg_file_handles

        tool = ShellBgTool()
        bg_id = "bash_bg_kiltest"
        mock_process = AsyncMock()
        mock_process.pid = 54321
        mock_process.returncode = None
        _bg_processes[bg_id] = mock_process
        mock_fh = MagicMock()
        _bg_file_handles[bg_id] = mock_fh
        _bg_meta[bg_id] = {
            "command": "sleep 999", "purpose": "test kill",
            "start_time": "", "status": "running",
            "output_file": "/tmp/kill_test.log",
            "session_key": "cli:direct",
        }
        try:
            with patch.object(
                BashTool, "_kill_process", return_value=None
            ) as mock_kill:
                result = await tool.execute(action="kill", bash_bg_id=bg_id)
                mock_kill.assert_called_once()
            assert "killed" in result.lower()
            assert bg_id in result
            mock_fh.close.assert_called_once()
            assert bg_id not in _bg_processes
        finally:
            _bg_processes.pop(bg_id, None)
            _bg_file_handles.pop(bg_id, None)
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_kill_unknown_task(self):
        from nanobot.agent.tools.shell import ShellBgTool

        tool = ShellBgTool()
        result = await tool.execute(action="kill", bash_bg_id="bash_bg_nonexistent")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_kill_already_completed_task(self):
        """Killing a task that already finished should report not-running."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta, _bg_processes

        tool = ShellBgTool()
        bg_id = "bash_bg_done"
        _bg_meta[bg_id] = {
            "command": "echo done", "purpose": "test",
            "start_time": "", "status": "completed",
            "output_file": "/tmp/done.log",
            "session_key": "cli:direct",
        }
        # No entry in _bg_processes (already cleaned up)
        try:
            result = await tool.execute(action="kill", bash_bg_id=bg_id)
            assert "already finished" in result.lower()
            assert "completed" in result
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_kill_process_exception(self):
        """If _kill_process throws, should return error message."""
        from nanobot.agent.tools.shell import ShellBgTool, _bg_meta, _bg_processes

        tool = ShellBgTool()
        bg_id = "bash_bg_kfail"
        mock_process = AsyncMock()
        mock_process.pid = 99999
        mock_process.returncode = None
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "sleep", "purpose": "test",
            "start_time": "", "status": "running",
            "output_file": "/tmp/kfail.log",
            "session_key": "cli:direct",
        }
        try:
            with patch.object(
                BashTool, "_kill_process",
                side_effect=PermissionError("access denied"),
            ):
                result = await tool.execute(action="kill", bash_bg_id=bg_id)
            assert "Error" in result
            assert "access denied" in result
        finally:
            _bg_processes.pop(bg_id, None)
            _bg_meta.pop(bg_id, None)

    # -- edge cases ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self):
        from nanobot.agent.tools.shell import ShellBgTool

        tool = ShellBgTool()
        result = await tool.execute(action="pause")
        assert "Unknown action" in result

    # -- _monitor_process ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_monitor_process_sets_completed_on_exit_0(self):
        from nanobot.agent.tools.shell import _bg_meta, _bg_processes, _monitor_process

        bg_id = "bash_bg_mon0"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_process.returncode = 0
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "true", "purpose": "test",
            "start_time": "", "status": "running", "output_file": "/tmp/m.log",
        }
        try:
            await _monitor_process(bg_id)
            assert _bg_meta[bg_id]["status"] == "completed"
            assert _bg_meta[bg_id]["exit_code"] == 0
            assert bg_id not in _bg_processes
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_monitor_process_sets_failed_on_nonzero_exit(self):
        from nanobot.agent.tools.shell import _bg_meta, _bg_processes, _monitor_process

        bg_id = "bash_bg_mon1"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=1)
        mock_process.returncode = 1
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "false", "purpose": "test",
            "start_time": "", "status": "running", "output_file": "/tmp/m.log",
        }
        try:
            await _monitor_process(bg_id)
            assert _bg_meta[bg_id]["status"] == "failed"
            assert _bg_meta[bg_id]["exit_code"] == 1
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_monitor_process_cleans_up_file_handle(self):
        from nanobot.agent.tools.shell import _bg_meta, _bg_processes, _bg_file_handles, _monitor_process

        bg_id = "bash_bg_mon_fh"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_process.returncode = 0
        _bg_processes[bg_id] = mock_process
        mock_fh = MagicMock()
        _bg_file_handles[bg_id] = mock_fh
        _bg_meta[bg_id] = {
            "command": "true", "purpose": "test",
            "start_time": "", "status": "running", "output_file": "/tmp/m.log",
        }
        try:
            await _monitor_process(bg_id)
            mock_fh.close.assert_called_once()
            assert bg_id not in _bg_file_handles
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_monitor_process_noop_on_missing_meta(self):
        from nanobot.agent.tools.shell import _monitor_process

        # Should not raise
        await _monitor_process("bash_bg_nonexistent")


class TestMonitorProcessNotification:
    """Test that _monitor_process sends InboundMessage on completion."""

    @pytest.mark.asyncio
    async def test_sends_inbound_on_completion(self):
        from nanobot.agent.tools.shell import (
            _bg_meta, _bg_processes, _bg_file_handles, _monitor_process,
        )
        from nanobot.bus.events import InboundMessage

        mock_bus = AsyncMock()
        bg_id = "bash_bg_notif1"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_process.returncode = 0
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "echo done",
            "purpose": "test",
            "start_time": "",
            "status": "running",
            "output_file": "/tmp/notif.log",
            "channel": "feishu",
            "chat_id": "chat_xyz",
            "session_key": "feishu:chat_xyz",
            "bus": mock_bus,
        }
        try:
            await _monitor_process(bg_id)
            assert _bg_meta[bg_id]["status"] == "completed"
            mock_bus.publish_inbound.assert_called_once()
            msg = mock_bus.publish_inbound.call_args[0][0]
            assert isinstance(msg, InboundMessage)
            assert msg.channel == "system"
            assert msg.sender_id == "bg_task"
            assert msg.chat_id == "feishu:chat_xyz"
            assert msg.session_key_override == "feishu:chat_xyz"
            assert msg.metadata["injected_event"] == "bg_task_result"
            assert msg.metadata["bg_id"] == bg_id
            assert "bash_bg_notif1" in msg.content
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_sends_notification_on_nonzero_exit(self):
        """Notification fires even when process exits with non-zero."""
        from nanobot.agent.tools.shell import (
            _bg_meta, _bg_processes, _monitor_process,
        )

        mock_bus = AsyncMock()
        bg_id = "bash_bg_notif_fail"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=1)
        mock_process.returncode = 1
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "false",
            "purpose": "test",
            "start_time": "",
            "status": "running",
            "output_file": "/tmp/notif_fail.log",
            "channel": "feishu",
            "chat_id": "chat_xyz",
            "session_key": "feishu:chat_xyz",
            "bus": mock_bus,
        }
        try:
            await _monitor_process(bg_id)
            assert _bg_meta[bg_id]["status"] == "failed"
            mock_bus.publish_inbound.assert_called_once()
            msg = mock_bus.publish_inbound.call_args[0][0]
            assert msg.metadata["bg_id"] == bg_id
            assert "exit_code" in msg.content or "1" in msg.content
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_sends_notification_on_wait_exception(self):
        """Notification fires when process.wait() raises."""
        from nanobot.agent.tools.shell import (
            _bg_meta, _bg_processes, _monitor_process,
        )

        mock_bus = AsyncMock()
        bg_id = "bash_bg_notif_exc"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(side_effect=RuntimeError("process died"))
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "crash",
            "purpose": "test",
            "start_time": "",
            "status": "running",
            "output_file": "/tmp/notif_exc.log",
            "channel": "feishu",
            "chat_id": "chat_xyz",
            "session_key": "feishu:chat_xyz",
            "bus": mock_bus,
        }
        try:
            await _monitor_process(bg_id)
            assert _bg_meta[bg_id]["status"] == "failed"
            assert _bg_meta[bg_id]["error"] == "process died"
            mock_bus.publish_inbound.assert_called_once()
            msg = mock_bus.publish_inbound.call_args[0][0]
            assert msg.metadata["bg_id"] == bg_id
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_no_notification_without_bus(self):
        """If bus is None (e.g. CLI mode without set_context), don't crash."""
        from nanobot.agent.tools.shell import (
            _bg_meta, _bg_processes, _monitor_process,
        )

        bg_id = "bash_bg_nobus"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_process.returncode = 0
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "true", "purpose": "test",
            "start_time": "", "status": "running", "output_file": "/tmp/n.log",
            "channel": "cli", "chat_id": "direct",
            "session_key": "cli:direct", "bus": None,
        }
        try:
            await _monitor_process(bg_id)
            assert _bg_meta[bg_id]["status"] == "completed"
        finally:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_no_notification_without_origin(self):
        """If meta has no channel/session_key keys (old-style), don't crash."""
        from nanobot.agent.tools.shell import (
            _bg_meta, _bg_processes, _monitor_process,
        )

        bg_id = "bash_bg_noorigin"
        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_process.returncode = 0
        _bg_processes[bg_id] = mock_process
        _bg_meta[bg_id] = {
            "command": "true", "purpose": "test",
            "start_time": "", "status": "running", "output_file": "/tmp/n.log",
        }
        try:
            await _monitor_process(bg_id)
            assert _bg_meta[bg_id]["status"] == "completed"
        finally:
            _bg_meta.pop(bg_id, None)


# ---------------------------------------------------------------------------
# BashTool set_context / origin ContextVar
# ---------------------------------------------------------------------------


class TestBashToolSetContext:
    """Test BashTool set_context stores origin info via ContextVar."""

    def test_set_context_stores_values(self):
        tool = BashTool()
        tool.set_context(
            bus=None,
            channel="feishu",
            chat_id="chat_abc",
            session_key="feishu:chat_abc",
        )
        assert tool._origin_channel.get() == "feishu"
        assert tool._origin_chat_id.get() == "chat_abc"
        assert tool._session_key.get() == "feishu:chat_abc"

    def test_set_context_defaults(self):
        tool = BashTool()
        assert tool._origin_channel.get() == "cli"
        assert tool._origin_chat_id.get() == "direct"
        assert tool._session_key.get() == "cli:direct"

    def test_set_context_stores_bus(self):
        mock_bus = MagicMock()
        tool = BashTool()
        tool.set_context(
            bus=mock_bus,
            channel="cli",
            chat_id="direct",
            session_key="cli:direct",
        )
        assert tool._bus is mock_bus


# ---------------------------------------------------------------------------
# TestBackgroundOrigin — background tasks store origin info
# ---------------------------------------------------------------------------


class TestBackgroundOrigin:
    """Test that background tasks store origin info in _bg_meta."""

    @pytest.mark.asyncio
    async def test_bg_meta_stores_origin(self):
        from nanobot.agent.tools.shell import _bg_meta

        tool = BashTool()
        mock_bus = MagicMock()
        tool.set_context(
            bus=mock_bus,
            channel="feishu",
            chat_id="chat_123",
            session_key="feishu:chat_123",
        )
        with patch("nanobot.agent.tools.shell.asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.pid = 88888
            mock_exec.return_value = mock_process
            with patch.object(tool, "_resolve_shell", return_value="/bin/bash"):
                with patch("nanobot.agent.tools.shell.get_data_dir") as mock_dir:
                    mock_dir.return_value = Path("/tmp/nanobot-test")
                    with patch("builtins.open", MagicMock()):
                        await tool.execute(
                            command="sleep 100",
                            run_in_background=True,
                            purpose="test origin",
                        )
        bg_ids = [k for k in _bg_meta if k.startswith("bash_bg_")]
        assert len(bg_ids) >= 1
        meta = _bg_meta[bg_ids[-1]]
        assert meta["channel"] == "feishu"
        assert meta["chat_id"] == "chat_123"
        assert meta["session_key"] == "feishu:chat_123"
        assert meta["bus"] is mock_bus
        for bg_id in bg_ids:
            _bg_meta.pop(bg_id, None)

    @pytest.mark.asyncio
    async def test_bg_return_message_mentions_notification(self):
        tool = BashTool()
        with patch("nanobot.agent.tools.shell.asyncio.create_subprocess_exec") as mock_exec:
            mock_process = AsyncMock()
            mock_process.pid = 77777
            mock_exec.return_value = mock_process
            with patch.object(tool, "_resolve_shell", return_value="/bin/bash"):
                with patch("nanobot.agent.tools.shell.get_data_dir") as mock_dir:
                    mock_dir.return_value = Path("/tmp/nanobot-test")
                    with patch("builtins.open", MagicMock()):
                        result = await tool.execute(
                            command="sleep 100",
                            run_in_background=True,
                            purpose="test msg",
                        )
        assert "notified" in result.lower() or "notification" in result.lower()


# ---------------------------------------------------------------------------
# TestBgTaskAnnounceTemplate — background task completion template
# ---------------------------------------------------------------------------


class TestBgTaskAnnounceTemplate:
    """Test the bg_task_announce.md template renders correctly."""

    def test_renders_with_exit_code_0(self):
        from nanobot.utils.prompt_templates import render_template

        result = render_template(
            "agent/bg_task_announce.md",
            bg_id="bash_bg_abc123",
            exit_code=0,
            command="npm run build",
        )
        assert "bash_bg_abc123" in result
        assert "completed" in result.lower()
        assert "npm run build" in result
        assert "shell_bg" in result

    def test_renders_with_nonzero_exit(self):
        from nanobot.utils.prompt_templates import render_template

        result = render_template(
            "agent/bg_task_announce.md",
            bg_id="bash_bg_fail99",
            exit_code=1,
            command="make test",
        )
        assert "bash_bg_fail99" in result
        assert "1" in result
        assert "make test" in result
