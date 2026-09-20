from __future__ import annotations

import os
import sys
from pathlib import Path


def _default_app_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "VoxStick"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VoxStick"
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "VoxStick"


def _app_dir() -> Path:
    configured = os.environ.get("VOX_STICK_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return _default_app_dir()


APP_SUPPORT_DIR = _app_dir()
STATE_PATH = APP_SUPPORT_DIR / "state.json"
QUOTA_PATH = APP_SUPPORT_DIR / "quota.json"
CLAUDE_QUOTA_PATH = APP_SUPPORT_DIR / "claude-quota.json"
RECORDING_PATH = APP_SUPPORT_DIR / "recording.json"
HUD_STATE_PATH = APP_SUPPORT_DIR / "hud-state.json"
RECORDINGS_DIR = APP_SUPPORT_DIR / "Recordings"


def ensure_app_support() -> Path:
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    return APP_SUPPORT_DIR
