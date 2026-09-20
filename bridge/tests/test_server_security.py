import os
import unittest
from unittest import mock

from vox_stick.server import settings


class ServerSecurityTests(unittest.TestCase):
    def test_loopback_host_does_not_require_token(self) -> None:
        self.assertFalse(settings.reaches_beyond_loopback("127.0.0.1"))
        self.assertFalse(settings.reaches_beyond_loopback("localhost"))
        self.assertFalse(settings.reaches_beyond_loopback("::1"))

    def test_non_loopback_host_requires_token(self) -> None:
        self.assertTrue(settings.reaches_beyond_loopback("0.0.0.0"))
        self.assertTrue(settings.reaches_beyond_loopback(""))
        self.assertTrue(settings.reaches_beyond_loopback("192.168.1.10"))

    def test_placeholder_token_is_treated_as_missing(self) -> None:
        with mock.patch.dict(os.environ, {"VOX_STICK_BRIDGE_TOKEN": "change-this-shared-token"}):
            self.assertEqual(settings.BridgeSettings.from_env().token, "")

    def test_real_token_is_used(self) -> None:
        with mock.patch.dict(os.environ, {"VOX_STICK_BRIDGE_TOKEN": "abc123-secret"}):
            self.assertEqual(settings.BridgeSettings.from_env().token, "abc123-secret")


if __name__ == "__main__":
    unittest.main()
