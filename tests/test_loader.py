"""Unit tests of the loader pieces main.py relies on, without database or network.

- telling a Kaggle dataset from a local file, and choosing the file format
- the y/n-free prompts: ask_choice, and host/port defaults in resolve_settings
- read_csv keeps names such as 'None' and 'NA' as text (only empty cells are missing)

Run:  python main.py debug                 (every test in tests/)
      python tests/test_loader.py          (this file only)
"""

import argparse
import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# pylint: disable=wrong-import-position,protected-access
from get_data import cli  # noqa: E402
from get_data.config import resolve_settings  # noqa: E402
from get_data.prompts import ask_choice  # noqa: E402
from get_data.transform import read_csv  # noqa: E402

BOTH = [("games.csv", 401_000_000), ("games.json", 1_008_000_000)]
JSON_ONLY = [("games.json", 1_008_000_000)]


def typing(*answers):
    """Patch input() to type `answers` one by one, and silence what is printed."""
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch("builtins.input", side_effect=list(answers)))
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    return stack


class TempDirTest(unittest.TestCase):
    """Gives each test a temporary folder in self.tmp."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class ClassifySourceTest(TempDirTest):
    """classify_source tells a Kaggle dataset from a local file."""

    def test_owner_and_name_is_a_dataset(self):
        self.assertEqual(cli.classify_source("owner/name"), ("kaggle", "owner/name"))

    def test_dataset_url_is_a_dataset(self):
        url = "https://www.kaggle.com/datasets/fronkongames/steam-games-dataset"
        self.assertEqual(cli.classify_source(url),
                         ("kaggle", "fronkongames/steam-games-dataset"))

    def test_csv_or_json_name_is_a_file_even_in_quotes(self):
        self.assertEqual(cli.classify_source('"tmp/games.json"'),
                         ("local", Path("tmp/games.json")))

    def test_existing_file_without_extension_is_a_file(self):
        path = self.tmp / "export"
        path.write_text("x", encoding="utf-8")
        self.assertEqual(cli.classify_source(str(path)), ("local", path))

    def test_anything_else_is_rejected(self):
        with self.assertRaises(ValueError):
            cli.classify_source("not a dataset")


class ChooseKaggleFileTest(unittest.TestCase):
    """The file taken from a dataset: --file, else the asked format, else CSV, else JSON."""

    def choose(self, files, what=None, file=None):
        """Call the chooser with the given dataset listing and arguments."""
        return cli._choose_kaggle_file(files, argparse.Namespace(what=what, file=file))

    def test_csv_by_default(self):
        self.assertEqual(self.choose(BOTH), "games.csv")

    def test_json_when_asked(self):
        self.assertEqual(self.choose(BOTH, what="json"), "games.json")

    def test_json_when_the_dataset_has_no_csv(self):
        self.assertEqual(self.choose(JSON_ONLY), "games.json")

    def test_asking_for_a_missing_format_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            self.choose(JSON_ONLY, what="csv")

    def test_exact_file_wins_and_must_exist(self):
        self.assertEqual(self.choose(BOTH, what="csv", file="games.json"), "games.json")
        with self.assertRaises(FileNotFoundError):
            self.choose(BOTH, file="other.csv")


class StepGetFileTest(TempDirTest):
    """A local file must exist and match the asked format."""

    def args(self, path, what=None):
        """Arguments for a local file and an optional format."""
        return argparse.Namespace(local_file=path, kaggle=None, what=what)

    def test_matching_file_is_used(self):
        path = self.tmp / "games.json"
        path.write_text("{}", encoding="utf-8")
        self.assertEqual(cli.step_get_file(self.args(path, "json")), path)
        self.assertEqual(cli.step_get_file(self.args(path)), path)

    def test_format_mismatch_is_an_error(self):
        path = self.tmp / "games.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Asked for CSV"):
            cli.step_get_file(self.args(path, "csv"))

    def test_missing_file_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            cli.step_get_file(self.args(self.tmp / "nope.csv"))


class PackageCliTest(unittest.TestCase):
    """python -m get_data --json still means 'take the JSON file'."""

    def test_json_flag_sets_the_format(self):
        args = cli.build_parser().parse_args(["--json", "--dry-run"])
        with mock.patch.object(cli, "setup_logging"), \
                mock.patch.object(cli, "run") as run:
            self.assertEqual(cli.run_cli(args), 0)
        self.assertEqual(run.call_args.args[0].what, "json")


class AskChoiceTest(unittest.TestCase):
    """ask_choice accepts a full answer, a unique beginning, or Enter for the default."""

    PARTS = ["get", "model", "qa", "analyse", "debug"]

    def test_full_answer_and_unique_beginning(self):
        with typing("qa", "an"):
            self.assertEqual(ask_choice("Part", self.PARTS), "qa")
            self.assertEqual(ask_choice("Part", self.PARTS), "analyse")

    def test_unknown_answer_is_asked_again(self):
        with typing("zzz", "Model"):
            self.assertEqual(ask_choice("Part", self.PARTS), "model")

    def test_enter_takes_the_default(self):
        with typing(""):
            self.assertEqual(ask_choice("What", ["csv", "json"], default="csv"), "csv")


class ResolveSettingsTest(TempDirTest):
    """Connection settings: flag > saved file > prompt; host and port offer defaults."""

    def flags(self, **given):
        """Command-line flags, None unless given."""
        values = {"user": None, "database": None, "host": None, "port": None}
        return argparse.Namespace(**{**values, **given})

    def test_enter_keeps_the_tunnel_defaults(self):
        with typing("me", "steam", "", ""):
            settings, _ = resolve_settings(self.flags(), self.tmp / "none.json")
        self.assertEqual((settings.username, settings.database, settings.host,
                          settings.port), ("me", "steam", "localhost", 5433))

    def test_typed_host_and_port_are_used(self):
        with typing("me", "steam", "DebianServ", "5432"):
            settings, _ = resolve_settings(self.flags(), self.tmp / "none.json")
        self.assertEqual((settings.host, settings.port), ("DebianServ", 5432))

    def test_flags_beat_the_saved_file_and_nothing_is_asked(self):
        saved = self.tmp / "db_config.json"
        saved.write_text(json.dumps({"username": "saved", "database": "db",
                                     "host": "h", "port": 1}), encoding="utf-8")
        with typing():  # any question would raise StopIteration
            settings, _ = resolve_settings(self.flags(user="flag"), saved)
        self.assertEqual((settings.username, settings.database, settings.port),
                         ("flag", "db", 1))

    def test_a_port_that_is_not_a_number_is_an_error(self):
        with typing("me", "steam", "", "abc"), self.assertRaises(ValueError):
            resolve_settings(self.flags(), self.tmp / "none.json")


class ReadCsvTest(TempDirTest):
    """Only empty cells become missing; 'None', 'NA' and 'null' stay text."""

    CSV = "AppID,Name,Developers,Price\n1,None,NA,1.5\n2,NA,null,\n3,,Studio,2\n"

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "games.csv"
        self.path.write_text(self.CSV, encoding="utf-8")

    def check(self, df, name, developers, price):
        """Compare the three columns, treating NaN as missing."""
        self.assertEqual([None if isinstance(v, float) else v for v in df[name]],
                         ["None", "NA", None])
        self.assertEqual(df[developers].tolist(), ["NA", "null", "Studio"])
        self.assertEqual(df[price].iloc[0], 1.5)
        self.assertTrue(math.isnan(df[price].iloc[1]))

    def test_names_that_look_like_missing_values_survive(self):
        self.check(read_csv(self.path), "Name", "Developers", "Price")

    def test_same_with_replaced_header(self):
        df = read_csv(self.path, names=["appid", "name", "developers", "price"])
        self.check(df, "name", "developers", "price")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
