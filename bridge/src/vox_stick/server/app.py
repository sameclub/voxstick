"""Bridge session and process entry point.

The session owns the mutable picture of both agents; quota caching lives in
`quotas`, agent selection in `arbiter`, and the HTTP surface in `routing`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from typing import Any

from vox_stick.audio.recorder import RecordingController
from vox_stick.config.dotenv import load_env
from vox_stick.config.paths import RECORDING_PATH, STATE_PATH, ensure_app_support
from vox_stick.desktop.hud import hide_hud
from vox_stick.protocol.state import (
    AgentStatus,
    AlertState,
    AlertType,
    VoxStickState,
    default_state,
    event_id,
    now_time_text,
    state_from_dict,
)
from vox_stick.providers.base import ProviderObservation
from vox_stick.providers.claude import observe_claude
from vox_stick.providers.codex import observe_codex
from vox_stick.server import arbiter
from vox_stick.server.arbiter import ProviderArbiter
from vox_stick.server.quotas import QuotaLedger
from vox_stick.server.routing import BridgeServer
from vox_stick.server.settings import BridgeSettings, refuse_unsafe_bind

# How long a status pushed from the device outranks the session files.
MANUAL_STATUS_SECONDS = 60

CLEARED_ALERT = AlertState(event_id="", type=AlertType.NONE, message="")

MANUAL_ALERT_TEXT = {
    AgentStatus.DONE: ("done", AlertType.DONE, "Codex task completed"),
    AgentStatus.APPROVAL: ("approval", AlertType.APPROVAL, "Codex is waiting for approval"),
    AgentStatus.ERROR: ("error", AlertType.ERROR, "Codex needs attention"),
}


class BridgeSession:
    """The live picture of both agents, shared across request threads."""

    def __init__(self, settings: BridgeSettings | None = None) -> None:
        ensure_app_support()
        self.settings = settings or BridgeSettings.from_env()
        self._lock = threading.RLock()
        self._manual_until = 0.0
        self._state = self._read_state()
        self._arbiter = ProviderArbiter(self._state.active_provider or "codex")
        self._quotas = QuotaLedger(self.settings.claude_poll_seconds)
        self._quotas.seed_claude_from(self._state)
        self._quotas.restore_codex_into(self._state)
        self.recording = RecordingController(RECORDING_PATH)
        hide_hud()

    # -- reads ------------------------------------------------------------

    def get_state(self) -> VoxStickState:
        with self._lock:
            self._resurvey()
            self._state.time = now_time_text()
            self._write_state()
            return self._state

    # -- writes -----------------------------------------------------------

    def update_from_event(self, event: dict[str, Any]) -> VoxStickState:
        with self._lock:
            pushed = event.get("codex_status") or event.get("status")
            name = str(event.get("event") or "")
            if pushed:
                self._force_codex_status(str(pushed), str(event.get("message") or ""))
                self._manual_until = time.monotonic() + MANUAL_STATUS_SECONDS
            elif name == "button_double":
                self.resurvey_with_quota()
            elif name == "button_short":
                self._state.alert = CLEARED_ALERT
            self._write_state()
            return self._state

    def refresh_quota(self) -> VoxStickState:
        with self._lock:
            self.resurvey_with_quota()
            self._write_state()
            return self._state

    def resurvey_with_quota(self) -> None:
        """A refresh press updates both agents, not just the highlighted one."""
        self._quotas.poll_claude(force=True)
        claude = self._quotas.record_claude(observe_claude(self.settings.project_root))
        self._state.claude = arbiter.as_provider_state(claude)

        codex = observe_codex(self.settings.project_root)
        self._quotas.record_codex(codex, self._state, persist_when_missing=True)
        self._state.codex = arbiter.as_codex_state(codex)

        highlighted = claude if self._state.active_provider == "claude" else codex
        self._state.provider = arbiter.as_provider_state(highlighted)

    # -- recording --------------------------------------------------------

    def start_recording(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self.recording.start(request)
        with self._lock:
            self._state.alert = CLEARED_ALERT
            self._write_state()
        return self._recording_reply(session)

    def stop_recording(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._recording_reply(self.recording.stop(request))

    def upload_recording_audio(
        self,
        pcm: bytes,
        *,
        session_id: str = "",
        sample_rate: int = 16000,
        channels: int = 1,
        bits_per_sample: int = 16,
    ) -> dict[str, Any]:
        session = self.recording.attach_pcm(
            pcm,
            session_id=session_id,
            sample_rate=sample_rate,
            channels=channels,
            bits_per_sample=bits_per_sample,
        )
        return self._recording_reply(session)

    def _recording_reply(self, session: Any) -> dict[str, Any]:
        return {"recording": session.to_jsonable(), "state": self.get_state().to_jsonable()}

    # -- internals --------------------------------------------------------

    def _resurvey(self) -> None:
        codex = observe_codex(self.settings.project_root)
        claude = observe_claude(self.settings.project_root)
        self._quotas.record_codex(codex, self._state)

        if time.monotonic() < self._manual_until:
            arbiter.overlay_manual_status(codex, self._state)

        active = self._arbiter.choose(self.settings.provider_preference, codex, claude)
        self._state.active_provider = active

        self._quotas.poll_claude(force=False)
        self._quotas.record_claude(claude)
        highlighted = claude if active == "claude" else codex

        self._state.codex = arbiter.as_codex_state(codex)
        self._state.claude = arbiter.as_provider_state(claude)
        self._state.provider = arbiter.as_provider_state(highlighted)
        self._adopt_alert(arbiter.alert_source(highlighted, codex, claude))

    def _adopt_alert(self, observation: ProviderObservation) -> None:
        if not arbiter.carries_alert(observation):
            self._state.alert = CLEARED_ALERT
            return
        self._state.alert = AlertState(
            event_id=observation.alert_event_id,
            type=AlertType(observation.alert_type),
            message=observation.alert_message,
        )

    def _force_codex_status(self, raw: str, message: str) -> None:
        try:
            status = AgentStatus(raw.upper())
        except ValueError:
            status = AgentStatus.UNKNOWN
        self._state.codex.status = status
        if self._state.active_provider == "codex":
            self._state.provider.status = status

        template = MANUAL_ALERT_TEXT.get(status)
        if template is None:
            self._state.alert = CLEARED_ALERT
            return
        kind, alert_type, fallback = template
        self._state.alert = AlertState(event_id(kind), alert_type, message or fallback)

    def _read_state(self) -> VoxStickState:
        try:
            return state_from_dict(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return default_state()

    def _write_state(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(self._state.to_jsonable(), indent=2) + "\n", encoding="utf-8"
        )


def create_server(host: str, port: int) -> BridgeServer:
    """Build the server without serving it, so the desktop tool can own the
    thread and shut it down again."""
    settings = BridgeSettings.from_env()
    refuse_unsafe_bind(host, settings)
    server = BridgeServer((host, port), BridgeSession(settings), settings)
    if not settings.authenticates:
        print(
            "WARNING: VOX_STICK_BRIDGE_TOKEN is not set; POST endpoints are "
            "unauthenticated on loopback only.",
            flush=True,
        )
    return server


def run_server(host: str, port: int) -> None:
    server = create_server(host, port)
    print(f"VoxStick Bridge listening on http://{host}:{port}", flush=True)
    server.serve_forever()


def _spawn_hud() -> "subprocess.Popen[bytes] | None":
    """Run the HUD in its own process: Tk must own a process main thread."""
    try:
        return subprocess.Popen([sys.executable, "-m", "vox_stick.desktop.hud_window"])
    except OSError as exc:
        print(f"HUD failed to start: {exc}", flush=True)
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run VoxStick Bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--hud", action="store_true", help="also run the desktop HUD window")
    parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> None:
    env_file = load_env()
    if env_file is not None:
        print(f"Loaded configuration from {env_file}", flush=True)
    args = build_parser().parse_args(argv)
    hud = _spawn_hud() if args.hud else None
    try:
        run_server(args.host, args.port)
    finally:
        if hud is not None and hud.poll() is None:
            hud.terminate()
