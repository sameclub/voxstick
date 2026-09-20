from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import time
import wave
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from vox_stick.audio.transcriber import TranscriptionAdapter
from vox_stick.config.paths import RECORDINGS_DIR
from vox_stick.desktop.hud import hide_hud, show_hud
from vox_stick.paste.input_injector import PasteInjector, PasteResult

MIN_AUDIO_DURATION_SECONDS = 0.7
MIN_AUDIO_RMS = 120.0
MIN_SPEECH_SECONDS = 0.28
MIN_SPEECH_WINDOWS = 3
SPEECH_WINDOW_SECONDS = 0.10
SPEECH_RMS_THRESHOLD = 900.0
SPEECH_EDGE_IGNORE_SECONDS = 0.18
KNOWN_ASR_HALLUCINATIONS = (
    "\u8bf7\u4e0d\u541d\u70b9\u8d5e\u8ba2\u9605\u8f6c\u53d1\u6253\u8d4f\u652f\u6301\u660e\u955c\u4e0e\u70b9\u70b9\u680f\u76ee",
    "\u8bf7\u4f7f\u7528\u7b80\u4f53\u4e2d\u6587\u8f93\u51fa\u3002",
)


@dataclass
class RecordingSession:
    session_id: str = ""
    active: bool = False
    started_at: str = ""
    stopped_at: str = ""
    status: str = "idle"
    message: str = ""
    transcript: str = ""
    transcript_source: str = "none"
    pasted: bool = False
    audio_file: str = ""
    audio_source: str = "none"
    target_provider: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AudioMetrics:
    duration_seconds: float
    audio_bytes: int
    rms: float
    ac_rms: float
    speech_seconds: float
    speech_windows: int


