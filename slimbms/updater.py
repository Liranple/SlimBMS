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
import tempfile
import urllib.request
import zipfile
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
        self.download_url = url   # the release .zip
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
    zip_url = None
    for asset in data.get("assets", []):
        if str(asset.get("name", "")).lower().endswith(".zip"):
            zip_url = asset.get("browser_download_url")
            break
    return ReleaseInfo(tag, zip_url, data.get("body", ""))


def download(url: str, dest: str, timeout: float = 60.0) -> None:
    """Stream a URL to a file."""
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            fh.write(chunk)


def download_update(zip_url: str):
    """Download and extract the update zip. Returns ``(new_app_dir, tmp_root)``
    where ``new_app_dir`` holds the fresh SlimBMS.exe. Safe on a worker thread.

    Only valid when :func:`is_frozen`.
    """
    if not is_frozen():
        raise RuntimeError("\uc18c\uc2a4 \uc2e4\ud589 \uc911\uc5d0\ub294 \uc790\ub3d9 \uc5c5\ub370\uc774\ud2b8\ub97c \uc801\uc6a9\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4.")
    tmp_root = tempfile.mkdtemp(prefix="slimbms_update_")
    zip_path = os.path.join(tmp_root, "update.zip")
    download(zip_url, zip_path)
    extract_dir = os.path.join(tmp_root, "app")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)
    new_app_dir = _find_app_dir(extract_dir)
    if new_app_dir is None:
        raise RuntimeError("\uc5c5\ub370\uc774\ud2b8 \ud30c\uc77c\uc5d0\uc11c SlimBMS.exe\ub97c \ucc3e\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4.")
    return new_app_dir, tmp_root


def _find_app_dir(root):
    for base, _dirs, files in os.walk(root):
        if "SlimBMS.exe" in files:
            return base
    return None


def swap_and_restart(new_app_dir: str, tmp_root: str) -> None:
    """Hand off to a batch that, once this process exits, copies the new app
    folder over the current one and relaunches. Does not return (it exits).

    Must be called on the main thread after :func:`download_update`.
    """
    current = os.path.abspath(sys.executable)
    app_dir = os.path.dirname(current)
    bat = os.path.join(tmp_root, "_slimbms_update.bat")
    log = os.path.join(tmp_root, "update_log.txt")
    # Delay with `ping`, NOT `timeout`: `timeout` needs a valid console input
    # handle, which a windowed (no-console) app's spawned batch may not have, and
    # it then hangs. `ping -n 4 127.0.0.1` is a stdin-free ~3s sleep that works
    # in any context. robocopy output goes to a log; on failure the window stays
    # open so the error is visible instead of silently stalling.
    script = (
        "@echo off\r\n"
        "title SlimBMS Update\r\n"
        "echo Updating SlimBMS, please wait...\r\n"
        "ping -n 4 127.0.0.1 >nul\r\n"
        f'robocopy "{new_app_dir}" "{app_dir}" /E /R:8 /W:2 >"{log}" 2>&1\r\n'
        "if errorlevel 8 goto failed\r\n"
        f'start "" "{current}"\r\n'
        "exit\r\n"
        ":failed\r\n"
        "echo.\r\n"
        f'echo Update failed. Log file: {log}\r\n'
        "echo Please reopen SlimBMS and try again, or reinstall from the release page.\r\n"
        "pause\r\n"
    )
    with open(bat, "w", encoding="ascii") as fh:
        fh.write(script)

    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(["cmd", "/c", bat], creationflags=creationflags, close_fds=True)
    # sys.exit() does not reliably terminate the app from inside a Qt slot, which
    # would leave the exe locked; force-kill so the batch can replace the files.
    sys.stdout.flush()
    os._exit(0)
