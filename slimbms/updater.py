"""In-app updater.

Checks the GitHub Releases API for a newer version and, when running as the
packaged Windows exe, downloads the new ``SlimBMS.exe`` and swaps it in via a
small helper batch that waits for this process to exit, then relaunches.

When running from source (not frozen), updates are reported but not applied.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from typing import Optional, Tuple

from . import __version__

REPO = "Liranple/SlimBMS"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
_UA = {"User-Agent": "SlimBMS-updater", "Accept": "application/vnd.github+json"}


def is_frozen() -> bool:
    """True when running as a PyInstaller-built exe."""
    return getattr(sys, "frozen", False)


def parse_version(tag: str) -> Tuple[int, ...]:
    """'v0.4.0' / '0.4.0' -> (0, 4, 0); non-numeric parts collapse to 0."""
    tag = tag.strip().lstrip("vV")
    parts = []
    for chunk in tag.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) if parts else (0,)


def is_newer(tag: str, current: str = __version__) -> bool:
    return parse_version(tag) > parse_version(current)


class ReleaseInfo:
    def __init__(self, tag: str, url: Optional[str], notes: str):
        self.tag = tag
        self.exe_url = url
        self.notes = notes


def check_latest(timeout: float = 8.0) -> Optional[ReleaseInfo]:
    """Query the latest release. Returns None on any network/parse error."""
    try:
        req = urllib.request.Request(API_LATEST, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception:  # noqa: BLE001 — offline, rate-limited, etc.
        return None
    tag = data.get("tag_name", "")
    exe_url = None
    for asset in data.get("assets", []):
        if str(asset.get("name", "")).lower().endswith(".exe"):
            exe_url = asset.get("browser_download_url")
            break
    return ReleaseInfo(tag, exe_url, data.get("body", ""))


def download(url: str, dest: str, timeout: float = 60.0) -> None:
    """Stream a URL to a file."""
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            fh.write(chunk)


def download_new_exe(exe_url: str) -> str:
    """Download the new exe next to the current one; return its path.

    Only valid when :func:`is_frozen`. Safe to call from a worker thread.
    """
    if not is_frozen():
        raise RuntimeError("소스 실행 중에는 자동 업데이트를 적용할 수 없습니다.")
    folder = os.path.dirname(os.path.abspath(sys.executable))
    new_exe = os.path.join(folder, "SlimBMS_update.exe")
    download(exe_url, new_exe)
    return new_exe


def swap_and_restart(new_exe: str) -> None:
    """Hand off to a batch that replaces the running exe once this process
    exits, then relaunches. Does not return (it exits the process).

    Must be called on the main thread after :func:`download_new_exe`.
    """
    current = os.path.abspath(sys.executable)
    folder = os.path.dirname(current)
    bat = os.path.join(folder, "_slimbms_update.bat")
    # Wait for this exe to be released, replace it, relaunch, then self-delete.
    # Wait for the old exe to be released, replace it, let the filesystem settle
    # (avoids a race where the fresh onefile exe is relaunched before its temp
    # extraction is ready -> "Failed to load Python DLL"), then relaunch.
    script = (
        "@echo off\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        ":retry\r\n"
        f'del "{current}" >nul 2>&1\r\n'
        f'if exist "{current}" (timeout /t 1 /nobreak >nul & goto retry)\r\n'
        f'move /y "{new_exe}" "{current}" >nul\r\n'
        "timeout /t 3 /nobreak >nul\r\n"
        f'start "" "{current}"\r\n'
        'del "%~f0"\r\n'
    )
    with open(bat, "w", encoding="ascii") as fh:
        fh.write(script)

    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags |= subprocess.CREATE_NO_WINDOW
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags |= subprocess.DETACHED_PROCESS
    subprocess.Popen(["cmd", "/c", bat], creationflags=creationflags, close_fds=True)
    sys.exit(0)
