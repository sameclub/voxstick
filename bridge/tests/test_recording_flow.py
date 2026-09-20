import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from vox_stick.audio import recorder
from vox_stick.audio.transcriber import TranscriptionResult
from vox_stick.paste.input_injector import PasteResult

SAMPLE_RATE = 16000


def speech_like_pcm(seconds: float = 1.2, amplitude: int = 6000) -> bytes:
    """Loud enough to clear the recorder's silence and speech-window gates."""
    count = int(SAMPLE_RATE * seconds)
    samples = [amplitude if (index // 40) % 2 == 0 else -amplitude for index in range(count)]
    return struct.pack(f"<{count}h", *samples)


class RecordingFlowTests(unittest.TestCase):
    def test_provider_is_saved_and_does_not_switch_at_stop(self):
        self.controller.start({"provider": "codex"})
        self.assertEqual(recorder.RecordingController(self.controller.path).session.target_provider, "codex")
        with mock.patch.object(self.controller.paste_injector, "paste", return_value=PasteResult(True, "ok")) as paste:
            session = self.controller.stop({"provider": "claude", "text": "routing test", "paste": True})
        paste.assert_called_once_with("routing test", press_enter=True, target_provider="codex")
        self.assertTrue(session.pasted)

    def test_recovered_upload_routes_using_stop_provider(self):
        self.controller.attach_pcm(speech_like_pcm(), session_id="recovered123")
        with mock.patch.object(self.controller.paste_injector, "paste", return_value=PasteResult(True, "ok")) as paste:
            self.controller.stop({"provider": "claude", "text": "routing test"})
        paste.assert_called_once_with("routing test", press_enter=True, target_provider="claude")

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            self.controller.start({"provider": "other"})

    def test_missing_provider_preserves_transcript_without_sending(self):
        self.controller.start({"audio_source": "s3ai_pcm"})
        with mock.patch.object(self.controller.paste_injector, "paste") as paste:
            session = self.controller.stop({"text": "routing test", "paste": True})
        paste.assert_not_called()
        self.assertFalse(session.pasted)
        self.assertEqual(session.status, "paste_failed")
        self.assertEqual(session.transcript, "routing test")
        self.assertIn("v1.2", session.message)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.recordings = root / "Recordings"
        self.recordings.mkdir()
        patches = [
            mock.patch.object(recorder, "RECORDINGS_DIR", self.recordings),
            mock.patch.object(recorder, "show_hud", lambda *a, **k: None),
            mock.patch.object(recorder, "hide_hud", lambda *a, **k: None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.controller = recorder.RecordingController(root / "recording.json")

    def test_uploaded_pcm_becomes_a_wav_file(self) -> None:
        session = self.controller.start({"audio_source": "s3ai_pcm", "session_id": "abcdefgh1234"})
        self.assertTrue(session.active)
        self.assertEqual(session.audio_source, "device_pcm")

        pcm = speech_like_pcm(0.5)
        session = self.controller.attach_pcm(pcm, session_id="abcdefgh1234", sample_rate=SAMPLE_RATE)
        audio_file = Path(session.audio_file)
        self.assertTrue(audio_file.is_file())
        with wave.open(str(audio_file), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), SAMPLE_RATE)
            self.assertEqual(wav.getnframes(), len(pcm) // 2)

    def test_stop_transcribes_and_pastes(self) -> None:
        self.controller.start({"provider": "codex", "audio_source": "s3ai_pcm", "session_id": "abcdefgh1234"})
        self.controller.attach_pcm(speech_like_pcm(), session_id="abcdefgh1234", sample_rate=SAMPLE_RATE)

        transcribe = mock.Mock(
            return_value=TranscriptionResult(text="打开串口监视器", success=True, message="ok", source="tencent")
        )
        paste = mock.Mock(return_value=PasteResult(True, "Pasted into the focused app"))
        with mock.patch.object(self.controller.transcriber, "transcribe", transcribe), mock.patch.object(
            self.controller.paste_injector, "paste", paste
        ):
            session = self.controller.stop({"paste": True})

        self.assertEqual(session.status, "pasted")
        self.assertEqual(session.transcript, "打开串口监视器")
        self.assertTrue(session.pasted)
        paste.assert_called_once()

    def test_silent_audio_is_skipped_before_transcription(self) -> None:
        self.controller.start({"audio_source": "s3ai_pcm", "session_id": "abcdefgh1234"})
        self.controller.attach_pcm(b"\x00\x00" * SAMPLE_RATE, session_id="abcdefgh1234", sample_rate=SAMPLE_RATE)

        transcribe = mock.Mock()
        with mock.patch.object(self.controller.transcriber, "transcribe", transcribe):
            session = self.controller.stop({"paste": True})

        self.assertEqual(session.status, "audio_skipped")
        self.assertFalse(session.pasted)
        transcribe.assert_not_called()

    def test_too_short_audio_is_skipped(self) -> None:
        self.controller.start({"audio_source": "s3ai_pcm", "session_id": "abcdefgh1234"})
        self.controller.attach_pcm(speech_like_pcm(0.3), session_id="abcdefgh1234", sample_rate=SAMPLE_RATE)

        transcribe = mock.Mock()
        with mock.patch.object(self.controller.transcriber, "transcribe", transcribe):
            session = self.controller.stop({"paste": True})

        self.assertEqual(session.status, "audio_skipped")
        transcribe.assert_not_called()

    def test_empty_upload_is_reported(self) -> None:
        self.controller.start({"audio_source": "s3ai_pcm", "session_id": "abcdefgh1234"})
        session = self.controller.attach_pcm(b"", session_id="abcdefgh1234")
        self.assertEqual(session.status, "audio_failed")

    def test_mismatched_session_is_rejected(self) -> None:
        self.controller.start({"audio_source": "s3ai_pcm", "session_id": "abcdefgh1234"})
        session = self.controller.attach_pcm(speech_like_pcm(0.5), session_id="zzzzzzzz9999")
        self.assertEqual(session.status, "audio_failed")

    def test_known_hallucination_transcript_is_rejected(self) -> None:
        self.controller.start({"audio_source": "s3ai_pcm", "session_id": "abcdefgh1234"})
        self.controller.attach_pcm(speech_like_pcm(), session_id="abcdefgh1234", sample_rate=SAMPLE_RATE)

        hallucination = recorder.KNOWN_ASR_HALLUCINATIONS[0]
        transcribe = mock.Mock(
            return_value=TranscriptionResult(text=hallucination, success=True, message="ok", source="tencent")
        )
        paste = mock.Mock()
        with mock.patch.object(self.controller.transcriber, "transcribe", transcribe), mock.patch.object(
            self.controller.paste_injector, "paste", paste
        ):
            session = self.controller.stop({"paste": True})

        self.assertEqual(session.status, "transcript_rejected")
        paste.assert_not_called()


if __name__ == "__main__":
    unittest.main()
