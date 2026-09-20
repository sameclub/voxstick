"""Minimal .env loader.

A shell would source this file directly (`set -a; . .env`). PowerShell has
no equivalent, so the bridge loads it itself and stays launchable the same way
on every platform. Values already present in the environment win.
"""

from __future__ import annotations

import os
from pathlib import Path

from vox_stick.config.paths import APP_SUPPORT_DIR


def candidate_paths() -> list[Path]:
    configured = os.environ.get("VOX_STICK_ENV_FILE", "").strip()
    if configured:
        return [Path(configured).expanduser()]
    bridge_dir = Path(__file__).resolve().parents[2].parent
    return [bridge_dir / ".env", APP_SUPPORT_DIR / ".env"]


def load_env(paths: list[Path] | None = None) -> Path | None:
    for path in paths if paths is not None else candidate_paths():
        if not path.is_file():
            continue
        for key, value in parse_env(path.read_text(encoding="utf-8")).items():
            os.environ.setdefault(key, value)
        return path
    return None


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if value:
            values[key] = value
    return values
