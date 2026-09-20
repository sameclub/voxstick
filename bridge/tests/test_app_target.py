import unittest
from unittest import mock

from vox_stick.paste import input_injector, app_target
from vox_stick.paste.input_injector import PasteResult


class TargetPasteTests(unittest.TestCase):
    def setUp(self):
        self.backend = mock.Mock()
        self.backend.read_clipboard.return_value = "previous"
        self.backend.write_clipboard.return_value = PasteResult(True, "ok")
        self.backend.send_paste.return_value = PasteResult(True, "ok")
        with mock.patch.object(input_injector, "_select_backend", return_value=self.backend):
            self.injector = input_injector.PasteInjector()

    def test_routes_both_apps_and_sends(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), \
                 mock.patch.object(app_target, "focus_composer", return_value=123) as focus, \
                 mock.patch.object(app_target, "is_foreground", return_value=True), \
                 mock.patch.object(input_injector.time, "sleep"):
                result = self.injector.paste("voice text", press_enter=True, target_provider=provider)
                self.assertTrue(result.success)
                focus.assert_called_once_with(provider)
                self.backend.send_paste.assert_called_with(press_enter=True)
                self.backend.write_clipboard.assert_called_with("previous")

    def test_focus_failure_does_not_paste_or_change_clipboard(self):
        with mock.patch.object(app_target, "focus_composer", side_effect=RuntimeError("No input")):
            result = self.injector.paste("voice text", target_provider="claude")
        self.assertFalse(result.success)
        self.backend.write_clipboard.assert_not_called()
        self.backend.send_paste.assert_not_called()

    def test_focus_lost_before_paste_restores_clipboard(self):
        with mock.patch.object(app_target, "focus_composer", return_value=123), \
             mock.patch.object(app_target, "is_foreground", return_value=False):
            result = self.injector.paste("voice text", target_provider="codex")
        self.assertFalse(result.success)
        self.backend.send_paste.assert_not_called()
        self.backend.write_clipboard.assert_called_with("previous")

    def test_unknown_target_rejected(self):
        with self.assertRaises(RuntimeError):
            app_target.focus_composer("other")

    def test_windows_enter_failure_is_reported(self):
        backend = input_injector._WindowsBackend.__new__(input_injector._WindowsBackend)
        backend._user32 = mock.Mock()
        backend._user32.GetForegroundWindow.return_value = 123
        backend._send_keys = mock.Mock(side_effect=[True, False])
        with mock.patch.object(input_injector.time, "sleep"):
            result = backend.send_paste(press_enter=True)
        self.assertFalse(result.success)
        self.assertIn("send failed", result.message)

    def test_windows_does_not_send_after_window_switch(self):
        backend = input_injector._WindowsBackend.__new__(input_injector._WindowsBackend)
        backend._user32 = mock.Mock()
        backend._user32.GetForegroundWindow.side_effect = [123, 456]
        backend._send_keys = mock.Mock(return_value=True)
        with mock.patch.object(input_injector.time, "sleep"):
            result = backend.send_paste(press_enter=True)
        self.assertFalse(result.success)
        self.assertEqual(backend._send_keys.call_count, 1)
