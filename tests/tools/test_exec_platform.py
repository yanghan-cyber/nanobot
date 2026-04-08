"""Tests for cross-platform shell execution.

Verifies that BashTool selects the correct shell, environment, path-append
strategy, and sandbox behaviour per platform — without actually running
platform-specific binaries (all subprocess calls are mocked).
"""

from unittest.mock import AsyncMock, patch

import pytest

from nanobot.agent.tools.shell import BashTool


# ---------------------------------------------------------------------------
# _build_env
# ---------------------------------------------------------------------------

class TestBuildEnvUnix:

    def test_expected_keys(self):
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", False):
            env = BashTool()._build_env()
        assert "HOME" in env
        assert "LANG" in env

    def test_home_from_environ(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/dev")
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", False):
            env = BashTool()._build_env()
        assert env["HOME"] == "/Users/dev"

    def test_secrets_excluded(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("NANOBOT_TOKEN", "tok-secret")
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", False):
            env = BashTool()._build_env()
        assert "OPENAI_API_KEY" not in env
        assert "NANOBOT_TOKEN" not in env
        for v in env.values():
            assert "secret" not in v.lower()


class TestBuildEnvWindows:

    _EXPECTED_KEYS = {
        "SYSTEMROOT", "COMSPEC", "USERPROFILE", "HOMEDRIVE",
        "HOMEPATH", "TEMP", "TMP", "PATHEXT", "PATH",
    }

    def test_expected_keys(self):
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", True):
            env = BashTool()._build_env()
        assert set(env) == self._EXPECTED_KEYS

    def test_secrets_excluded(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
        monkeypatch.setenv("NANOBOT_TOKEN", "tok-secret")
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", True):
            env = BashTool()._build_env()
        assert "OPENAI_API_KEY" not in env
        assert "NANOBOT_TOKEN" not in env
        for v in env.values():
            assert "secret" not in v.lower()

    def test_path_has_sensible_default(self):
        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", True),
            patch.dict("os.environ", {}, clear=True),
        ):
            env = BashTool()._build_env()
        assert "system32" in env["PATH"].lower()

    def test_systemroot_forwarded(self, monkeypatch):
        monkeypatch.setenv("SYSTEMROOT", r"D:\Windows")
        with patch("nanobot.agent.tools.shell._IS_WINDOWS", True):
            env = BashTool()._build_env()
        assert env["SYSTEMROOT"] == r"D:\Windows"


# ---------------------------------------------------------------------------
# path_append
# ---------------------------------------------------------------------------

class TestPathAppendPlatform:

    @pytest.mark.asyncio
    async def test_path_append_added_to_env(self):
        """path_append is appended to PATH in the env dict via os.pathsep."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0

        captured_env = {}

        async def capture_spawn(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return mock_proc

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", False),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, side_effect=capture_spawn),
            patch.object(BashTool, "_guard_command", return_value=None),
            patch.object(BashTool, "_resolve_shell", return_value="/bin/bash"),
        ):
            tool = BashTool(path_append="/opt/bin")
            await tool.execute(command="ls")

        assert "/opt/bin" in captured_env.get("PATH", "")


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------

class TestSandboxPlatform:

    @pytest.mark.asyncio
    async def test_bwrap_skipped_on_windows(self):
        """bwrap must be silently skipped on Windows, not crash."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", True),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc),
            patch.object(BashTool, "_guard_command", return_value=None),
            patch.object(BashTool, "_resolve_shell", return_value="cmd.exe"),
        ):
            tool = BashTool(sandbox="bwrap")
            result = await tool.execute(command="dir")

        assert "ok" in result

    @pytest.mark.asyncio
    async def test_bwrap_applied_on_unix(self):
        """On Unix, sandbox wrapping should still happen normally."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"sandboxed", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", False),
            patch("nanobot.agent.tools.shell.wrap_command", return_value="bwrap -- sh -c ls") as mock_wrap,
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc),
            patch.object(BashTool, "_guard_command", return_value=None),
            patch.object(BashTool, "_resolve_shell", return_value="/bin/bash"),
        ):
            tool = BashTool(sandbox="bwrap", working_dir="/workspace")
            await tool.execute(command="ls")

        mock_wrap.assert_called_once()


# ---------------------------------------------------------------------------
# end-to-end (mocked subprocess, full execute path)
# ---------------------------------------------------------------------------

class TestExecuteEndToEnd:

    @pytest.mark.asyncio
    async def test_windows_full_path(self):
        """Full execute() flow on Windows: env, spawn, output formatting."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello world\r\n", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", True),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc),
            patch.object(BashTool, "_guard_command", return_value=None),
            patch.object(BashTool, "_resolve_shell", return_value="bash.exe"),
        ):
            tool = BashTool()
            result = await tool.execute(command="echo hello world")

        assert "hello world" in result
        assert "Exit code: 0" in result

    @pytest.mark.asyncio
    async def test_unix_full_path(self):
        """Full execute() flow on Unix: env, spawn, output formatting."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello world\n", b"")
        mock_proc.returncode = 0

        with (
            patch("nanobot.agent.tools.shell._IS_WINDOWS", False),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc),
            patch.object(BashTool, "_guard_command", return_value=None),
            patch.object(BashTool, "_resolve_shell", return_value="/bin/bash"),
        ):
            tool = BashTool()
            result = await tool.execute(command="echo hello world")

        assert "hello world" in result
        assert "Exit code: 0" in result
