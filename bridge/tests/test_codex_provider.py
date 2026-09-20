import unittest
from datetime import datetime, timezone

from unittest import mock

from vox_stick.codex import local_observer
from vox_stick.codex.local_observer import LocalCodexObservation
from vox_stick.codex.quota import QuotaSnapshot
from vox_stick.protocol.state import AgentStatus
from vox_stick.providers.codex import observation_from_local_codex


class CodexProviderTests(unittest.TestCase):
    def test_codex_local_observation_maps_to_provider_observation(self) -> None:
        timestamp = datetime(2026, 6, 28, 9, 41, tzinfo=timezone.utc)
        observation = observation_from_local_codex(
            LocalCodexObservation(
                status=AgentStatus.DONE,
                project="VoxStick",
                quota=QuotaSnapshot(66, 96, "09:40", False),
                quota_found=True,
                alert_type="DONE",
                alert_message="Codex task completed",
                alert_timestamp=timestamp,
                latest_event_timestamp=timestamp,
                codex_online=True,
            )
        )

        self.assertEqual(observation.provider_id, "codex")
        self.assertEqual(observation.display_name, "Codex")
        self.assertEqual(observation.status, AgentStatus.DONE)
        self.assertEqual(observation.quota_5h_remaining, 66)
        self.assertEqual(observation.quota_7d_remaining, 96)
        self.assertEqual(observation.alert_type, "DONE")
        self.assertEqual(observation.alert_event_id, f"evt_{timestamp.astimezone().strftime('%Y%m%d_%H%M%S')}_done")
        self.assertEqual(observation.latest_event_timestamp, timestamp)

    def test_missing_codex_quota_maps_to_unknown_bars(self) -> None:
        observation = observation_from_local_codex(
            LocalCodexObservation(
                status=AgentStatus.IDLE,
                project="VoxStick",
                quota=None,
                quota_found=False,
                codex_online=True,
            )
        )

        self.assertIsNone(observation.quota_5h_remaining)
        self.assertIsNone(observation.quota_7d_remaining)
        self.assertEqual(observation.alert_type, "NONE")


BS = chr(92)
LF = chr(10)


class CodexProcessDetectionTests(unittest.TestCase):
    def _detect(self, output: str) -> bool:
        lines = [line.strip().lower() for line in output.splitlines() if line.strip()]
        with mock.patch.object(local_observer, "command_lines", return_value=lines):
            return local_observer._codex_process_running()

    def test_detects_windows_app_server(self) -> None:
        path = "c:" + BS + "users" + BS + "sam" + BS + "appdata" + BS + "local" + BS + "openai"
        command = '"' + path + BS + 'codex.exe" app-server --listen stdio://'
        self.assertTrue(self._detect(command + LF))

    def test_detects_macos_app_bundle(self) -> None:
        self.assertTrue(self._detect("/Applications/Codex.app/Contents/MacOS/Codex" + LF))

    def test_detects_bare_executable(self) -> None:
        self.assertTrue(self._detect("/usr/local/bin/codex" + LF))

    def test_ignores_a_script_that_merely_mentions_codex(self) -> None:
        # Command lines are collapsed to one line per process precisely so a
        # fragment like this cannot look like an executable named codex.
        self.assertFalse(self._detect('python -c print("codex marker hits") ' + LF + "bash" + LF))

    def test_no_processes_reports_not_running(self) -> None:
        self.assertFalse(self._detect(""))


if __name__ == "__main__":
    unittest.main()