class RecordingController:
    """project-owned push-to-talk session boundary."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.transcriber = TranscriptionAdapter()
        self.paste_injector = PasteInjector()
        self.session = self._load()

    def start(self, request: dict[str, Any] | None = None) -> RecordingSession:
        request = request or {}
        target_provider = str(request.get("provider") or "").strip().lower()
        if target_provider not in {"", "codex", "claude"}:
            raise ValueError("Unsupported recording provider")
        requested_source = str(request.get("audio_source") or request.get("source") or "")
        requested_session_id = _requested_session_id(request)
        self.session = RecordingSession(
            session_id=requested_session_id or uuid.uuid4().hex,
            active=True,
            started_at=datetime.now().isoformat(timespec="seconds"),
            stopped_at="",
            status="recording",
            message="Recording session started",
            target_provider=target_provider,
        )
        # The device is the only microphone. There is no host-side AVFoundation
        # fallback; the recording command hooks cover that role portably.
        self.session.audio_source = "device_pcm"
        self.session.message = f"Waiting for audio upload from {requested_source or 'device'}"
        show_hud("listening")

        hook = _run_command_hook("VOX_STICK_RECORDING_START_CMD", self.session.to_jsonable(), timeout=15)
        if hook is not None and not hook[0]:
            self.session.active = False
            self.session.stopped_at = datetime.now().isoformat(timespec="seconds")
            self.session.status = "start_failed"
            self.session.message = hook[2] or "Recording start command failed"
            show_hud("failed", hold_seconds=1.8)
        self._save()
        return self.session

    def attach_pcm(
        self,
        pcm: bytes,
        *,
        session_id: str = "",
        sample_rate: int = 16000,
        channels: int = 1,
        bits_per_sample: int = 16,
    ) -> RecordingSession:
        if not pcm:
            self.session.status = "audio_failed"
            self.session.message = "Uploaded audio was empty"
            show_hud("failed", hold_seconds=1.8)
            self._save()
            return self.session
        session_id = _clean_session_id(session_id)
        if session_id and self.session.session_id and session_id != self.session.session_id and self.session.active:
            self.session.status = "audio_failed"
            self.session.message = "Uploaded audio session did not match active recording"
            show_hud("failed", hold_seconds=1.8)
            self._save()
            return self.session
        if session_id and (not self.session.session_id or session_id != self.session.session_id):
            self.session = RecordingSession(
                session_id=session_id,
                active=True,
                started_at=datetime.now().isoformat(timespec="seconds"),
                status="recording",
                message="Recovered recording session from the device audio upload",
                audio_source="device_pcm",
            )
        if bits_per_sample != 16:
            self.session.status = "audio_failed"
            self.session.message = "Only 16-bit PCM audio is supported"
            show_hud("failed", hold_seconds=1.8)
            self._save()
            return self.session

        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        sid = self.session.session_id or session_id or uuid.uuid4().hex
        audio_file = RECORDINGS_DIR / f"{sid}.wav"
        with wave.open(str(audio_file), "wb") as wav:
            wav.setnchannels(max(1, channels))
            wav.setsampwidth(bits_per_sample // 8)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)

        self.session.audio_file = str(audio_file)
        self.session.audio_source = "device_pcm"
        self.session.message = "Device audio uploaded"
        show_hud("sending")
        self._save()
        return self.session

    def stop(self, request: dict[str, Any] | None = None) -> RecordingSession:
        request = request or {}
        # An upload may recover a session when the start request was lost.
        if not self.session.target_provider and request.get("provider"):
            provider = str(request["provider"]).strip().lower()
            if provider not in {"codex", "claude"}:
                raise ValueError("Unsupported recording provider")
            self.session.target_provider = provider
        self.session.active = False
        self.session.stopped_at = datetime.now().isoformat(timespec="seconds")
        explicit_text = str(request.get("text") or request.get("transcript") or "")
        stop_hook_source = False
        stop_hook = _run_command_hook("VOX_STICK_RECORDING_STOP_CMD", self.session.to_jsonable(), timeout=120)
        if stop_hook is not None:
            hook_ok, hook_stdout, hook_stderr = stop_hook
            if hook_ok and hook_stdout.strip():
                explicit_text = hook_stdout.strip()
                stop_hook_source = True
            elif not hook_ok:
                self.session.status = "stop_failed"
                self.session.message = hook_stderr or "Recording stop command failed"
                show_hud("failed", hold_seconds=1.8)
                self._save_stop_result()
                return self.session
        should_paste = bool(request.get("paste", True))
        show_hud("transcribing")

        if not explicit_text:
            metrics = _wav_metrics(self.session.audio_file)
            if metrics is not None:
                print(
                    "recording audio metrics "
                    f"session={self.session.session_id} "
                    f"file={self.session.audio_file} "
                    f"bytes={metrics.audio_bytes} "
                    f"duration={metrics.duration_seconds:.3f}s "
                    f"rms={metrics.rms:.1f} "
                    f"ac_rms={metrics.ac_rms:.1f} "
                    f"speech_seconds={metrics.speech_seconds:.2f} "
                    f"speech_windows={metrics.speech_windows}",
                    flush=True,
                )
                if metrics.duration_seconds < MIN_AUDIO_DURATION_SECONDS:
                    self.session.pasted = False
                    self.session.status = "audio_skipped"
                    self.session.message = (
                        f"Audio too short for transcription: {metrics.duration_seconds:.2f}s"
                    )
                    show_hud("unclear", hold_seconds=1.8)
                    self._save_stop_result()
                    return self.session
                if metrics.rms < MIN_AUDIO_RMS:
                    self.session.pasted = False
                    self.session.status = "audio_skipped"
                    self.session.message = f"Audio appears silent: rms={metrics.rms:.1f}"
                    show_hud("unclear", hold_seconds=1.8)
                    self._save_stop_result()
                    return self.session
                if (
                    metrics.speech_seconds < MIN_SPEECH_SECONDS
                    or metrics.speech_windows < MIN_SPEECH_WINDOWS
                ):
                    self.session.pasted = False
                    self.session.status = "audio_skipped"
                    self.session.message = (
                        "No clear speech detected before transcription: "
                        f"speech_seconds={metrics.speech_seconds:.2f}"
                    )
                    show_hud("unclear", hold_seconds=1.8)
                    self._save_stop_result()
                    return self.session

        transcript = self.transcriber.transcribe(
            self.session.to_jsonable(),
            explicit_text=explicit_text,
        )
        self.session.transcript_source = "recording_stop_cmd" if stop_hook_source else transcript.source
        if transcript.success and transcript.text:
            self.session.transcript = transcript.text
            transcript_message = (
                "Transcript supplied by recording stop command"
                if stop_hook_source else transcript.message
            )
            rejection_reason = _transcript_rejection_reason(transcript.text)
            if rejection_reason:
                self.session.pasted = False
                self.session.status = "transcript_rejected"
                self.session.message = rejection_reason
                show_hud("unclear", hold_seconds=1.8)
                print(
                    "recording transcript rejected "
                    f"session={self.session.session_id} "
                    f"source={self.session.transcript_source} "
                    f"reason={rejection_reason}",
                    flush=True,
                )
                self._save_stop_result()
                return self.session
            if should_paste:
                if self.session.target_provider:
                    paste = self.paste_injector.paste(
                        transcript.text, press_enter=True,
                        target_provider=self.session.target_provider,
                    )
                else:
                    paste = PasteResult(False, "Recording request has no provider. Update VoxStick to v1.2 or later; transcript saved, nothing sent.")
                self.session.pasted = paste.success
                self.session.status = "pasted" if paste.success else "paste_failed"
                self.session.message = paste.message if paste.success else f"{transcript_message}; {paste.message}"
                if paste.success:
                    hide_hud(delay_seconds=0.5)
                else:
                    show_hud("failed", hold_seconds=1.8)
            else:
                self.session.pasted = False
                self.session.status = "transcribed"
                self.session.message = transcript_message
                hide_hud(delay_seconds=0.5)
        else:
            self.session.pasted = False
            self.session.status = "transcription_failed"
            self.session.message = transcript.message
            show_hud("failed", hold_seconds=1.8)
        self._save_stop_result()
        return self.session

    def _load(self) -> RecordingSession:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return RecordingSession()
        return RecordingSession(
            session_id=str(data.get("session_id") or ""),
            active=bool(data.get("active", False)),
            started_at=str(data.get("started_at") or ""),
            stopped_at=str(data.get("stopped_at") or ""),
            status=str(data.get("status") or "idle"),
            message=str(data.get("message") or ""),
            transcript=str(data.get("transcript") or ""),
            transcript_source=str(data.get("transcript_source") or "none"),
            pasted=bool(data.get("pasted", False)),
            audio_file=str(data.get("audio_file") or ""),
            audio_source=str(data.get("audio_source") or "none"),
            target_provider=str(data.get("target_provider") or ""),
        )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.session.to_jsonable(), indent=2) + "\n", encoding="utf-8")

    def _save_stop_result(self) -> None:
        self._save()
        print(
            "recording stop result "
            f"session={self.session.session_id} "
            f"status={self.session.status} "
            f"source={self.session.transcript_source} "
            f"audio_source={self.session.audio_source} "
            f"transcript_chars={len(self.session.transcript)} "
            f"pasted={self.session.pasted} "
            f"message={self.session.message}",
            flush=True,
        )


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _wav_metrics(audio_file: str) -> AudioMetrics | None:
    if not audio_file:
        return None
    path = Path(audio_file)
    if not path.is_file() or path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            sample_width = wav.getsampwidth()
            raw = wav.readframes(frames)
    except (OSError, wave.Error):
        return None

    duration_seconds = frames / rate if rate else 0.0
    if sample_width != 2 or not raw:
        return AudioMetrics(duration_seconds, len(raw), 0.0, 0.0, 0.0, 0)

    usable_len = len(raw) - (len(raw) % 2)
    samples = [sample for (sample,) in struct.iter_unpack("<h", raw[:usable_len])]
    count = len(samples)
    if count == 0:
        return AudioMetrics(duration_seconds, len(raw), 0.0, 0.0, 0.0, 0)
    total = sum(int(sample) * int(sample) for sample in samples)
    rms = math.sqrt(total / count) if count else 0.0
    mean = sum(samples) / count
    ac_rms = math.sqrt(sum((sample - mean) * (sample - mean) for sample in samples) / count)

    window_size = max(1, int(rate * SPEECH_WINDOW_SECONDS)) if rate else count
    edge_windows = int(math.ceil(SPEECH_EDGE_IGNORE_SECONDS / SPEECH_WINDOW_SECONDS))
    window_rms: list[float] = []
    for start in range(0, count, window_size):
        chunk = samples[start:start + window_size]
        if len(chunk) < max(1, window_size // 2):
            continue
        chunk_mean = sum(chunk) / len(chunk)
        chunk_rms = math.sqrt(
            sum((sample - chunk_mean) * (sample - chunk_mean) for sample in chunk) / len(chunk)
        )
        window_rms.append(chunk_rms)
    speech_windows = 0
    for index, value in enumerate(window_rms):
        if index < edge_windows or index >= len(window_rms) - edge_windows:
            continue
        if value >= SPEECH_RMS_THRESHOLD:
            speech_windows += 1
    speech_seconds = speech_windows * SPEECH_WINDOW_SECONDS
    return AudioMetrics(duration_seconds, len(raw), rms, ac_rms, speech_seconds, speech_windows)


def _transcript_rejection_reason(text: str) -> str:
    normalized = _normalized_transcript(text)
    for phrase in KNOWN_ASR_HALLUCINATIONS:
        if phrase in normalized:
            return "Rejected known ASR hallucination transcript"
    return ""


def _normalized_transcript(text: str) -> str:
    return "".join(str(text).split()).lower()


def _requested_session_id(request: dict[str, Any]) -> str:
    return _clean_session_id(str(request.get("session_id") or ""))


def _clean_session_id(raw: str) -> str:
    value = raw.strip()
    if not 8 <= len(value) <= 64:
        return ""
    if not all(ch.isalnum() or ch in {"-", "_"} for ch in value):
        return ""
    return value


def _run_command_hook(
    env_name: str,
    payload: dict[str, Any],
    timeout: int,
) -> tuple[bool, str, str] | None:
    command = os.environ.get(env_name, "").strip()
    if not command:
        return None
    try:
        result = subprocess.run(
            command,
            input=json.dumps(payload),
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return (False, "", str(exc))
    return (result.returncode == 0, result.stdout, result.stderr.strip())

