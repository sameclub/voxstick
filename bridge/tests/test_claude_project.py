import os
import unittest
from pathlib import Path
from unittest.mock import patch

from vox_stick.providers import claude


class ClaudeProjectTests(unittest.TestCase):
    def observe(self, events, configured=""):
        with patch.dict(os.environ, {"VOX_STICK_PROJECT_NAME": configured}), \
             patch.object(claude, "_claude_process_running", return_value=True), \
             patch.object(claude, "session_files", return_value=[Path("session.jsonl")]), \
             patch.object(claude, "tail_json_events", return_value=events):
            return claude.observe_claude(Path("samestick-tool")).project

    def event(self, session, second, **fields):
        return dict(sessionId=session, timestamp=f"2026-09-20T13:00:{second:02d}Z",
                    type="assistant", **fields)

    def test_uses_active_session_even_when_latest_event_has_no_cwd(self):
        events = [self.event("a", 1, cwd=r"C:\Users\Sam\ESP32"),
                  self.event("b", 2, cwd="/home/sam/other-project"),
                  self.event("a", 3)]
        self.assertEqual(self.observe(events), "ESP32")
        self.assertEqual(self.observe(list(reversed(events))), "ESP32")

    def test_does_not_borrow_another_sessions_project(self):
        self.assertEqual(self.observe([self.event("a", 1, cwd="/work/old"),
                                       self.event("b", 2)]), "Unknown")

    def test_explicit_name_override(self):
        self.assertEqual(self.observe([self.event("a", 1, cwd="/work/real")],
                                      "Custom"), "Custom")

    def test_posix_path_and_directory_changes(self):
        self.assertEqual(self.observe([self.event("a", 1, cwd="/work/old"),
                                       self.event("a", 2, cwd="/work/new/")]), "new")


if __name__ == "__main__":
    unittest.main()
