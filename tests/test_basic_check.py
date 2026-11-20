"""Unit tests of analyse_data/basic_check.py, no database needed.

- 'schema.table' and bare table names, comma lists, which tables show random rows
- the interactive questions, with the schema and table listings faked
- the generated profiling SQL: per-type statistics, quoting, no DISTINCT on jsonb
- value summaries, percentages that never round to 100 % or 0 %, compact numbers
- fitting long values on one line

Run:  python main.py debug                 (every test in tests/)
      python tests/test_basic_check.py     (this file only)
"""

import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from sqlalchemy.dialects import postgresql

from analyse_data import basic_check as bc

ROOT = Path(__file__).resolve().parent.parent

QUOTE = postgresql.dialect().identifier_preparer.quote
COLUMNS = [
    bc.Column("appid", "bigint"),
    bc.Column("name", "text"),
    bc.Column("windows", "boolean"),
    bc.Column("data", "jsonb"),
    bc.Column("release_date", "date"),
    bc.Column('odd "name"', "integer"),
]


class SplitTableTest(unittest.TestCase):
    """--table accepts 'schema.table' or a table of --schema."""

    def test_qualified_name(self):
        self.assertEqual(bc.split_table("public.games_json", "core"), ("public", "games_json"))

    def test_bare_name_belongs_to_the_schema(self):
        self.assertEqual(bc.split_table("games", "core"), ("core", "games"))


def args(**given):
    """Arguments as the parser would give them, overridden by `given`."""
    values = {"schema": "core", "table": None, "rows": 3, "sample_from": None,
              "interactive": True}
    return argparse.Namespace(**{**values, **given})


SCHEMAS = [("core", 9), ("public", 14)]
TABLES = [("game_tags", 1_180_708), ("games", 148_909), ("tags", None)]


class TargetsTest(unittest.TestCase):
    """Which tables are profiled, and which of them show random rows."""

    def test_comma_list_mixes_schemas_and_drops_repeats(self):
        pairs = bc.targets(None, args(table="games, public.games_json,games"))
        self.assertEqual(pairs, [("core", "games"), ("public", "games_json")])

    def test_every_table_of_the_schema_by_default(self):
        with mock.patch.object(bc, "list_tables", return_value=TABLES):
            pairs = bc.targets(None, args())
        self.assertEqual([t for _, t in pairs], ["game_tags", "games", "tags"])

    def test_empty_schema_is_an_error(self):
        with mock.patch.object(bc, "list_tables", return_value=[]), \
                self.assertRaises(RuntimeError):
            bc.targets(None, args(schema="nothing"))

    def test_sample_from_limits_the_random_rows(self):
        pairs = [("core", "games"), ("core", "tags")]
        self.assertEqual(bc.sample_set(args(), pairs), set(pairs))
        self.assertEqual(bc.sample_set(args(sample_from="core.tags"), pairs), {("core", "tags")})
        self.assertEqual(bc.sample_set(args(rows=0), pairs), set())

    def test_sample_table_that_is_not_profiled_is_warned_about(self):
        with self.assertLogs("analyse_data", "WARNING"):
            picked = bc.sample_set(args(sample_from="games,players"), [("core", "games")])
        self.assertEqual(picked, {("core", "games")})


class InteractiveTest(unittest.TestCase):
    """choose_interactively lists schemas and tables before each question."""

    def choose(self, *answers, **given):
        """Run the questions with faked listings and typed answers."""
        a = args(**given)
        out = io.StringIO()
        with mock.patch.object(bc, "list_schemas", return_value=SCHEMAS), \
                mock.patch.object(bc, "list_tables", return_value=TABLES), \
                mock.patch("builtins.input", side_effect=list(answers)), \
                contextlib.redirect_stdout(out):
            bc.choose_interactively(None, a)
        return a, out.getvalue()

    def test_lists_come_before_the_questions(self):
        _, printed = self.choose("", "", "", "")
        self.assertIn("1  core   9 tables", printed)
        self.assertIn("2  games   about 148.9k rows", printed)
        self.assertIn("3  tags   not analysed yet", printed)

    def test_enter_everywhere_means_all_tables_of_core_with_3_rows(self):
        a, _ = self.choose("", "", "", "")
        self.assertEqual((a.schema, a.table, a.rows, a.sample_from), ("core", None, 3, None))

    def test_some_tables_and_rows_from_only_one_of_them(self):
        a, printed = self.choose("public", "games, 3", "5", "tags")
        self.assertEqual(a.schema, "public")
        self.assertEqual(a.table, "public.games,public.tags")
        self.assertEqual((a.rows, a.sample_from), (5, "public.tags"))
        self.assertIn("Profiled tables:", printed)

    def test_none_switches_the_random_rows_off(self):
        a, _ = self.choose("1", "1,2", "", "none")
        self.assertEqual((a.rows, a.sample_from), (0, None))

    def test_one_table_needs_no_sample_question(self):
        a, _ = self.choose("1", "games", "2")  # a fourth question would raise StopIteration
        self.assertEqual((a.table, a.rows, a.sample_from), ("core.games", 2, None))


