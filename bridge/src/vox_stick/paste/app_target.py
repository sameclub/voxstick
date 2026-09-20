"""Resolve voice destinations without falling back to an unrelated foreground app."""
from __future__ import annotations

import ctypes
import json
from pathlib import Path
import subprocess
import sys


APP_NAMES = {"codex": "ChatGPT", "claude": "Claude"}


def focus_composer(provider: str) -> int:
    if provider not in APP_NAMES:
        raise RuntimeError("Unknown voice destination")
    if sys.platform != "win32":
        raise RuntimeError("App-directed voice input currently requires Windows")
    script = Path(__file__).with_name("focus_composer.ps1")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script),
         "-AppName", APP_NAMES[provider]],
        capture_output=True, encoding="utf-8", errors="replace", timeout=22,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Could not focus {APP_NAMES[provider]}")
    try:
        hwnd = int(json.loads(result.stdout)["hwnd"])
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("App focus did not return a verified window") from exc
    if not is_foreground(hwnd):
        raise RuntimeError("Destination app lost focus; transcript was not pasted")
    return hwnd


def is_foreground(hwnd: int) -> bool:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return user32.GetForegroundWindow() == hwnd
