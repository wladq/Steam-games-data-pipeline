"""Unit tests of the loader pieces main.py relies on, without database or network.

- telling a Kaggle dataset from a local file, and choosing the file format
- the y/n-free prompts: ask_choice, and host/port defaults in resolve_settings
- connecting: up to three password attempts, none when the host does not answer
- read_csv keeps names such as 'None' and 'NA' as text (only empty cells are missing)

Run:  python main.py debug                 (every test in tests/)
      python tests/test_loader.py          (this file only)
"""

import argparse
import contextlib
import io
import json
import math
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# pylint: disable=wrong-import-position,protected-access
from sqlalchemy.exc import OperationalError  # noqa: E402

from get_data import cli, db, json_load  # noqa: E402
from get_data.config import DbSettings, resolve_settings  # noqa: E402
from get_data.prompts import ask_choice, ask_int, ask_selection  # noqa: E402
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
        with mock.patch.object(cli, "start"), \
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


class AskSelectionTest(unittest.TestCase):
    """ask_selection lists the options numbered and accepts names, numbers and shortcuts."""

    OPTIONS = ["games", "game_tags", "tags"]

    def ask(self, *answers, **kwargs):
        """Run ask_selection with typed answers; return (result, printed text)."""
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=list(answers)), \
                contextlib.redirect_stdout(out):
            result = ask_selection("Tables", self.OPTIONS, **kwargs)
        return result, out.getvalue()

    def test_options_are_listed_with_numbers_and_notes(self):
        _, printed = self.ask("1", notes={"games": "about 148.9k rows"})
        self.assertIn("1  games   about 148.9k rows", printed)
        self.assertIn("3  tags", printed)

    def test_names_and_numbers_mixed_in_a_comma_list(self):
        result, _ = self.ask("3, GAMES ,3", many=True)
        self.assertEqual(result, ["tags", "games"])

    def test_shortcut_and_default(self):
        result, _ = self.ask("", default="all", many=True,
                             shortcuts={"all": self.OPTIONS, "none": []})
        self.assertEqual(result, self.OPTIONS)
        result, _ = self.ask("none", many=True, shortcuts={"none": []})
        self.assertEqual(result, [])

    def test_unknown_items_and_too_many_are_asked_again(self):
        result, printed = self.ask("players", "1,2", "2")
        self.assertEqual(result, ["game_tags"])
        self.assertIn("Not in the list: players", printed)
        self.assertIn("only one", printed)

    def test_ask_int_repeats_until_a_whole_number(self):
        with typing("x", ""):
            self.assertEqual(ask_int("Rows", default=3), 3)


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


class StagingSchemaTest(unittest.TestCase):
    """The JSON loader's wide tables live in schema staging, never in public."""

    @classmethod
    def setUpClass(cls):
        cls.stmts = json_load.normalise_statements('"games_json"')
        cls.sql = "\n".join(cls.stmts)

    def test_schema_is_created_first(self):
        self.assertEqual(self.stmts[0], "CREATE SCHEMA IF NOT EXISTS staging")

    def test_every_table_statement_targets_staging(self):
        for stmt in self.stmts[1:]:
            with self.subTest(stmt=stmt[:60]):
                target = re.match(r"(?:CREATE TABLE|DROP TABLE IF EXISTS|ALTER TABLE|"
                                  r"CREATE INDEX ON) (\S+)", stmt).group(1)
                self.assertTrue(target.startswith("staging."))

    def test_foreign_keys_point_into_staging(self):
        refs = re.findall(r"REFERENCES (\S+?)\(", self.sql)
        self.assertTrue(refs)
        self.assertTrue(all(ref.startswith("staging.") for ref in refs), refs)

    def test_every_table_is_built_and_nothing_goes_to_public(self):
        built = set(re.findall(r"CREATE TABLE staging\.(\w+)", self.sql))
        self.assertEqual(built, set(json_load.NORMALISED_TABLES))
        self.assertNotIn("public.", self.sql)


