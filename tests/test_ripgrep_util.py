"""Tests for nanobot.utils.ripgrep -- rg binary management."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nanobot.utils.ripgrep import find_rg, ensure_rg, _download_url, _rg_binary_name


class TestFindRg:
    """Tests for find_rg()."""

    def test_finds_system_rg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/rg")
        assert find_rg() == "/usr/bin/rg"

    def test_finds_nanobot_bin_rg(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("nanobot.utils.ripgrep.get_data_dir", lambda: tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        rg_name = _rg_binary_name()
        (bin_dir / rg_name).write_text("fake", encoding="utf-8")
        assert find_rg() == str(bin_dir / rg_name)

    def test_returns_none_when_not_found(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("nanobot.utils.ripgrep.get_data_dir", lambda: tmp_path)
        assert find_rg() is None


class TestDownloadUrl:
    """Tests for _download_url platform detection."""

    def test_windows_x64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr("platform.machine", lambda: "AMD64")
        url = _download_url("14.1.0")
        assert "x86_64-pc-windows-msvc.zip" in url

    def test_linux_x64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr("platform.machine", lambda: "x86_64")
        url = _download_url("14.1.0")
        assert "x86_64-unknown-linux-musl.tar.gz" in url

    def test_linux_arm64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr("platform.machine", lambda: "aarch64")
        url = _download_url("14.1.0")
        assert "aarch64-unknown-linux-musl.tar.gz" in url

    def test_macos_arm64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("platform.machine", lambda: "arm64")
        url = _download_url("14.1.0")
        assert "aarch64-apple-darwin.tar.gz" in url


class TestRgBinaryName:
    def test_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert _rg_binary_name() == "rg.exe"

    def test_non_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert _rg_binary_name() == "rg"


class TestEnsureRg:
    def _reset_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nanobot.utils.ripgrep._cached_rg_path", None)

    def test_returns_system_rg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._reset_cache(monkeypatch)
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/rg")
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/rg")
        assert ensure_rg() == "/usr/bin/rg"

    def test_downloads_when_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._reset_cache(monkeypatch)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("nanobot.utils.ripgrep.get_data_dir", lambda: tmp_path)
        fake_rg = tmp_path / "bin" / _rg_binary_name()

        def fake_extract(buf: bytes, dest: Path) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("fake_rg", encoding="utf-8")

        monkeypatch.setattr("nanobot.utils.ripgrep._extract_to", fake_extract)
        monkeypatch.setattr("nanobot.utils.ripgrep._download_with_limit", lambda resp, max_size: b"fake-archive-data")

        class FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        monkeypatch.setattr("urllib.request.urlopen", lambda url: FakeResp())

        result = ensure_rg()
        assert result == str(fake_rg)
        assert fake_rg.exists()

    def test_raises_when_download_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._reset_cache(monkeypatch)
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("nanobot.utils.ripgrep.get_data_dir", lambda: tmp_path)

        def raise_error(url):
            raise OSError("network error")

        monkeypatch.setattr("urllib.request.urlopen", raise_error)

        with pytest.raises(FileNotFoundError, match="Install manually"):
            ensure_rg()