class ProfileSqlTest(unittest.TestCase):
    """One SELECT computes every statistic, with identifiers quoted."""

    @classmethod
    def setUpClass(cls):
        cls.sql = bc.profile_sql('"core"."games"', COLUMNS, QUOTE)

    def test_counts_for_every_column(self):
        self.assertTrue(self.sql.startswith("SELECT count(*) AS n, "))
        for i in range(len(COLUMNS)):
            self.assertIn(f"AS nn{i}", self.sql)
        self.assertTrue(self.sql.endswith('FROM "core"."games"'))

    def test_statistics_follow_the_type(self):
        self.assertIn('percentile_cont(0.5) WITHIN GROUP (ORDER BY appid::float8) AS md0',
                      self.sql)
        self.assertIn("max(length(name)) AS hi1", self.sql)
        self.assertIn("count(*) FILTER (WHERE windows) AS yes2", self.sql)
        self.assertIn("min(release_date)::text AS lo4", self.sql)

    def test_no_distinct_count_on_jsonb(self):
        self.assertIn("count(data) AS nn3", self.sql)
        self.assertNotIn("count(DISTINCT data)", self.sql)

    def test_awkward_names_are_quoted(self):
        self.assertIn('count(DISTINCT "odd ""name""") AS nd5', self.sql)


class DescribeTest(unittest.TestCase):
    """The value summary of each column type."""

    STATS: ClassVar[dict[str, float | None]] = {
        "n": 200,
        "nn0": 200, "nd0": 200, "lo0": 10.0, "hi0": 4_700_000.0, "av0": 1_900_000.0,
        "md0": 1_600_000.0,
        "nn1": 199, "nd1": 198, "lo1": 1, "hi1": 255,
        "nn2": 200, "nd2": 2, "yes2": 150,
        "nn3": 200,
        "nn4": 0, "nd4": 0, "lo4": None, "hi4": None,
        "nn5": 200, "nd5": 1, "lo5": 7.0, "hi5": 7.0, "av5": 7.0, "md5": 7.0,
    }

    def describe(self, i):
        """Summary of column i of COLUMNS."""
        return bc.describe(COLUMNS[i], self.STATS, i)

    def test_numeric(self):
        self.assertEqual(self.describe(0),
                         ("100 %", "200", "min 10, median 1.6M, mean 1.9M, max 4.7M"))

    def test_text_lengths(self):
        self.assertEqual(self.describe(1), ("99.5 %", "198", "length 1 to 255"))

    def test_boolean_shares_of_the_filled_rows(self):
        self.assertEqual(self.describe(2)[2], "75.0 % yes, 25.0 % no")

    def test_jsonb_has_no_distinct_count(self):
        self.assertEqual(self.describe(3), ("100 %", "-", ""))

    def test_empty_column_has_no_values(self):
        self.assertEqual(self.describe(4), ("0 %", "0", ""))


class FormattingTest(unittest.TestCase):
    """Numbers and text shaped for one terminal line."""

    def test_percent_never_rounds_to_the_extremes(self):
        self.assertEqual(bc.percent(148_842, 148_909), "99.9 %")  # 99.955 %
        self.assertEqual(bc.percent(1, 100_000), "0.1 %")
        self.assertEqual(bc.percent(5, 5), "100 %")
        self.assertEqual(bc.percent(0, 5), "0 %")
        self.assertEqual(bc.percent(1, 0), "-")

    def test_human(self):
        self.assertEqual(bc.human(1_234_567), "1.2M")
        self.assertEqual(bc.human(45_678), "45.7k")
        self.assertEqual(bc.human(3.14159), "3.14")
        self.assertEqual(bc.human(7.0), "7")
        self.assertEqual(bc.human(None), "")

    def test_fit_flattens_and_cuts(self):
        self.assertEqual(bc.fit("a\n  b", 10), "a b")
        self.assertEqual(bc.fit("x" * 20, 10), "xxxxxxx...")

    def test_format_value(self):
        self.assertEqual(bc.format_value(None), "<null>")
        self.assertEqual(bc.format_value({"tags": {"Indie": 3}}), '{"tags": {"Indie": 3}}')
        self.assertEqual(bc.format_value(12), "12")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
