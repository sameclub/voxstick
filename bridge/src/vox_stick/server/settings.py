"""Bridge configuration, read once into an immutable record.

Every knob is an environment variable so the desktop tool can set them before
importing the server. Reading them once keeps a long-running process from
changing behaviour halfway through a request.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

BRIDGE_NAME = "voxstick-bridge"

RECORDING_BYTES_DEFAULT = 1_800_000
RECORDING_BYTES_FLOOR = 256_000
RECORDING_BYTES_CEILING = 8_000_000

CLAUDE_POLL_DEFAULT_SECONDS = 300
CLAUDE_POLL_FLOOR_SECONDS = 30

PROVIDER_CHOICES = ("codex", "claude", "auto")

# Values shipped in .env.example. Treating them as unset stops a copied template
# from looking like a configured secret.
TEMPLATE_TOKENS = frozenset(
    {
        "change-this-shared-token",
        "paste-generated-token-here",
        "changeme",
        "change-me",
    }
)

# Directories a user is likely to be standing in when they launch the bridge;
# the project root is one level up from any of them.
NESTED_LAUNCH_DIRS = frozenset({"bridge", "firmware", "app", "scripts"})


def _env(name: str) -> str:
    return os.environ.get(f"VOX_STICK_{name}", "").strip()


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass(frozen=True)
class BridgeSettings:
    """A snapshot of the environment at construction time."""

    token: str
    provider_preference: str
    project_root: Path
    recording_byte_limit: int
    claude_poll_seconds: int

    @classmethod
    def from_env(cls) -> "BridgeSettings":
        return cls(
            token=_read_token(),
            provider_preference=_read_provider_preference(),
            project_root=_read_project_root(),
            recording_byte_limit=_read_recording_limit(),
            claude_poll_seconds=_read_claude_poll_seconds(),
        )

    @property
    def authenticates(self) -> bool:
        return bool(self.token)

    def rejects(self, supplied: str) -> bool:
        """Constant-time check; an unset token accepts everything."""
        import hmac

        if not self.token:
            return False
        return not hmac.compare_digest(supplied, self.token)


def _read_token() -> str:
    token = _env("BRIDGE_TOKEN")
    return "" if token.lower() in TEMPLATE_TOKENS else token


def _read_provider_preference() -> str:
    value = _env("PROVIDER").lower() or "auto"
    return value if value in PROVIDER_CHOICES else "auto"


def _read_project_root() -> Path:
    configured = _env("PROJECT_ROOT")
    root = Path(configured).expanduser() if configured else Path.cwd()
    if root.name in NESTED_LAUNCH_DIRS and (root.parent / "README.md").exists():
        root = root.parent
    return root.resolve()


def _read_recording_limit() -> int:
    raw = _env("MAX_RECORDING_AUDIO_BYTES")
    if not raw:
        return RECORDING_BYTES_DEFAULT
    try:
        return _clamp(int(raw), RECORDING_BYTES_FLOOR, RECORDING_BYTES_CEILING)
    except ValueError:
        return RECORDING_BYTES_DEFAULT


def _read_claude_poll_seconds() -> int:
    raw = _env("CLAUDE_USAGE_INTERVAL_SECONDS")
    try:
        value = int(raw)
    except ValueError:
        value = CLAUDE_POLL_DEFAULT_SECONDS
    if value <= 0:
        value = CLAUDE_POLL_DEFAULT_SECONDS
    return max(CLAUDE_POLL_FLOOR_SECONDS, value)


def reaches_beyond_loopback(host: str) -> bool:
    """True when binding `host` exposes the bridge past this machine.

    An unparseable host is treated as exposed: a hostname can resolve anywhere,
    and refusing to serve is the safer side of that guess.
    """
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return False
    if not normalized:
        return True
    try:
        return not ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return True


def refuse_unsafe_bind(host: str, settings: BridgeSettings) -> None:
    if reaches_beyond_loopback(host) and not settings.authenticates:
        raise SystemExit(
            "Refusing to bind VoxStick Bridge outside loopback without "
            "VOX_STICK_BRIDGE_TOKEN. Set a strong shared token or use --host 127.0.0.1."
        )
