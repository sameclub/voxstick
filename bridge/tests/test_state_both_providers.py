"""The device shows two columns, so /state must carry both agents every poll."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from vox_stick.codex.quota import QuotaSnapshot
from vox_stick.protocol.state import AgentStatus, VoxStickState, default_state, state_from_dict
from vox_stick.providers.base import ProviderObservation
from vox_stick.server import app


def observation(
    provider_id: str,
    *,
    status: AgentStatus = AgentStatus.IDLE,
    online: bool = True,
    quota: tuple[int | None, int | None] = (None, None),
) -> ProviderObservation:
    return ProviderObservation(
        provider_id=provider_id,
        display_name=provider_id.capitalize(),
        online=online,
        status=status,
        project="voxstick",
        quota_5h_remaining=quota[0],
        quota_7d_remaining=quota[1],
        quota_updated_at="09:41" if quota[0] is not None else "",
        quota_stale=False,
        alert_type="NONE",
        alert_message="",
        alert_event_id="",
        latest_event_timestamp=datetime(2026, 9, 20, 9, 41, tzinfo=timezone.utc),
    )


class StoreWithBothProvidersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patches = [
            mock.patch.object(app, "STATE_PATH", root / "state.json"),
            mock.patch.object(app, "QUOTA_PATH", root / "quota.json"),
            mock.patch.object(app, "CLAUDE_QUOTA_PATH", root / "claude-quota.json"),
            mock.patch.object(app, "RECORDING_PATH", root / "recording.json"),
            mock.patch.object(app, "ensure_app_support", lambda: root),
            mock.patch.object(app, "hide_hud", lambda *a, **k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _state(
        self,
        *,
        codex: ProviderObservation,
        claude: ProviderObservation,
        claude_quota: QuotaSnapshot | None = None,
        provider: str = "auto",
    ) -> VoxStickState:
        with mock.patch.object(app, "observe_codex", return_value=codex), mock.patch.object(
            app, "observe_claude", return_value=claude
        ), mock.patch.object(app, "_configured_provider", return_value=provider):
            store = app.BridgeStateStore()
            if claude_quota is not None:
                store._claude_quota = claude_quota
                store._claude_usage_last_success = app.time.monotonic()
            return store.get_state()

    def test_state_carries_claude_even_when_codex_is_active(self) -> None:
        state = self._state(
            codex=observation("codex", status=AgentStatus.RUNNING, quota=(20, 72)),
            claude=observation("claude", status=AgentStatus.IDLE),
            claude_quota=QuotaSnapshot(45, 63, "08:55", False),
        )

        self.assertEqual(state.active_provider, "codex")
        self.assertEqual(state.provider.id, "codex")
        # The column the device is not looking at still has numbers.
        self.assertEqual(state.claude.id, "claude")
        self.assertEqual(state.claude.display_name, "Claude")
        self.assertEqual(state.claude.quota_5h_remaining, 45)
        self.assertEqual(state.claude.quota_7d_remaining, 63)
        self.assertEqual(state.codex.quota_5h_remaining, 20)
        self.assertEqual(state.codex.quota_7d_remaining, 72)

    def test_claude_block_present_in_the_json_payload(self) -> None:
        state = self._state(
            codex=observation("codex", quota=(20, 72)),
            claude=observation("claude", status=AgentStatus.RUNNING),
            claude_quota=QuotaSnapshot(45, 63, "08:55", False),
        )
        payload = json.loads(json.dumps(state.to_jsonable()))

        self.assertIn("claude", payload)
        self.assertEqual(payload["claude"]["status"], "RUNNING")
        self.assertEqual(payload["claude"]["quota_5h_remaining"], 45)
        self.assertEqual(payload["codex"]["quota_5h_remaining"], 20)

    def test_claude_quota_is_null_when_usage_is_unavailable(self) -> None:
        state = self._state(
            codex=observation("codex", quota=(20, 72)),
            claude=observation("claude", status=AgentStatus.IDLE),
        )
        self.assertIsNone(state.claude.quota_5h_remaining)
        self.assertEqual(state.claude.status, AgentStatus.IDLE)

    def test_quota_refresh_updates_both_providers(self) -> None:
        codex = observation("codex", quota=(11, 22))
        claude = observation("claude")
        with mock.patch.object(app, "observe_codex", return_value=codex), mock.patch.object(
            app, "observe_claude", return_value=claude
        ), mock.patch.object(app, "_configured_provider", return_value="codex"):
            store = app.BridgeStateStore()
            store._claude_quota = QuotaSnapshot(45, 63, "08:55", False)
            store._claude_usage_last_success = app.time.monotonic()
            state = store.refresh_quota()

        self.assertEqual(state.codex.quota_5h_remaining, 11)
        self.assertEqual(state.claude.quota_5h_remaining, 45)


class StateSerializationTests(unittest.TestCase):
    def test_default_state_has_a_claude_block(self) -> None:
        payload = default_state().to_jsonable()
        self.assertEqual(payload["claude"]["id"], "claude")
        self.assertEqual(payload["claude"]["status"], "OFFLINE")

    def test_claude_block_round_trips(self) -> None:
        payload = default_state().to_jsonable()
        payload["claude"]["quota_5h_remaining"] = 45
        payload["claude"]["status"] = "RUNNING"

        restored = state_from_dict(payload)
        self.assertEqual(restored.claude.quota_5h_remaining, 45)
        self.assertEqual(restored.claude.status, AgentStatus.RUNNING)

    def test_state_without_a_claude_block_still_loads(self) -> None:
        payload = default_state().to_jsonable()
        payload.pop("claude")
        restored = state_from_dict(payload)
        self.assertEqual(restored.claude.id, "claude")
        self.assertIsNone(restored.claude.quota_5h_remaining)


if __name__ == "__main__":
    unittest.main()
