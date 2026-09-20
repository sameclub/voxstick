import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from vox_stick.protocol.state import AgentStatus, ProviderState, default_state
from vox_stick.codex.quota import QuotaSnapshot
from vox_stick.providers.base import ProviderObservation
from vox_stick.server import arbiter, quotas
from vox_stick.server import settings as bridge_settings


def _choose(preference, last, codex, claude):
    """Drive ProviderArbiter the way the session does, from a known last pick."""
    return arbiter.ProviderArbiter(last).choose(preference, codex, claude)


def _ledger(cached, *, succeeded=0.0):
    """A QuotaLedger with a planted Claude cache and no disk read."""
    ledger = quotas.QuotaLedger.__new__(quotas.QuotaLedger)
    ledger._poll_seconds = 300
    ledger._claude = cached
    ledger._claude_poll = quotas._ClaudePoll(attempted=0.0, succeeded=succeeded)
    return ledger

class ServerProviderTests(unittest.TestCase):
    def test_configured_provider_accepts_known_values_only(self) -> None:
        with mock.patch.dict(os.environ, {"VOX_STICK_PROVIDER": "claude"}):
            self.assertEqual(bridge_settings.BridgeSettings.from_env().provider_preference, "claude")
        with mock.patch.dict(os.environ, {"VOX_STICK_PROVIDER": "bogus"}):
            self.assertEqual(bridge_settings.BridgeSettings.from_env().provider_preference, "auto")

    def test_select_active_provider_respects_pinned_config(self) -> None:
        self.assertEqual(_choose("claude", "codex", self._obs("codex"), self._obs("claude")), "claude")

    def test_select_active_provider_auto_uses_online_provider(self) -> None:
        selected = _choose("auto",
            "codex",
            self._obs("codex", online=False),
            self._obs("claude", online=True),
        )

        self.assertEqual(selected, "claude")

    def test_select_active_provider_auto_uses_recent_activity_when_both_online(self) -> None:
        selected = _choose("auto",
            "codex",
            self._obs("codex", latest=datetime(2026, 6, 28, 9, 0, tzinfo=timezone.utc)),
            self._obs("claude", latest=datetime(2026, 6, 28, 9, 1, tzinfo=timezone.utc)),
        )

        self.assertEqual(selected, "claude")

    def test_select_active_provider_auto_keeps_last_when_none_online(self) -> None:
        selected = _choose("auto",
            "claude",
            self._obs("codex", online=False),
            self._obs("claude", online=False),
        )

        self.assertEqual(selected, "claude")

    def test_select_alert_observation_uses_non_active_provider_alert(self) -> None:
        active = self._obs("claude")
        codex = self._obs(
            "codex",
            status=AgentStatus.DONE,
            alert_type="DONE",
            alert_event_id="evt_codex_done",
            alert_message="Codex task completed",
        )

        selected = arbiter.alert_source(active, codex, active)

        self.assertIs(selected, codex)

    def test_select_alert_observation_prefers_active_provider_alert(self) -> None:
        active = self._obs(
            "claude",
            status=AgentStatus.APPROVAL,
            alert_type="APPROVAL",
            alert_event_id="evt_claude_approval",
            alert_message="Claude is waiting for approval",
        )
        codex = self._obs(
            "codex",
            status=AgentStatus.DONE,
            alert_type="DONE",
            alert_event_id="evt_codex_done",
            alert_message="Codex task completed",
        )

        selected = arbiter.alert_source(active, codex, active)

        self.assertIs(selected, active)

    def test_claude_usage_interval_has_minimum(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bridge_settings.BridgeSettings.from_env().claude_poll_seconds, 300)
        with mock.patch.dict(os.environ, {"VOX_STICK_CLAUDE_USAGE_INTERVAL_SECONDS": "5"}):
            self.assertEqual(bridge_settings.BridgeSettings.from_env().claude_poll_seconds, 30)
        with mock.patch.dict(os.environ, {"VOX_STICK_CLAUDE_USAGE_INTERVAL_SECONDS": "90"}):
            self.assertEqual(bridge_settings.BridgeSettings.from_env().claude_poll_seconds, 90)

    def test_failed_claude_usage_refresh_keeps_cached_quota_stale(self) -> None:
        ledger = _ledger(QuotaSnapshot(66, 96, "09:40", False), succeeded=1.0)

        with mock.patch.object(quotas, "fetch_claude_usage", return_value=None):
            with mock.patch.object(quotas, "save_quota") as save_quota:
                ledger.poll_claude(force=True)

        cached = ledger._claude
        self.assertEqual(cached.quota_5h_remaining, 66)
        self.assertEqual(cached.quota_7d_remaining, 96)
        self.assertTrue(cached.quota_stale)
        save_quota.assert_called_once_with(quotas.CLAUDE_QUOTA_PATH, cached)

    def test_failed_claude_usage_without_cache_remains_unknown(self) -> None:
        ledger = _ledger(QuotaSnapshot())

        with mock.patch.object(quotas, "fetch_claude_usage", return_value=None):
            with mock.patch.object(quotas, "save_quota") as save_quota:
                ledger.poll_claude(force=True)

        self.assertIsNone(ledger._claude.quota_5h_remaining)
        self.assertIsNone(ledger._claude.quota_7d_remaining)
        save_quota.assert_not_called()

    def test_successful_claude_usage_refresh_saves_quota(self) -> None:
        ledger = _ledger(QuotaSnapshot())
        refreshed = QuotaSnapshot(65, 95, "09:41", False)

        with mock.patch.object(quotas, "fetch_claude_usage", return_value=object()):
            with mock.patch.object(quotas, "claude_usage_to_quota", return_value=refreshed):
                with mock.patch.object(quotas, "save_quota") as save_quota:
                    ledger.poll_claude(force=True)

        self.assertEqual(ledger._claude, refreshed)
        save_quota.assert_called_once_with(quotas.CLAUDE_QUOTA_PATH, refreshed)

    def test_claude_quota_can_seed_from_saved_provider_state(self) -> None:
        state = default_state()
        state.provider = ProviderState(
            id="claude",
            display_name="Claude",
            implemented=True,
            status=AgentStatus.IDLE,
            project="VoxStick",
            quota_5h_remaining=26,
            quota_7d_remaining=92,
            quota_updated_at="22:44",
            quota_stale=False,
        )

        snapshot = quotas.salvage_claude(state)

        self.assertEqual(snapshot.quota_5h_remaining, 26)
        self.assertEqual(snapshot.quota_7d_remaining, 92)
        self.assertEqual(snapshot.quota_updated_at, "22:44")
        self.assertTrue(snapshot.quota_stale)

    def test_claude_quota_does_not_seed_from_other_provider_state(self) -> None:
        state = default_state()
        state.provider = ProviderState(
            id="codex",
            display_name="Codex",
            implemented=True,
            status=AgentStatus.IDLE,
            project="VoxStick",
            quota_5h_remaining=26,
            quota_7d_remaining=92,
            quota_updated_at="22:44",
            quota_stale=False,
        )

        snapshot = quotas.salvage_claude(state)

        self.assertIsNone(snapshot.quota_5h_remaining)
        self.assertIsNone(snapshot.quota_7d_remaining)

    def _obs(
        self,
        provider_id: str,
        *,
        online: bool = True,
        latest: datetime | None = None,
        status: AgentStatus = AgentStatus.IDLE,
        alert_type: str = "NONE",
        alert_message: str = "",
        alert_event_id: str = "",
    ) -> ProviderObservation:
        return ProviderObservation(
            provider_id=provider_id,
            display_name=provider_id.title(),
            online=online,
            status=status,
            project="VoxStick",
            quota_5h_remaining=None,
            quota_7d_remaining=None,
            quota_updated_at="",
            quota_stale=False,
            alert_type=alert_type,
            alert_message=alert_message,
            alert_event_id=alert_event_id,
            latest_event_timestamp=latest,
        )


if __name__ == "__main__":
    unittest.main()
