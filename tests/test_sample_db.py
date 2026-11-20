"""The sample games.json through the real SQL: loader, staging tables, core model, QA.

tests/data/games_sample.json holds nine made-up games that carry the problem cases of
the real dataset (README, "The data-quality story"); each game's "notes" says which.

- offline: the file passes the loader's sanity check and has the shapes the SQL handles
- in a database where get json, model and qa ran on the sample: the cleaned values, the
  links and the QA results are exactly as expected.  These tests only read, and they are
  skipped unless SAMPLE_DB_URL names that database.

The GitHub Actions workflow (.github/workflows/ci.yml) runs all of it on every push, in
a throwaway Postgres container.  By hand, against a database you can throw away:

    python main.py get json -l tests/data/games_sample.json -d <db> ...
    python main.py model -d <db> ...
    python main.py qa -d <db> ...
    SAMPLE_DB_URL=postgresql+psycopg://<user>:<password>@<host>:<port>/<db>
    python -m unittest -v tests.test_sample_db
"""

import datetime as dt
import json
import os
import re
import sys
import unittest
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# pylint: disable=wrong-import-position
from get_data import json_load
from get_data.sql_runner import autocommit, execute, read_statements
from qa_data import run_qa

SAMPLE = ROOT / "tests" / "data" / "games_sample.json"
SAMPLE_DB_URL = os.environ.get("SAMPLE_DB_URL")

# the warning checks that must fire on the sample, with their number of rows
EXPECTED_WARNINGS = {
    "game_name_missing": 1,                 # 1004
    "release_date_not_parsed": 1,           # 1008, 'Coming soon'
    "game_without_developer": 1,            # 1004
    "game_without_genre": 1,                # 1004
    "game_without_tags": 1,                 # 1004
    "tag_votes_missing": 2,                 # 1006, tags as a list of names
    "game_without_language": 1,             # 1004
    "no_platform": 1,                       # 1005
    "developer_name_is_company_suffix": 1,  # LIMITED
    "language_name_suspicious": 2,          # the two entries with a remark in brackets
}
# checks that need the CSV landing table, which the sample does not have
EXPECTED_SKIPS = {"csv_games_missing_from_json", "names_lost_by_csv_reader"}

LANGUAGES = """
    SELECT l.name, gl.full_audio
    FROM core.game_languages AS gl JOIN core.languages AS l USING (language_id)
    WHERE gl.appid = :appid"""
TAGS = """
    SELECT t.name, gt.position, gt.votes
    FROM core.game_tags AS gt JOIN core.tags AS t USING (tag_id)
    WHERE gt.appid = :appid ORDER BY gt.position"""


class SampleFileTest(unittest.TestCase):
    """The sample passes the loader's sanity check and has the shapes the SQL must handle."""

    def test_sanity_check_and_inventory(self):
        with self.assertLogs("get_data.json_load", "INFO"):  # keeps the output quiet
            inventory = json_load.scan(SAMPLE)
        self.assertEqual(inventory.games, 9)
        self.assertEqual(inventory.nul_bytes, 1)
        self.assertLessEqual({"discount", "required_age", "release_date", "score_rank",
                              "supported_languages", "tags"},
                             set(inventory.mixed_type_keys()))

    def test_every_game_says_what_it_is_for(self):
        games = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for appid, game in games.items():
            with self.subTest(appid=appid):
                self.assertTrue(game["notes"].startswith("Sample: "))


@unittest.skipUnless(SAMPLE_DB_URL, "set SAMPLE_DB_URL to a database where the pipeline "
                                    "ran on the sample")
