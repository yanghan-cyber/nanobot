"""Ripgrep binary management — find, download, ensure availability."""

from __future__ import annotations

import io
import os
import platform
import shutil
import sys
import tarfile
import threading
import urllib.error
import zipfile
from pathlib import Path

from loguru import logger

from nanobot.config.paths import get_data_dir

_RG_VERSION = "14.1.0"
_GITHUB_RELEASES = "https://github.com/BurntSushi/ripgrep/releases/download"

_cached_rg_path: str | None = None
_cache_lock = threading.Lock()
_MAX_DOWNLOAD_SIZE = 50_000_000  # 50 MB


def _rg_binary_name() -> str:
    """Return the rg binary filename for the current platform."""
    return "rg.exe" if sys.platform == "win32" else "rg"


def _nanobot_bin_dir() -> Path:
    """Return the ~/.nanobot/bin directory."""
    return get_data_dir() / "bin"


def _platform_tag() -> str:
    """Return the ripgrep release platform tag for the current OS/arch."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        arch = machine

    if sys.platform == "win32":
        return f"{arch}-pc-windows-msvc"
    elif sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    else:
        return f"{arch}-unknown-linux-musl"


def _download_url(version: str = _RG_VERSION) -> str:
    """Build the GitHub release download URL for the current platform."""
    tag = _platform_tag()
    ext = "zip" if sys.platform == "win32" else "tar.gz"
    filename = f"ripgrep-{version}-{tag}.{ext}"
    return f"{_GITHUB_RELEASES}/{version}/{filename}"


def _extract_to(buf: bytes, dest: Path) -> None:
    """Extract rg binary from a downloaded archive to dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    name = _rg_binary_name()

    if sys.platform == "win32":
        with zipfile.ZipFile(io.BytesIO(buf)) as zf:
            for member in zf.namelist():
                if Path(member).name == name:
                    with zf.open(member) as src, open(dest, "wb") as dst:
                        dst.write(src.read())
                    return
    else:
        with tarfile.open(fileobj=io.BytesIO(buf), mode="r:gz") as tf:
            for member in tf.getmembers():
                if Path(member.name).name == name and member.isfile():
                    src = tf.extractfile(member)
                    if src:
                        with open(dest, "wb") as dst:
                            dst.write(src.read())
                        os.chmod(str(dest), 0o755)
                        return

    raise FileNotFoundError(f"'{name}' not found in downloaded archive")


def find_rg() -> str | None:
    """Locate rg: system PATH first, then ~/.nanobot/bin/."""
    # System PATH
    found = shutil.which("rg")
    if found:
        return found

    # ~/.nanobot/bin/
    local = _nanobot_bin_dir() / _rg_binary_name()
    if local.exists():
        return str(local)

    return None


def _download_with_limit(resp, max_size: int) -> bytes:
    """Read response body in chunks with a size limit."""
    chunks = []
    total = 0
    while True:
        chunk = resp.read(1 << 20)  # 1 MB
        if not chunk:
            break
        total += len(chunk)
        if total > max_size:
            raise OSError(f"Download exceeded {max_size // 1_000_000}MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def ensure_rg() -> str:
    """Find rg or auto-download it. Returns path to rg binary. Result is cached."""
    global _cached_rg_path
    if _cached_rg_path:
        return _cached_rg_path

    with _cache_lock:
        if _cached_rg_path:
            return _cached_rg_path

        found = find_rg()
        if found:
            _cached_rg_path = found
            return found

        # Auto-download
        url = _download_url()
        dest = _nanobot_bin_dir() / _rg_binary_name()

        logger.info("ripgrep not found, downloading from {}", url)
        try:
            import urllib.request

            with urllib.request.urlopen(url) as resp:
                buf = _download_with_limit(resp, _MAX_DOWNLOAD_SIZE)
            _extract_to(buf, dest)
            logger.info("ripgrep extracted to {}", dest)
        except (urllib.error.URLError, OSError, tarfile.TarError, zipfile.BadZipFile) as e:
            raise FileNotFoundError(
                f"ripgrep (rg) not found and auto-download failed: {e}. "
                f"Install manually: https://github.com/BurntSushi/ripgrep#installation"
            ) from e

        _cached_rg_path = str(dest)
        return _cached_rg_path
