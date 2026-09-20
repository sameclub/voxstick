"""Cross-platform "is this agent running?" probe.

A POSIX bridge would shell out to `ps -axo command=`, which does not exist on
Windows. Windows process command lines come from a CIM query instead, and that
query is slow enough (hundreds of milliseconds) that `/state` polling every two
seconds must not pay for it on every request, so results are cached.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

CACHE_SECONDS = 15.0

_lock = threading.Lock()
_cache: tuple[float, list[str]] = (0.0, [])


def command_lines() -> list[str]:
    """Lowercased command lines of the current user's processes."""
    global _cache
    with _lock:
        cached_at, lines = _cache
        if lines and time.monotonic() - cached_at < CACHE_SECONDS:
            return lines
        lines = _windows_command_lines() if sys.platform == "win32" else _posix_command_lines()
        if lines:
            _cache = (time.monotonic(), lines)
        return lines


def _posix_command_lines() -> list[str]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]


def _windows_command_lines() -> list[str]:
    # A command line may itself contain newlines (an inline script, say). Collapse
    # them so every output line is exactly one process, or the first-token check
    # in the callers matches fragments of somebody else's script.
    script = (
        "Get-CimInstance Win32_Process | ForEach-Object { "
        "$line = if ($_.CommandLine) { $_.CommandLine } else { $_.Name }; "
        "$line -replace '[\\r\\n]+', ' ' }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_no_window_flag(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]


def _no_window_flag() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