class LegacyCopiesTest(unittest.TestCase):
    """After building staging, the old copies in public are offered for removal."""

    def run_step(self, legacy, *answers):
        """Run step_normalise with the database calls mocked; return the drop mock."""
        with mock.patch.object(cli.json_load, "normalise") as normalise, \
                mock.patch.object(cli.json_load, "legacy_tables", return_value=legacy), \
                mock.patch.object(cli.json_load, "drop_legacy") as drop, typing(*answers):
            cli.step_normalise(engine=None, landing_table="games_json")
        normalise.assert_called_once()
        return drop

    def test_yes_drops_the_old_copies(self):
        drop = self.run_step(["games", "game_tags"], "y", "y")
        drop.assert_called_once_with(None, ["games", "game_tags"])

    def test_enter_keeps_them(self):
        self.run_step(["games"], "y", "").assert_not_called()

    def test_no_question_without_old_copies(self):
        self.run_step([], "y").assert_not_called()  # a second question would raise StopIteration


def login_error() -> OperationalError:
    """A failed login as psycopg reports it: text only, the reason on the last line."""
    message = ('connection failed: connection to server at "127.0.0.1", port 5433 failed: '
               'could not initiate GSSAPI security context\n'
               'connection to server at "127.0.0.1", port 5433 failed: '
               'FATAL:  password authentication failed for user "me"')
    return OperationalError("connect", {}, Exception(message))


class ConnectEngineTest(unittest.TestCase):
    """connect_engine: up to three password attempts, but only when they can help."""

    SETTINGS = DbSettings(username="me", database="steam")

    def setUp(self):
        self.engines = []

        def make_engine(_settings, password):
            engine = mock.Mock(name=f"engine-{password}")
            self.engines.append(engine)
            return engine

        patches = {
            "reach": mock.patch.object(db.socket, "create_connection"),
            "password": mock.patch.object(db, "get_password", side_effect=["a", "b", "c"]),
            "engine": mock.patch.object(db, "make_engine", side_effect=make_engine),
            "test": mock.patch.object(db, "test_connection"),
            "env": mock.patch.dict(os.environ),
        }
        self.mocks = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        os.environ.pop("PGPASSWORD", None)

    def test_the_third_password_can_still_work(self):
        self.mocks["test"].side_effect = [login_error(), login_error(), "PostgreSQL 17.11"]
        with self.assertLogs("get_data.db", "WARNING") as logs:
            engine, version = db.connect_engine(self.SETTINGS)
        self.assertEqual(version, "PostgreSQL 17.11")
        self.assertIs(engine, self.engines[2])
        self.assertEqual(self.mocks["password"].call_count, 3)
        self.assertIn("attempt 1 of 3", logs.output[0])
        self.assertIn("password authentication failed", logs.output[0])
        for failed in self.engines[:2]:
            failed.dispose.assert_called_once()

    def test_three_failed_logins_give_up_with_the_server_reason(self):
        self.mocks["test"].side_effect = [login_error()] * 3
        with self.assertLogs("get_data.db", "WARNING"), \
                self.assertRaisesRegex(RuntimeError, "after 3 attempts: .*password authentication"):
            db.connect_engine(self.SETTINGS)
        self.assertEqual(self.mocks["password"].call_count, 3)

    def test_pgpassword_means_a_single_attempt(self):
        os.environ["PGPASSWORD"] = "from-env"
        self.mocks["test"].side_effect = [login_error()]
        with self.assertRaisesRegex(RuntimeError, "after 1 attempt: "):
            db.connect_engine(self.SETTINGS)
        self.assertEqual(self.mocks["password"].call_count, 1)

    def test_no_password_is_asked_when_nothing_answers(self):
        self.mocks["reach"].side_effect = ConnectionRefusedError("refused")
        with self.assertRaisesRegex(RuntimeError, "Nothing answers at localhost:5433"):
            db.connect_engine(self.SETTINGS)
        self.mocks["password"].assert_not_called()

    def test_errors_other_than_a_failed_connection_are_not_retried(self):
        self.mocks["test"].side_effect = ValueError("bug")
        with self.assertRaises(ValueError):
            db.connect_engine(self.SETTINGS)
        self.assertEqual(self.mocks["password"].call_count, 1)


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
