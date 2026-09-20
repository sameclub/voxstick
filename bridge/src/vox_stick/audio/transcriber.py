from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TENCENT_HOST = "asr.tencentcloudapi.com"
TENCENT_SERVICE = "asr"
TENCENT_ACTION = "SentenceRecognition"
TENCENT_VERSION = "2019-06-14"
TENCENT_ALGORITHM = "TC3-HMAC-SHA256"
DEFAULT_ENGINE = "16k_zh"
# SentenceRecognition accepts at most 60 s / 3 MB of base64 audio.
MAX_AUDIO_BYTES = 2_200_000
RETRYABLE_ERROR_CODES = {
    "InternalError",
    "RequestLimitExceeded",
    "FailedOperation.ServiceIsolate",
}


@dataclass
class TranscriptionResult:
    text: str = ""
    success: bool = False
    message: str = ""
    source: str = "none"


class TranscriptionAdapter:
    """Project-owned boundary for speech-to-text providers.

    Resolution order: transcript supplied by the request, a development
    override, a local command, then Tencent Cloud SentenceRecognition.
    """

    def transcribe(
        self,
        session_payload: dict[str, Any],
        explicit_text: str = "",
    ) -> TranscriptionResult:
        explicit_text = explicit_text.strip()
        if explicit_text:
            return TranscriptionResult(
                text=explicit_text,
                success=True,
                message="Transcript supplied by request",
                source="request",
            )

        configured_text = os.environ.get("VOX_STICK_TRANSCRIPT_TEXT", "").strip()
        if configured_text:
            return TranscriptionResult(
                text=configured_text,
                success=True,
                message="Transcript supplied by local development override",
                source="env",
            )

        command = os.environ.get("VOX_STICK_TRANSCRIBE_CMD", "").strip()
        if command:
            return _transcribe_with_command(command, session_payload)

        return self._transcribe_with_configured_asr(session_payload)

    def _transcribe_with_configured_asr(self, session_payload: dict[str, Any]) -> TranscriptionResult:
        audio_file_raw = str(session_payload.get("audio_file") or "").strip()
        audio_file = Path(audio_file_raw) if audio_file_raw else None
        if audio_file is None or not audio_file.is_file():
            return TranscriptionResult(
                success=False,
                message="No audio file available for transcription",
                source="none",
            )

        config = load_asr_config()
        if config.get("provider") != "tencent" or not config.get("secret_id") or not config.get("secret_key"):
            return TranscriptionResult(
                success=False,
                message="No transcription adapter configured",
                source="none",
            )
        return transcribe_tencent(audio_file, config)


def load_asr_config() -> dict[str, str]:
    secret_id = (
        os.environ.get("VOX_STICK_TENCENT_SECRET_ID", "").strip()
        or os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip()
    )
    secret_key = (
        os.environ.get("VOX_STICK_TENCENT_SECRET_KEY", "").strip()
        or os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip()
    )
    provider = os.environ.get("VOX_STICK_ASR_PROVIDER", "").strip().lower() or "tencent"
    if provider != "tencent":
        return {}
    return {
        "provider": "tencent",
        "secret_id": secret_id,
        "secret_key": secret_key,
        "engine": os.environ.get("VOX_STICK_ASR_ENGINE", "").strip() or DEFAULT_ENGINE,
    }


def transcribe_tencent(audio_file: Path, config: dict[str, str]) -> TranscriptionResult:
    try:
        audio = audio_file.read_bytes()
    except OSError as exc:
        return TranscriptionResult(success=False, message=f"Could not read audio: {exc}", source="tencent")
    if not audio:
        return TranscriptionResult(success=False, message="Audio file was empty", source="tencent")
    if len(audio) > MAX_AUDIO_BYTES:
        return TranscriptionResult(
            success=False,
            message=f"Audio is {len(audio)} bytes; SentenceRecognition accepts at most {MAX_AUDIO_BYTES}",
            source="tencent",
        )

    attempts = _asr_attempt_count()
    last = TranscriptionResult(success=False, message="Tencent transcription failed", source="tencent")
    for attempt in range(1, attempts + 1):
        last = _transcribe_tencent_once(audio, audio_file.suffix.lstrip(".").lower() or "wav", config)
        if last.success or attempt >= attempts or not _is_retryable(last.message):
            return last
        time.sleep(min(2.0, 0.4 * attempt))
    return last


