import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vox_stick.audio import transcriber

BS = chr(92)

# The worked example from https://cloud.tencent.com/document/api/213/30654.
# The docs mask the example credentials, so the fixed reference values are the
# payload hash and the CanonicalRequest hash, which pin down everything about
# the request that is easy to get wrong.
DOC_PAYLOAD = (
    '{"Limit": 1, "Filters": [{"Values": ["'
    + BS + "u672a" + BS + "u547d" + BS + "u540d"
    + '"], "Name": "instance-name"}]}'
)
DOC_PAYLOAD_SHA256 = "35e9c5b0e3ae67532d3c9f17ead6c90222632e5b1ff7f6e89887f1398934f064"
DOC_CANONICAL_REQUEST_SHA256 = "7019a55be8395899b900fb5564e4200d984910f34794a27cb3fb7d10ff6a1e84"
DOC_TIMESTAMP = 1551113065


class TencentSignatureTests(unittest.TestCase):
    def test_payload_hash_matches_tencent_example(self) -> None:
        self.assertEqual(hashlib.sha256(DOC_PAYLOAD.encode("utf-8")).hexdigest(), DOC_PAYLOAD_SHA256)

    def test_canonical_request_matches_tencent_example(self) -> None:
        canonical = transcriber.build_canonical_request(
            DOC_PAYLOAD.encode("utf-8"),
            host="cvm.tencentcloudapi.com",
            action="DescribeInstances",
        )
        self.assertEqual(
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            DOC_CANONICAL_REQUEST_SHA256,
        )

    def test_credential_scope_uses_utc_date_of_the_timestamp(self) -> None:
        headers = transcriber.tencent_headers(
            DOC_PAYLOAD.encode("utf-8"),
            secret_id="AKIDEXAMPLE",
            secret_key="SECRETEXAMPLE",
            action="DescribeInstances",
            host="cvm.tencentcloudapi.com",
            service="cvm",
            timestamp=DOC_TIMESTAMP,
        )
        self.assertIn("Credential=AKIDEXAMPLE/2019-02-25/cvm/tc3_request", headers["Authorization"])
        self.assertIn("SignedHeaders=content-type;host;x-tc-action", headers["Authorization"])
        self.assertEqual(headers["X-TC-Timestamp"], str(DOC_TIMESTAMP))

    def test_signature_is_deterministic_and_key_dependent(self) -> None:
        def sign(secret_key: str) -> str:
            headers = transcriber.tencent_headers(
                b"{}",
                secret_id="AKIDEXAMPLE",
                secret_key=secret_key,
                action="SentenceRecognition",
                timestamp=DOC_TIMESTAMP,
            )
            return headers["Authorization"].rsplit("Signature=", 1)[1]

        self.assertEqual(sign("key-a"), sign("key-a"))
        self.assertNotEqual(sign("key-a"), sign("key-b"))

    def test_asr_request_targets_sentence_recognition(self) -> None:
        headers = transcriber.tencent_headers(
            b"{}",
            secret_id="AKIDEXAMPLE",
            secret_key="SECRETEXAMPLE",
            action=transcriber.TENCENT_ACTION,
        )
        self.assertEqual(headers["X-TC-Action"], "SentenceRecognition")
        self.assertEqual(headers["X-TC-Version"], "2019-06-14")
        self.assertEqual(headers["Host"], "asr.tencentcloudapi.com")


