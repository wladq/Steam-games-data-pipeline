"""Unit tests of qa_data/run_qa.py with a fake database, no connection needed.

- a violated constraint is recognised by SQLSTATE 23514, whatever language the
  server writes its messages in; other errors stay ERROR
- checks: comma-separated 'requires', skip when a table is missing, FAIL for an
  error-severity check that finds rows

Run:  python main.py debug                 (every test in tests/)
      python tests/test_run_qa.py          (this file only)
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# pylint: disable=wrong-import-position
from qa_data import run_qa  # noqa: E402

GERMAN = ('Check-Constraint »ck_games_price_not_negative« in Relation »games« '
          'wird von irgendeiner Zeile verletzt')


class DbError(Exception):
    """Stands in for a psycopg error: a message plus its SQLSTATE."""

    def __init__(self, sqlstate, message):
        super().__init__(message)
        self.sqlstate = sqlstate


class Wrapped(Exception):
    """Stands in for SQLAlchemy's wrapper, which keeps the driver error in .orig."""

    def __init__(self, orig):
        super().__init__(f"({type(orig).__name__}) {orig}\n[SQL: ...]")
        self.orig = orig


class Result:
    """The two ways run_qa reads a result: scalar_one() and mappings().all()."""

    def __init__(self, value=None, rows=()):
        self.value, self.rows = value, list(rows)

    def scalar_one(self):
        """The single value."""
        return self.value

    def mappings(self):
        """Rows as dicts (they already are)."""
        return self

    def all(self):
        """Every row."""
        return self.rows


class SqlstateTest(unittest.TestCase):
    """sqlstate() finds the code inside SQLAlchemy's wrapper or on the error itself."""

    def test_wrapped_and_bare_errors(self):
        self.assertEqual(run_qa.sqlstate(Wrapped(DbError("23514", "x"))), "23514")
        self.assertEqual(run_qa.sqlstate(DbError("42P01", "x")), "42P01")
        self.assertIsNone(run_qa.sqlstate(ValueError("violated")))


class ConstraintsTest(unittest.TestCase):
    """run_constraints sorts each constraint into valid, VIOLATED or ERROR."""

    @staticmethod
    def fake_execute(_conn, sql):
        """Fail two VALIDATE statements the way the server would."""
        if "VALIDATE CONSTRAINT ck_games_price_not_negative" in sql:
            raise Wrapped(DbError("23514", GERMAN))
        if "VALIDATE CONSTRAINT ck_games_discount_is_percent" in sql:
            raise Wrapped(DbError("42501", "permission denied for table games"))
        if "VALIDATE CONSTRAINT ck_games_metacritic_range" in sql:
            raise ValueError("check constraint is violated")  # English text, but no SQLSTATE
        return Result()

    @classmethod
    def setUpClass(cls):
        with mock.patch.object(run_qa, "execute", side_effect=cls.fake_execute):
            cls.results = run_qa.run_constraints(conn=None)

    def test_violation_is_found_by_code_in_any_language(self):
        status, detail = self.results["ck_games_price_not_negative"]
        self.assertEqual(status, "VIOLATED")
        self.assertIn("verletzt", detail)

    def test_other_database_errors_stay_errors(self):
        self.assertEqual(self.results["ck_games_discount_is_percent"][0], "ERROR")

    def test_message_text_alone_is_not_trusted(self):
        self.assertEqual(self.results["ck_games_metacritic_range"][0], "ERROR")

    def test_the_rest_are_valid(self):
        valid = [name for name, (status, _) in self.results.items() if status == "valid"]
        self.assertEqual(len(valid), len(self.results) - 3)
        self.assertEqual(len(self.results), 14)


class ChecksTest(unittest.TestCase):
    """run_checks skips checks whose tables are missing and fails error-level findings."""

    @staticmethod
    def fake_execute(_conn, sql):
        """public.games_raw is missing; price_negative finds two rows; the rest pass."""
        if sql.startswith("SELECT to_regclass("):
            return Result("public.games_raw" not in sql)
        if "price < 0" in sql:
            return Result(2, rows=[{"appid": 1, "price": -1}])
        return Result(0)

    @classmethod
    def setUpClass(cls):
        with mock.patch.object(run_qa, "execute", side_effect=cls.fake_execute):
            cls.results = {c["check"]: c for c in run_qa.run_checks(conn=None)}

    def test_check_needing_a_missing_table_is_skipped(self):
        for name in ("csv_games_missing_from_json", "names_lost_by_csv_reader"):
            with self.subTest(check=name):
                self.assertEqual(self.results[name]["status"], "skip")
                self.assertEqual(self.results[name]["detail"], "public.games_raw does not exist")

    def test_error_check_with_rows_fails(self):
        self.assertEqual((self.results["price_negative"]["status"],
                          self.results["price_negative"]["failed"]), ("FAIL", 2))

    def test_report_turns_a_failure_into_a_bad_exit(self):
        with self.assertLogs("qa_data", "INFO"):
            self.assertFalse(run_qa.report(None, list(self.results.values())))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