def _transcribe_tencent_once(
    audio: bytes,
    voice_format: str,
    config: dict[str, str],
    opener=urllib.request.urlopen,  # noqa: ANN001
) -> TranscriptionResult:
    payload = {
        "EngSerViceType": config.get("engine") or DEFAULT_ENGINE,
        "SourceType": 1,
        "VoiceFormat": voice_format if voice_format in {"wav", "pcm", "mp3", "m4a", "aac"} else "wav",
        "UsrAudioKey": f"voxstick-{int(time.time() * 1000)}",
        "Data": base64.b64encode(audio).decode("ascii"),
        "DataLen": len(audio),
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = tencent_headers(
        body,
        secret_id=config["secret_id"],
        secret_key=config["secret_key"],
        action=TENCENT_ACTION,
    )
    request = urllib.request.Request(
        f"https://{TENCENT_HOST}/",
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with opener(request, timeout=_asr_timeout_seconds()) as response:
            raw = response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        return TranscriptionResult(success=False, message=f"Tencent ASR HTTP {exc.code}", source="tencent")
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        return TranscriptionResult(success=False, message=f"Tencent ASR request failed: {exc}", source="tencent")

    return parse_tencent_response(raw)


def parse_tencent_response(raw: bytes) -> TranscriptionResult:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return TranscriptionResult(success=False, message="Tencent ASR returned invalid JSON", source="tencent")
    response = data.get("Response") if isinstance(data, dict) else None
    if not isinstance(response, dict):
        return TranscriptionResult(success=False, message="Tencent ASR returned an unexpected shape", source="tencent")

    error = response.get("Error")
    if isinstance(error, dict):
        code = str(error.get("Code") or "UnknownError")
        message = str(error.get("Message") or "")
        return TranscriptionResult(success=False, message=f"Tencent ASR error {code}: {message}", source="tencent")

    text = str(response.get("Result") or "").strip()
    if not text:
        return TranscriptionResult(success=False, message="Tencent ASR returned no text", source="tencent")
    return TranscriptionResult(text=text, success=True, message="Transcript from Tencent ASR", source="tencent")


SIGNED_HEADERS = "content-type;host;x-tc-action"
CONTENT_TYPE = "application/json; charset=utf-8"


def build_canonical_request(body: bytes, *, host: str, action: str) -> str:
    """The CanonicalRequest of signature v3.

    https://cloud.tencent.com/document/api/1093/35640
    """
    canonical_headers = (
        f"content-type:{CONTENT_TYPE}\n"
        f"host:{host}\n"
        f"x-tc-action:{action.lower()}\n"
    )
    return "\n".join(["POST", "/", "", canonical_headers, SIGNED_HEADERS, _sha256_hex(body)])


def tencent_headers(
    body: bytes,
    *,
    secret_id: str,
    secret_key: str,
    action: str,
    host: str = TENCENT_HOST,
    service: str = TENCENT_SERVICE,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build TC3-HMAC-SHA256 signed headers."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    canonical_request = build_canonical_request(body, host=host, action=action)

    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(
        [
            TENCENT_ALGORITHM,
            str(timestamp),
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )

    secret_date = _hmac_sha256(f"TC3{secret_key}".encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, service)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = _hmac_sha256(secret_signing, string_to_sign).hex()

    authorization = (
        f"{TENCENT_ALGORITHM} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={SIGNED_HEADERS}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "Content-Type": CONTENT_TYPE,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": TENCENT_VERSION,
    }


def _transcribe_with_command(command: str, session_payload: dict[str, Any]) -> TranscriptionResult:
    try:
        result = subprocess.run(
            command,
            input=json.dumps(session_payload),
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=_command_timeout_seconds(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return TranscriptionResult(success=False, message=f"Transcription command failed: {exc}", source="command")

    transcript = result.stdout.strip()
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "Transcription command failed").strip()
        return TranscriptionResult(success=False, message=message, source="command")
    if not transcript:
        return TranscriptionResult(success=False, message="Transcription command returned no text", source="command")
    return TranscriptionResult(
        text=transcript,
        success=True,
        message="Transcript supplied by local command",
        source="command",
    )


def _is_retryable(message: str) -> bool:
    if "Tencent ASR request failed" in message or "timed out" in message.lower():
        return True
    if "Tencent ASR HTTP 5" in message:
        return True
    return any(code in message for code in RETRYABLE_ERROR_CODES)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _command_timeout_seconds() -> int:
    raw = os.environ.get("VOX_STICK_TRANSCRIBE_TIMEOUT_SECONDS", "120")
    try:
        value = int(raw)
    except ValueError:
        return 120
    return max(5, min(600, value))


def _asr_timeout_seconds() -> int:
    raw = os.environ.get("VOX_STICK_ASR_TIMEOUT_SECONDS", "15")
    try:
        value = int(raw)
    except ValueError:
        return 15
    return max(3, min(60, value))


def _asr_attempt_count() -> int:
    raw = os.environ.get("VOX_STICK_ASR_ATTEMPTS", "2")
    try:
        value = int(raw)
    except ValueError:
        return 2
    return max(1, min(5, value))
