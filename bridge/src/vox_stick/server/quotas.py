"""Quota bookkeeping for both agents.

Neither agent exposes a real quota API, so the numbers are scavenged: Codex
from its own session files, Claude from an undocumented endpoint behind the
CLI's OAuth credentials. Both can go silent at any time, so the ledger keeps
the last good figures and marks them stale rather than blanking the display.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from vox_stick.claude.usage import fetch_usage as fetch_claude_usage
from vox_stick.claude.usage import to_quota_snapshot as claude_usage_to_quota
from vox_stick.codex.quota import QuotaSnapshot, load_quota, save_quota
from vox_stick.config.paths import CLAUDE_QUOTA_PATH, QUOTA_PATH
from vox_stick.protocol.state import VoxStickState
from vox_stick.providers.base import ProviderObservation

# A Claude figure this old is shown, but flagged: the endpoint has usually
# stopped answering rather than the usage genuinely standing still.
CLAUDE_TRUST_SECONDS = 30 * 60


def populated(snapshot: QuotaSnapshot) -> bool:
    return snapshot.quota_5h_remaining is not None or snapshot.quota_7d_remaining is not None


def as_stale(snapshot: QuotaSnapshot) -> QuotaSnapshot:
    return QuotaSnapshot(
        quota_5h_remaining=snapshot.quota_5h_remaining,
        quota_7d_remaining=snapshot.quota_7d_remaining,
        quota_updated_at=snapshot.quota_updated_at,
        quota_stale=True,
    )


def salvage_claude(state: VoxStickState) -> QuotaSnapshot:
    """Recover Claude figures from a persisted state file.

    Only useful when the last run left Claude active; anything recovered this
    way is stale by definition.
    """
    provider = state.provider
    if provider.id != "claude":
        return QuotaSnapshot()
    recovered = QuotaSnapshot(
        quota_5h_remaining=provider.quota_5h_remaining,
        quota_7d_remaining=provider.quota_7d_remaining,
        quota_updated_at=provider.quota_updated_at,
        quota_stale=True,
    )
    return recovered if populated(recovered) else QuotaSnapshot()


def _overwrite(observation: ProviderObservation, snapshot: QuotaSnapshot) -> ProviderObservation:
    observation.quota_5h_remaining = snapshot.quota_5h_remaining
    observation.quota_7d_remaining = snapshot.quota_7d_remaining
    observation.quota_updated_at = snapshot.quota_updated_at
    observation.quota_stale = snapshot.quota_stale
    return observation


@dataclass
class _ClaudePoll:
    """When the Claude endpoint was last called, and last answered."""

    attempted: float = 0.0
    succeeded: float = 0.0


class QuotaLedger:
    """Holds both agents' quota caches and decides what the device sees.

    Codex figures live in the observation itself and are mirrored to disk.
    Claude figures are polled on a timer and cached here, because the endpoint
    is slow and rate-sensitive.
    """

    def __init__(self, poll_seconds: int) -> None:
        self._poll_seconds = poll_seconds
        self._claude = load_quota(CLAUDE_QUOTA_PATH)
        self._claude_poll = _ClaudePoll()

    def seed_claude_from(self, state: VoxStickState) -> None:
        """Fall back to the persisted state when no cache file survived."""
        if not populated(self._claude):
            self._claude = salvage_claude(state)

    def restore_codex_into(self, state: VoxStickState) -> None:
        stored = load_quota(QUOTA_PATH)
        state.codex.quota_5h_remaining = stored.quota_5h_remaining
        state.codex.quota_7d_remaining = stored.quota_7d_remaining
        state.codex.quota_updated_at = stored.quota_updated_at
        state.codex.quota_stale = stored.quota_stale

    def record_codex(
        self,
        observation: ProviderObservation,
        state: VoxStickState,
        *,
        persist_when_missing: bool = False,
    ) -> ProviderObservation:
        """Merge whatever Codex reported with what we already had.

        A session file that carries fresh numbers wins and is written through.
        Otherwise the previous figures are reused and marked stale, so a quiet
        Codex shows its last known usage instead of dashes.
        """
        reported = QuotaSnapshot(
            quota_5h_remaining=observation.quota_5h_remaining,
            quota_7d_remaining=observation.quota_7d_remaining,
            quota_updated_at=observation.quota_updated_at,
            quota_stale=observation.quota_stale,
        )
        if populated(reported):
            save_quota(QUOTA_PATH, reported)
            return _overwrite(observation, reported)

        remembered = QuotaSnapshot(
            quota_5h_remaining=state.codex.quota_5h_remaining,
            quota_7d_remaining=state.codex.quota_7d_remaining,
            quota_updated_at=state.codex.quota_updated_at,
            quota_stale=state.codex.quota_stale,
        )
        resolved = as_stale(remembered) if populated(remembered) else remembered
        if persist_when_missing:
            save_quota(QUOTA_PATH, resolved)
        return _overwrite(observation, resolved)

    def poll_claude(self, *, force: bool) -> None:
        """Call the Claude usage endpoint, at most once per interval."""
        now = time.monotonic()
        if not force and now - self._claude_poll.attempted < self._poll_seconds:
            return
        self._claude_poll.attempted = now

        usage = fetch_claude_usage()
        if usage is None:
            # Keep the last good figures, flagged, so the column does not blink
            # to dashes every time the endpoint refuses.
            self._claude = as_stale(self._claude) if populated(self._claude) else QuotaSnapshot()
            if populated(self._claude):
                save_quota(CLAUDE_QUOTA_PATH, self._claude)
            return

        self._claude = claude_usage_to_quota(usage)
        save_quota(CLAUDE_QUOTA_PATH, self._claude)
        self._claude_poll.succeeded = now

    def record_claude(self, observation: ProviderObservation) -> ProviderObservation:
        return _overwrite(observation, self._claude_view())

    def _claude_view(self) -> QuotaSnapshot:
        if not populated(self._claude):
            return self._claude
        last_success = self._claude_poll.succeeded
        if last_success and time.monotonic() - last_success > CLAUDE_TRUST_SECONDS:
            return as_stale(self._claude)
        return self._claude