class TencentResponseTests(unittest.TestCase):
    def test_successful_response_yields_text(self) -> None:
        raw = json.dumps({"Response": {"Result": " 把这段提交上去 ", "RequestId": "abc"}}).encode("utf-8")
        result = transcriber.parse_tencent_response(raw)
        self.assertTrue(result.success)
        self.assertEqual(result.text, "把这段提交上去")
        self.assertEqual(result.source, "tencent")

    def test_error_response_is_reported(self) -> None:
        raw = json.dumps(
            {"Response": {"Error": {"Code": "AuthFailure.SignatureFailure", "Message": "bad signature"}}}
        ).encode("utf-8")
        result = transcriber.parse_tencent_response(raw)
        self.assertFalse(result.success)
        self.assertIn("AuthFailure.SignatureFailure", result.message)

    def test_empty_result_is_a_failure(self) -> None:
        raw = json.dumps({"Response": {"Result": "", "RequestId": "abc"}}).encode("utf-8")
        self.assertFalse(transcriber.parse_tencent_response(raw).success)

    def test_invalid_json_is_a_failure(self) -> None:
        self.assertFalse(transcriber.parse_tencent_response(b"not json").success)


class TranscriptionAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(
            os.environ,
            {
                "VOX_STICK_TRANSCRIPT_TEXT": "",
                "VOX_STICK_TRANSCRIBE_CMD": "",
                "VOX_STICK_TENCENT_SECRET_ID": "",
                "VOX_STICK_TENCENT_SECRET_KEY": "",
                "TENCENTCLOUD_SECRET_ID": "",
                "TENCENTCLOUD_SECRET_KEY": "",
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_explicit_text_wins(self) -> None:
        result = transcriber.TranscriptionAdapter().transcribe({}, explicit_text="  hello  ")
        self.assertTrue(result.success)
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.source, "request")

    def test_env_override(self) -> None:
        os.environ["VOX_STICK_TRANSCRIPT_TEXT"] = "from env"
        result = transcriber.TranscriptionAdapter().transcribe({})
        self.assertEqual(result.text, "from env")
        self.assertEqual(result.source, "env")

    def test_local_command_output_is_used(self) -> None:
        os.environ["VOX_STICK_TRANSCRIBE_CMD"] = f'"{sys.executable}" -c "print(\'from command\')"'
        result = transcriber.TranscriptionAdapter().transcribe({})
        self.assertTrue(result.success, result.message)
        self.assertEqual(result.text, "from command")
        self.assertEqual(result.source, "command")

    def test_missing_credentials_reports_no_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "take.wav"
            audio.write_bytes(b"RIFF0000WAVE")
            result = transcriber.TranscriptionAdapter().transcribe({"audio_file": str(audio)})
        self.assertFalse(result.success)
        self.assertEqual(result.message, "No transcription adapter configured")

    def test_missing_audio_file_reports_clearly(self) -> None:
        result = transcriber.TranscriptionAdapter().transcribe({"audio_file": ""})
        self.assertFalse(result.success)
        self.assertEqual(result.message, "No audio file available for transcription")

    def test_oversized_audio_is_rejected_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "take.wav"
            audio.write_bytes(b"0" * (transcriber.MAX_AUDIO_BYTES + 1))
            result = transcriber.transcribe_tencent(
                audio, {"secret_id": "a", "secret_key": "b", "engine": "16k_zh"}
            )
        self.assertFalse(result.success)
        self.assertIn("accepts at most", result.message)


class AsrConfigTests(unittest.TestCase):
    def test_tencent_config_from_generic_env_names(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "VOX_STICK_ASR_PROVIDER": "",
                "VOX_STICK_TENCENT_SECRET_ID": "",
                "VOX_STICK_TENCENT_SECRET_KEY": "",
                "TENCENTCLOUD_SECRET_ID": "id-from-sdk-env",
                "TENCENTCLOUD_SECRET_KEY": "key-from-sdk-env",
                "VOX_STICK_ASR_ENGINE": "",
            },
            clear=False,
        ):
            config = transcriber.load_asr_config()
        self.assertEqual(config["secret_id"], "id-from-sdk-env")
        self.assertEqual(config["engine"], "16k_zh")

    def test_unknown_provider_yields_no_config(self) -> None:
        with mock.patch.dict(os.environ, {"VOX_STICK_ASR_PROVIDER": "groq"}, clear=False):
            self.assertEqual(transcriber.load_asr_config(), {})


if __name__ == "__main__":
    unittest.main()
