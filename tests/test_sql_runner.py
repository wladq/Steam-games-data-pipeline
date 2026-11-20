"""Unit tests of get_data/sql_runner.py, the frame every step runs in.

- guarded: the step's own exit code, 130 on Ctrl+C, 1 on an error with a hint
- run_step: logging set up, the engine handed to the work and always disposed
- open_engine: offers to save the settings only when asked, disposes on failure
- autocommit and the .sql statement splitter

Run:  python main.py debug                 (every test in tests/)
      python tests/test_sql_runner.py      (this file only)
"""

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

from get_data import db, sql_runner
from get_data.config import DbSettings

ROOT = Path(__file__).resolve().parent.parent

LOGGER = "get_data.sql_runner"


def args(**given):
    """Arguments as add_common_args would give them."""
    values = {"user": None, "database": None, "host": None, "port": None, "yes": False,
              "verbose": False, "log_dir": Path("logs")}
    return argparse.Namespace(**{**values, **given})


class GuardedTest(unittest.TestCase):
    """guarded turns the outcome of any step into an exit code."""

    def test_the_steps_own_code_and_none_as_zero(self):
        self.assertEqual(sql_runner.guarded(lambda: 1), 1)
        self.assertEqual(sql_runner.guarded(lambda: None), 0)

    def test_ctrl_c_gives_130(self):
        def interrupted():
            raise KeyboardInterrupt

        with self.assertLogs(LOGGER, "ERROR") as logs:
            self.assertEqual(sql_runner.guarded(interrupted), 130)
        self.assertIn("Interrupted", logs.output[0])

    def test_an_error_gives_1_with_its_message_and_where_the_details_are(self):
        def broken():
            raise RuntimeError("core.games does not exist")

        with self.assertLogs(LOGGER, "DEBUG") as logs:
            self.assertEqual(sql_runner.guarded(broken, log_dir=Path("logs")), 1)
        text = "\n".join(logs.output)
        self.assertIn("core.games does not exist", text)
        self.assertIn("Traceback", text)
        self.assertIn("debug.log", text)


class RunStepTest(unittest.TestCase):
    """run_step: start, connect, work(engine), dispose, exit code."""

    def setUp(self):
        self.engine = mock.Mock(name="engine")
        patches = [mock.patch.object(sql_runner, "start"),
                   mock.patch.object(sql_runner, "open_engine", return_value=self.engine)]
        self.start, self.open_engine = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def test_work_gets_the_engine_and_its_code_is_returned(self):
        seen = []
        code = sql_runner.run_step(args(), lambda engine: seen.append(engine) or 3)
        self.assertEqual((code, seen), (3, [self.engine]))
        self.start.assert_called_once()
        self.engine.dispose.assert_called_once()

    def test_the_engine_is_disposed_even_when_the_work_fails(self):
        def broken(_engine):
            raise ValueError("bad query")

        with self.assertLogs(LOGGER, "ERROR"):
            self.assertEqual(sql_runner.run_step(args(), broken), 1)
        self.engine.dispose.assert_called_once()

    def test_a_failed_connection_is_an_error_too(self):
        self.open_engine.side_effect = RuntimeError("Nothing answers at localhost:5433")
        with self.assertLogs(LOGGER, "ERROR"):
            self.assertEqual(sql_runner.run_step(args(), lambda engine: 0), 1)


class OpenEngineTest(unittest.TestCase):
    """open_engine offers to save settings only for the loader."""

    def setUp(self):
        self.engine = mock.Mock(name="engine")
        settings = DbSettings(username="me", database="steam")
        patches = {
            "settings": mock.patch.object(sql_runner, "resolve_settings",
                                          return_value=(settings, {})),
            "connect": mock.patch.object(db, "connect_engine",
                                         return_value=(self.engine, "PostgreSQL 17")),
            "save": mock.patch.object(sql_runner, "offer_to_save"),
        }
        self.mocks = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)

    def test_no_save_offer_by_default(self):
        with self.assertLogs(LOGGER, "INFO"):
            self.assertIs(sql_runner.open_engine(args()), self.engine)
        self.mocks["save"].assert_not_called()

    def test_save_offer_for_the_loader(self):
        with self.assertLogs(LOGGER, "INFO"):
            sql_runner.open_engine(args(), offer_save=True)
        self.mocks["save"].assert_called_once()

    def test_engine_is_disposed_when_the_save_offer_fails(self):
        self.mocks["save"].side_effect = KeyboardInterrupt
        with self.assertLogs(LOGGER, "INFO"), self.assertRaises(KeyboardInterrupt):
            sql_runner.open_engine(args(), offer_save=True)
        self.engine.dispose.assert_called_once()


class AutocommitTest(unittest.TestCase):
    """autocommit asks for a connection in which every statement stands alone."""

    def test_isolation_level(self):
        engine = mock.Mock()
        sql_runner.autocommit(engine)
        engine.connect.return_value.execution_options.assert_called_once_with(
            isolation_level="AUTOCOMMIT")


class SplitStatementsTest(unittest.TestCase):
    """The .sql splitter cuts at ';' only outside quotes, $$ bodies and comments."""

    def test_semicolons_inside_quotes_bodies_and_comments_do_not_split(self):
        sql = ("SELECT 'a;b';\n"
               "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1; $$;\n"
               "SELECT 2 -- not here; really\n;")
        stmts = sql_runner.split_statements(sql)
        self.assertEqual(len(stmts), 3)
        self.assertEqual(stmts[0].sql, "SELECT 'a;b'")
        self.assertTrue(stmts[1].sql.endswith("$$ SELECT 1; $$"))

    def test_header_comments_become_meta_and_a_blank_line_ends_a_file_header(self):
        sql = ("-- file header: not meta\n\n"
               "-- check: price_negative\n-- severity: error\nSELECT 1;")
        stmt = sql_runner.split_statements(sql)[0]
        self.assertEqual(stmt.meta, {"check": "price_negative", "severity": "error"})
        self.assertEqual(stmt.sql, "SELECT 1")


class RunAsScriptTest(unittest.TestCase):
    """run_as_script parses the step's own flags and returns run's exit code."""

    def test_flags_are_parsed_and_the_exit_code_returned(self):
        def add_arguments(parser):
            parser.add_argument("--checks-only", action="store_true")

        seen = []

        def run(parsed):
            seen.append(parsed)
            return 7

        code = sql_runner.run_as_script("Doc line.\n\nMore.", add_arguments, run,
                                        ["--checks-only"])
        self.assertEqual(code, 7)
        self.assertTrue(seen[0].checks_only)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
