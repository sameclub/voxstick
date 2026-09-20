import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vox_stick.config import dotenv, paths

LF = chr(10)


class DotenvTests(unittest.TestCase):
    def test_parses_assignments_comments_and_quotes(self) -> None:
        text = LF.join(
            [
                "# a comment",
                "",
                "PLAIN=value",
                'QUOTED="spaced value"',
                "SINGLE='single'",
                "export EXPORTED=exported",
                "EMPTY=",
                "NOT_AN_ASSIGNMENT",
                "WITH_EQUALS=a=b",
            ]
        )
        values = dotenv.parse_env(text)
        self.assertEqual(values["PLAIN"], "value")
        self.assertEqual(values["QUOTED"], "spaced value")
        self.assertEqual(values["SINGLE"], "single")
        self.assertEqual(values["EXPORTED"], "exported")
        self.assertEqual(values["WITH_EQUALS"], "a=b")
        # An empty value means "use the built-in default", so it is not exported.
        self.assertNotIn("EMPTY", values)
        self.assertNotIn("NOT_AN_ASSIGNMENT", values)

    def test_existing_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("VOX_STICK_TEST_A=from-file" + LF + "VOX_STICK_TEST_B=from-file" + LF)
            with mock.patch.dict(os.environ, {"VOX_STICK_TEST_A": "from-shell"}, clear=False):
                os.environ.pop("VOX_STICK_TEST_B", None)
                loaded = dotenv.load_env([env_file])
                self.assertEqual(loaded, env_file)
                self.assertEqual(os.environ["VOX_STICK_TEST_A"], "from-shell")
                self.assertEqual(os.environ["VOX_STICK_TEST_B"], "from-file")
            os.environ.pop("VOX_STICK_TEST_B", None)

    def test_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(dotenv.load_env([Path(tmp) / "absent.env"]))

    def test_candidate_paths_include_the_bridge_directory(self) -> None:
        with mock.patch.dict(os.environ, {"VOX_STICK_ENV_FILE": ""}, clear=False):
            candidates = dotenv.candidate_paths()
        self.assertEqual(candidates[0].name, ".env")
        self.assertEqual(candidates[0].parent.name, "bridge")


class PathsTests(unittest.TestCase):
    def test_platform_default_is_under_the_user_profile(self) -> None:
        directory = paths._default_app_dir()
        self.assertEqual(directory.name, "VoxStick")
        if sys.platform == "win32":
            self.assertIn("AppData", str(directory))
        elif sys.platform == "darwin":
            self.assertIn("Application Support", str(directory))

    def test_home_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"VOX_STICK_HOME": tmp}, clear=False):
                self.assertEqual(paths._app_dir(), Path(tmp))

    def test_derived_paths_sit_in_the_app_directory(self) -> None:
        self.assertEqual(paths.STATE_PATH.parent, paths.APP_SUPPORT_DIR)
        self.assertEqual(paths.RECORDINGS_DIR.parent, paths.APP_SUPPORT_DIR)


if __name__ == "__main__":
    unittest.main()
