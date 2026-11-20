"""Unit tests of get_data/paths.py: where the project keeps its own files.

- in a clone everything stays in the repository folder, as before
- installed with a regular pip install, the files go to the user's local data folder,
  on Windows never the roaming profile; saving the settings creates that folder

Run:  python main.py debug                 (every test in tests/)
      python -m unittest tests.test_paths  (this file only)
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from get_data import PROJECT_NAME, config, paths

ROOT = Path(__file__).resolve().parent.parent
HOME = Path("/home/someone")


class CloneTest(unittest.TestCase):
    """A checkout of the repository keeps its files in the repository folder."""

    def test_this_checkout_is_a_clone(self):
        self.assertTrue(paths.is_clone(ROOT))
        self.assertEqual(paths.DATA_ROOT, ROOT)

    def test_files_stay_where_the_readme_says(self):
        self.assertEqual(paths.CONFIG_PATH, ROOT / "db_config.json")
        self.assertEqual(paths.LOG_DIR, ROOT / "logs")
        self.assertEqual(paths.TMP_DIR, ROOT / "tmp")
        self.assertEqual(paths.DASHBOARD_DIR, ROOT / "docs")

    def test_a_folder_without_pyproject_is_not_a_clone(self):
        site_packages = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, site_packages, ignore_errors=True)
        self.assertFalse(paths.is_clone(site_packages))


class UserDataDirTest(unittest.TestCase):
    """Installed with pip install ., the files go to the user's local data folder."""

    def test_windows_uses_local_appdata_never_the_roaming_profile(self):
        env = {"APPDATA": r"C:\Users\x\AppData\Roaming",
               "LOCALAPPDATA": r"C:\Users\x\AppData\Local"}
        found = paths.user_data_dir("win32", env, HOME)
        self.assertEqual(found, Path(env["LOCALAPPDATA"]) / PROJECT_NAME)
        self.assertNotIn("Roaming", str(found))

    def test_windows_without_localappdata_falls_back_to_the_same_place(self):
        self.assertEqual(paths.user_data_dir("win32", {}, HOME),
                         HOME / "AppData" / "Local" / PROJECT_NAME)

    def test_macos(self):
        self.assertEqual(paths.user_data_dir("darwin", {}, HOME),
                         HOME / "Library" / "Application Support" / PROJECT_NAME)

    def test_linux_follows_xdg_data_home(self):
        self.assertEqual(paths.user_data_dir("linux", {"XDG_DATA_HOME": "/data"}, HOME),
                         Path("/data") / PROJECT_NAME)
        self.assertEqual(paths.user_data_dir("linux", {}, HOME),
                         HOME / ".local" / "share" / PROJECT_NAME)


class SaveSettingsTest(unittest.TestCase):
    """An installed copy's data folder may not exist yet when the settings are saved."""

    def test_saving_creates_the_folder_and_never_writes_a_password(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        target = root / "not-created-yet" / "db_config.json"
        settings = config.DbSettings("me", "steam", "localhost", 5433)
        with mock.patch("builtins.input", return_value="y"), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertLogs("get_data.config", "INFO"):
            config.offer_to_save(settings, {}, target)
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(saved["database"], "steam")
        self.assertNotIn("password", saved)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
