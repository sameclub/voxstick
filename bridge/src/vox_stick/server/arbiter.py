"""Deciding which agent the device is looking at, and which one is shouting.

Two agents are watched at once but the screen highlights one. The arbiter picks
it, and separately picks whose alert deserves the beep — those are not always
the same agent, and an alert from the quiet one still matters.
"""

from __future__ import annotations

from vox_stick.protocol.state import (
    AlertType,
    CodexState,
    ProviderState,
    VoxStickState,
)
from vox_stick.providers.base import ProviderObservation

AUDIBLE_ALERTS = frozenset({AlertType.DONE, AlertType.APPROVAL, AlertType.ERROR})
KNOWN_PROVIDERS = frozenset({"codex", "claude"})


def carries_alert(observation: ProviderObservation) -> bool:
    """An alert counts only with an id, so the device can beep exactly once."""
    try:
        alert = AlertType(observation.alert_type)
    except ValueError:
        return False
    return alert in AUDIBLE_ALERTS and bool(observation.alert_event_id)


def alert_source(
    highlighted: ProviderObservation,
    *others: ProviderObservation,
) -> ProviderObservation:
    """The highlighted agent wins; otherwise any other agent with an alert."""
    if carries_alert(highlighted):
        return highlighted
    for candidate in others:
        if candidate is not highlighted and carries_alert(candidate):
            return candidate
    return highlighted


def as_codex_state(observation: ProviderObservation) -> CodexState:
    return CodexState(
        status=observation.status,
        project=observation.project,
        quota_5h_remaining=observation.quota_5h_remaining,
        quota_7d_remaining=observation.quota_7d_remaining,
        quota_updated_at=observation.quota_updated_at,
        quota_stale=observation.quota_stale,
    )


def as_provider_state(observation: ProviderObservation) -> ProviderState:
    return ProviderState(
        id=observation.provider_id,
        display_name=observation.display_name,
        implemented=True,
        status=observation.status,
        project=observation.project,
        quota_5h_remaining=observation.quota_5h_remaining,
        quota_7d_remaining=observation.quota_7d_remaining,
        quota_updated_at=observation.quota_updated_at,
        quota_stale=observation.quota_stale,
    )


def overlay_manual_status(observation: ProviderObservation, state: VoxStickState) -> None:
    """Let a status pushed over HTTP outrank what the session files say.

    A device button press should be visible immediately, even though the next
    poll of the session files would contradict it.
    """
    observation.status = state.codex.status
    observation.alert_type = state.alert.type.value
    observation.alert_message = state.alert.message
    observation.alert_event_id = state.alert.event_id


class ProviderArbiter:
    """Picks the highlighted agent, remembering the last choice.

    Remembering matters: when both agents go idle the display should stay where
    it was rather than snapping back to a default.
    """

    def __init__(self, initial: str = "codex") -> None:
        self._last = initial if initial in KNOWN_PROVIDERS else "codex"

    @property
    def last(self) -> str:
        return self._last

    def choose(
        self,
        preference: str,
        codex: ProviderObservation,
        claude: ProviderObservation,
    ) -> str:
        chosen = self._decide(preference, codex, claude)
        self._last = chosen
        return chosen

    def _decide(
        self,
        preference: str,
        codex: ProviderObservation,
        claude: ProviderObservation,
    ) -> str:
        if preference in KNOWN_PROVIDERS:
            return preference

        live = {"codex": codex.online, "claude": claude.online}
        if live["codex"] != live["claude"]:
            return "codex" if live["codex"] else "claude"
        if not live["codex"]:
            return self._last

        # Both are running: whichever wrote to its session file most recently
        # is the one being typed into.
        codex_at = codex.latest_event_timestamp
        claude_at = claude.latest_event_timestamp
        if codex_at is not None and claude_at is not None:
            return "claude" if claude_at > codex_at else "codex"
        if claude_at is not None:
            return "claude"
        if codex_at is not None:
            return "codex"
        return self._last
