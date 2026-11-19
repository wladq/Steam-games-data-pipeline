"""Unit tests of dashboard/export.py with a made-up snapshot, no database needed.

- queries.sql: every statement is a named, read-only SELECT
- the Markdown report: numbers, QA state, escaping, the Mermaid pie
- the chart page: the data is embedded once and cannot break out of its <script> block
- write_outputs creates the three files

Run:  python main.py debug                 (every test in tests/)
      python tests/test_dashboard.py       (this file only)
"""

import datetime as dt
import decimal
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# pylint: disable=wrong-import-position
from dashboard import export  # noqa: E402
from get_data.sql_runner import read_statements  # noqa: E402


def snapshot() -> dict:
    """A small snapshot shaped like the real one, with one tricky tag name."""
    checks = [
        {"check": "row_counts_match", "severity": "error", "status": "pass", "failed": 0,
         "about": "rows arrived", "detail": ""},
        {"check": "game_name_missing", "severity": "warn", "status": "warn", "failed": 1,
         "about": "games without a name", "detail": ""},
    ]
    constraints = [{"table_name": "core.games", "constraint_name": "ck_price", "valid": True}]
    return {
        "totals": [{"games": 148909, "developers": 92522, "tags": 452, "genres": 33,
                    "languages": 107, "free_games": 27000, "paid_games": 121909,
                    "reviews": 123456789, "first_release": "1997-06-30",
                    "last_release": "2026-11-01"}],
        "releases_per_year": [{"year": 2024, "games": 18000}, {"year": 2025, "games": 9000}],
        "price_bands": [{"band": "Free", "games": 27000}, {"band": "Under $5", "games": 50000}],
        "platforms": [{"platform": "Windows", "games": 148900, "percent": 100.0}],
        "top_tags": [{"tag": "Indie", "votes": 5000000, "games": 90000},
                     {"tag": "A|B </script><b>", "votes": 10, "games": 1}],
        "top_genres": [{"genre": "Indie", "games": 90000}],
        "top_languages": [{"language": "English", "games": 140000, "full_audio": 20000}],
        "review_concentration": [{"top_percent_of_games": p, "percent_of_reviews": float(p)}
                                 for p in range(1, 101)],
        "constraints": constraints,
        "checks": checks,
        "qa": export.qa_summary(constraints, checks),
        "generated": "2026-11-19",
        "project": "Steam-games-data-pipeline",
        "version": "0.6.0",
    }


class QueriesFileTest(unittest.TestCase):
    """queries.sql holds only named SELECT statements."""

    def test_every_statement_is_a_named_select(self):
        stmts = read_statements(export.QUERIES_FILE)
        names = [s.meta.get("query") for s in stmts]
        self.assertTrue(stmts)
        self.assertNotIn(None, names)
        self.assertEqual(len(names), len(set(names)))
        for s in stmts:
            with self.subTest(query=s.meta["query"]):
                self.assertRegex(s.sql, r"^(SELECT|WITH)\b")
                self.assertNotRegex(s.sql.upper(), r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b")

    def test_the_sections_the_report_needs_exist(self):
        names = {s.meta["query"] for s in read_statements(export.QUERIES_FILE)}
        needed = set(snapshot()) - {"checks", "qa", "generated", "project", "version"}
        self.assertLessEqual(needed, names)


class HelpersTest(unittest.TestCase):
    """Value conversion and the small text builders."""

    def test_jsonable(self):
        self.assertEqual(export.jsonable(decimal.Decimal("12")), 12)
        self.assertEqual(export.jsonable(decimal.Decimal("1.5")), 1.5)
        self.assertEqual(export.jsonable(dt.date(2026, 11, 19)), "2026-11-19")
        self.assertEqual(export.jsonable("x"), "x")

    def test_text_bar_is_proportional(self):
        self.assertEqual(export.text_bar(10, 10, width=8), "█" * 8)
        self.assertEqual(export.text_bar(5, 10, width=8), "█" * 4)
        self.assertEqual(export.text_bar(1, 1000, width=8), "█")  # tiny but visible
        self.assertEqual(export.text_bar(0, 10), "")

    def test_qa_summary(self):
        checks = [{"status": "pass"}, {"status": "warn"}, {"status": "FAIL"}]
        summary = export.qa_summary([{"valid": True}, {"valid": False}], checks)
        self.assertFalse(summary["ok"])
        self.assertEqual((summary["constraints"], summary["constraints_valid"]), (2, 1))
        self.assertEqual(summary["checks"]["warn"], 1)
        self.assertFalse(export.qa_summary([], [{"status": "pass"}])["ok"])


class MarkdownTest(unittest.TestCase):
    """The report shows the numbers and the QA state, and stays valid Markdown."""

    @classmethod
    def setUpClass(cls):
        cls.md = export.render_markdown(snapshot())

    def test_headline_numbers_and_qa_state(self):
        self.assertIn("| Games | 148,909 |", self.md)
        self.assertIn("✅ OK", self.md)
        self.assertIn("1 of 1 constraints valid", self.md)
        self.assertIn("1 passed, 1 warnings, 0 failed", self.md)

    def test_pipe_in_a_name_does_not_break_the_table(self):
        self.assertIn("A\\|B", self.md)

    def test_mermaid_pie_for_free_and_paid(self):
        self.assertRegex(self.md, r'```mermaid\npie showData\n.*\n    "Free" : 27000\n')

    def test_missing_constraints_are_explained(self):
        data = snapshot()
        data["constraints"] = []
        data["qa"] = export.qa_summary([], data["checks"])
        self.assertIn("run `python main.py qa`", export.render_markdown(data))


class HtmlTest(unittest.TestCase):
    """The chart page embeds the data once, safely, and it reads back unchanged."""

    @classmethod
    def setUpClass(cls):
        cls.data = snapshot()
        cls.html = export.render_html(cls.data, export.TEMPLATE_FILE.read_text(encoding="utf-8"))

    def test_placeholder_replaced_and_data_round_trips(self):
        self.assertNotIn(export.DATA_PLACEHOLDER, self.html)
        block = re.search(r'<script id="dashboard-data" type="application/json">(.*?)</script>',
                          self.html, re.S).group(1)
        self.assertEqual(json.loads(block), self.data)

    def test_a_name_cannot_close_the_script_block(self):
        self.assertNotIn("A|B </script>", self.html)
        self.assertEqual(self.html.count("</script>"), 3)  # Plotly, data block, page code

    def test_template_without_placeholder_is_an_error(self):
        with self.assertRaises(ValueError):
            export.render_html(self.data, "<html></html>")


class WriteOutputsTest(unittest.TestCase):
    """write_outputs creates report.md, index.html and data/dashboard.json."""

    def test_three_files_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = export.write_outputs(snapshot(), Path(tmp) / "docs")
            names = sorted(p.relative_to(Path(tmp) / "docs").as_posix() for p in paths)
            self.assertEqual(names, ["data/dashboard.json", "index.html", "report.md"])
            for path in paths:
                self.assertGreater(path.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
