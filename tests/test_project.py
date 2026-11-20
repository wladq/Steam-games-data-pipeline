"""Unit tests of the project metadata: pyproject.toml agrees with the code.

- the name matches PROJECT_NAME, and the version is read from get_data/__init__.py
- the dependencies match requirements.txt, each with an upper bound
- every folder listed for the wheel exists, and the package cannot reach PyPI
- the licence is GPL-3.0-only and LICENSE holds its text

Run:  python main.py debug                 (every test in tests/)
      python tests/test_project.py         (this file only)
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# pylint: disable=wrong-import-position
from get_data import PROJECT_NAME, __version__

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10
    tomllib = None


def normalised(name: str) -> str:
    """A distribution name compared the way pip compares it (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement(line: str) -> str:
    """'pandas >= 2.2  # note' -> 'pandas>=2.2'."""
    return line.split("#")[0].replace(" ", "").strip()


@unittest.skipIf(tomllib is None, "tomllib needs Python 3.11 or newer")
class PyprojectTest(unittest.TestCase):
    """pyproject.toml describes the code that is actually here."""

    @classmethod
    def setUpClass(cls):
        cls.data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        cls.project = cls.data["project"]

    def test_build_system_is_present(self):
        self.assertIn("hatchling.build", self.data["build-system"]["build-backend"])

    def test_name_matches_the_code(self):
        self.assertEqual(normalised(self.project["name"]), normalised(PROJECT_NAME))

    def test_version_is_read_from_the_package(self):
        self.assertIn("version", self.project["dynamic"])
        self.assertNotIn("version", self.project)
        path = ROOT / self.data["tool"]["hatch"]["version"]["path"]
        found = re.search(r'^__version__ *= *"([^"]+)"', path.read_text(encoding="utf-8"),
                          re.MULTILINE)
        self.assertEqual(found.group(1), __version__)

    def test_dependencies_match_requirements_txt(self):
        lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        listed = {requirement(line) for line in lines if requirement(line)}
        self.assertEqual({requirement(d) for d in self.project["dependencies"]}, listed)

    def test_every_dependency_stops_before_the_next_major_version(self):
        for dependency in self.project["dependencies"]:
            with self.subTest(dependency=dependency):
                self.assertRegex(requirement(dependency), r">=[\d.]+,<\d+$")

    def test_wheel_folders_exist(self):
        for folder in self.data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]:
            with self.subTest(folder=folder):
                self.assertTrue((ROOT / folder).is_dir())

    def test_cannot_be_uploaded_to_pypi(self):
        self.assertIn("Private :: Do Not Upload", self.project["classifiers"])

    def test_readme_exists(self):
        self.assertTrue((ROOT / self.project["readme"]).is_file())

    def test_licence_is_gplv3_and_its_text_is_shipped(self):
        self.assertEqual(self.project["license"], "GPL-3.0-only")
        for name in self.project["license-files"]:
            with self.subTest(file=name):
                head = (ROOT / name).read_text(encoding="utf-8")[:200]
                self.assertIn("GNU GENERAL PUBLIC LICENSE", head)
                self.assertIn("Version 3, 29 June 2007", head)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