class SampleDatabaseTest(unittest.TestCase):
    """After get json, model and qa ran on the sample, the database holds what it should."""

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(SAMPLE_DB_URL)
        cls.addClassCleanup(cls.engine.dispose)
        cls.conn = autocommit(cls.engine)  # one failing query cannot break the next test
        cls.addClassCleanup(cls.conn.close)

    def rows(self, sql: str, **params) -> list[tuple]:
        """All rows of a query, as plain tuples."""
        return [tuple(row) for row in self.conn.execute(text(sql), params)]

    def column(self, sql: str, **params) -> list:
        """The first column of every row of a query."""
        return [row[0] for row in self.rows(sql, **params)]

    def flagged(self, check: str, column: str) -> list:
        """One column of the rows a check in 02_checks.sql reports, sorted."""
        sql = next(s.sql for s in read_statements(run_qa.CHECKS_FILE)
                   if s.meta.get("check") == check)
        return sorted(row[column] for row in execute(self.conn, sql).mappings())

    def test_every_game_arrives_in_every_table(self):
        for table in ("public.games_json", "staging.games", "core.games"):
            with self.subTest(table=table):
                self.assertEqual(self.column(f"SELECT count(*) FROM {table}"), [9])

    def test_values_are_typed_and_disguised_gaps_become_null(self):
        games = {row[0]: row[1:] for row in self.rows(
            "SELECT appid, name, release_date, release_date_raw, price, discount, "
            "required_age, metacritic_score, owners_min, owners_max FROM core.games")}
        self.assertEqual(games[1001], ("Clean Reference", dt.date(2008, 10, 21), "Oct 21, 2008",
                                       Decimal("9.99"), 0, 0, 85, 20000, 50000))
        # strings converted, the nested release date read, metacritic 0 means no score
        self.assertEqual(games[1006][1:7], (dt.date(2008, 10, 21), "21 Oct, 2008",
                                            Decimal("4.99"), 50, 17, None))
        self.assertEqual(games[1007][1:3], (dt.date(2008, 10, 1), "Oct 2008"))
        self.assertEqual(games[1008][1:3], (None, "Coming soon"))
        self.assertEqual(games[1008][7:], (0, 0))
        self.assertEqual(games[1009][1:3], (None, None))
        self.assertIsNone(games[1004][0])

    def test_language_entries_are_cleaned(self):
        self.assertEqual(sorted(self.rows(LANGUAGES, appid=1002)), [
            ("Czech", False), ("English", True), ("German", False), ("Italian", False),
            ("Japanese", False), ("K'iche", False), ("Polish", False), ("Russian", True),
            ("Slovak", False), ("Spanish - Spain", False), ("Turkish", False)])

    def test_a_lone_language_string_is_split(self):
        self.assertEqual(sorted(self.rows(LANGUAGES, appid=1006)),
                         [("English", False), ("French", False)])

    def test_known_errors_are_kept_as_published(self):
        self.assertEqual(sorted(name for name, _ in self.rows(LANGUAGES, appid=1003)),
                         ["Japanese (all with full audio support)",
                          "Traditional Chinese (text only)"])
        self.assertEqual(self.column(
            "SELECT d.name FROM core.game_developers AS gd "
            "JOIN core.developers AS d USING (developer_id) WHERE gd.appid = 1003"),
            ["LIMITED"])

    def test_developer_spellings_merge_and_a_comma_does_not_split(self):
        games_per_developer = dict(self.rows(
            "SELECT d.name, count(*) FROM core.developers AS d "
            "JOIN core.game_developers AS gd USING (developer_id) GROUP BY d.name"))
        self.assertEqual(games_per_developer, {
            "1UP Games": 3, "CAPCOM Co., Ltd.": 1, "LIMITED": 1,
            "Northwind Studio": 2, "Quiet Owl": 1, "Shape Works": 1})

    def test_tags_are_ranked_by_votes(self):
        self.assertEqual(self.rows(TAGS, appid=1001),
                         [("Action", 1, 300), ("Casual", 2, 120), ("Indie", 3, 120)])
        self.assertEqual(self.rows(TAGS, appid=1006), [("Indie", 1, None), ("Puzzle", 2, None)])

    def test_core_reconciles_with_the_staging_tables(self):
        self.assertEqual(self.column("SELECT count(*) FROM core.game_tags"), [12])
        self.assertEqual(self.column("SELECT count(*) FROM staging.game_tags"), [12])
        self.assertEqual(self.column("SELECT count(*) FROM staging.game_package_subs"), [1])

    def test_nul_characters_are_stripped(self):
        self.assertEqual(self.column(
            "SELECT data->>'about_the_game' FROM public.games_json WHERE appid = 1006"),
            ["Hiddenbyte"])

    def test_every_constraint_is_validated(self):
        expected = set(re.findall(r"ADD CONSTRAINT (\w+)",
                                  run_qa.CONSTRAINTS_FILE.read_text(encoding="utf-8")))
        found = dict(self.rows(
            "SELECT conname, convalidated FROM pg_constraint "
            "WHERE connamespace = 'core'::regnamespace AND contype = 'c'"))
        self.assertEqual(set(found), expected)
        self.assertEqual(sorted(name for name, valid in found.items() if not valid), [])

    def test_checks_report_exactly_the_planted_problems(self):
        for result in run_qa.run_checks(self.conn):
            name = result["check"]
            with self.subTest(check=name):
                if name in EXPECTED_WARNINGS:
                    self.assertEqual((result["status"], result["failed"]),
                                     ("warn", EXPECTED_WARNINGS[name]))
                elif name in EXPECTED_SKIPS:
                    self.assertEqual(result["status"], "skip")
                else:
                    self.assertEqual(result["status"], "pass", result["detail"])

    def test_known_errors_are_flagged_by_name(self):
        self.assertEqual(self.flagged("language_name_suspicious", "name"),
                         ["Japanese (all with full audio support)",
                          "Traditional Chinese (text only)"])
        self.assertEqual(self.flagged("developer_name_is_company_suffix", "name"), ["LIMITED"])
        self.assertEqual(self.flagged("game_name_missing", "appid"), [1004])
        self.assertEqual(self.flagged("no_platform", "appid"), [1005])
        self.assertEqual(self.flagged("release_date_not_parsed", "appid"), [1008])


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
