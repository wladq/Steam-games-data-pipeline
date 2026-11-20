"""Offline tests of the SQL side, no database needed.

- every .sql file splits into balanced statements, and every check has its headers
- the language cleaning of core.split_languages, mirrored in Python, cleans the real
  problem entries found in games.json
- the QA rules flag the known errors that are left in the data on purpose, and do not
  flag clean names; their patterns are read from qa_data/02_checks.sql itself

Run:  python main.py debug                 (every test in tests/)
      python tests/test_cleaning.py        (this file only)
      python -m unittest discover tests    (every test, without main.py)

The Python copy of the cleaning only approximates PostgreSQL's regular expressions;
the real test runs the SQL on tests/data/games_sample.json: tests/test_sample_db.py,
which the GitHub Actions workflow runs on every push.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# pylint: disable=wrong-import-position
from get_data.sql_runner import read_statements

SQL_FILES = (
    "model_data/01_schema.sql",
    "model_data/02_populate.sql",
    "qa_data/01_constraints.sql",
    "qa_data/02_checks.sql",
)
CHECKS_FILE = ROOT / "qa_data" / "02_checks.sql"

# (one entry of a language list as games.json has it, expected clean names)
LANGUAGE_CASES = [
    (("English&amp;amp;amp;amp;lt;strong&amp;amp;amp;amp;gt;"
      "&amp;amp;amp;amp;lt;/strong&amp;amp;amp;amp;gt;"), ["English"]),
    ("English\r\nRussian\r\nSpanish - Spain\r\nJapanese\r\nCzech",
     ["Czech", "English", "Japanese", "Russian", "Spanish - Spain"]),
    ("Italian \r\n\r\n[b][/b] ", ["Italian"]),
    ("English[b][/b]", ["English"]),
    ("English.", ["English"]),
    ("German;", ["German"]),
    ("#lang_slovakian", ["Slovak"]),
    ("Spanish - Latin America", ["Spanish - Latin America"]),
]

# Errors left in the data on purpose: the cleaning keeps them, the QA rules must flag
# them.  When the cleaning learns to fix one, move it to the cases above.
KNOWN_LANGUAGE_ERRORS = ["Japanese (all with full audio support)",
                         "Traditional Chinese (text only)"]  # flagged for its length
KNOWN_DEVELOPER_ERRORS = ["LIMITED"]  # the JSON lists it as a game's only developer

CLEAN_LANGUAGES = ["English", "Spanish - Spain", "Traditional Chinese", "K'iche",
                   "Portuguese - Brazil"]
CLEAN_DEVELOPERS = ["Limited Run Games", "CAPCOM Co., Ltd.", "Valve", "Inc Studios"]


def split_languages(entry: str) -> list[str]:
    """Mirror of core.split_languages in model_data/01_schema.sql."""
    t = re.sub(r"&(amp;)+", "&", entry).replace("&lt;", "<").replace("&gt;", ">")
    t = re.sub(r"&#?[a-z0-9]+;", " ", t, flags=re.IGNORECASE)
    out = set()
    for part in re.split(r",|;|[\r\n]+|(?:\\[rn])+|<br\s*/?>", t, flags=re.IGNORECASE):
        lang = re.sub(r"\\[rnt]|<[^>]*>|\[/?[a-z]+\]|[*#]", " ", part, flags=re.IGNORECASE)
        lang = re.sub(r"\s+", " ", lang).strip(" '\".")
        if lang:
            out.add("Slovak" if lang == "lang_slovakian" else lang)
    return sorted(out)


def check_sql(name: str) -> str:
    """The SQL of one check in 02_checks.sql, found by its '-- check:' name."""
    for stmt in read_statements(CHECKS_FILE):
        if stmt.meta.get("check") == name:
            return stmt.sql
    raise LookupError(f"no check named {name} in {CHECKS_FILE.name}")


def sql_pattern(sql: str, operator: str) -> str:
    """The quoted regular expression after `operator` (e.g. '!~'), with '' unescaped."""
    match = re.search(re.escape(operator) + r"\s*'((?:[^']|'')*)'", sql)
    if not match:
        raise LookupError(f"no {operator} pattern in: {sql[:80]}")
    return match.group(1).replace("''", "'")


class SqlFilesTest(unittest.TestCase):
    """The SQL files are well formed for the statement splitter and the QA runner."""

    def test_files_split_into_balanced_statements(self):
        for rel in SQL_FILES:
            with self.subTest(file=rel):
                stmts = read_statements(ROOT / rel)
                self.assertTrue(stmts)
                for s in stmts:
                    self.assertEqual(s.sql.count("'") % 2, 0, f"quotes, line {s.line}")
                    self.assertEqual(s.sql.count("("), s.sql.count(")"),
                                     f"parentheses, line {s.line}")

    def test_every_check_has_unique_name_and_headers(self):
        stmts = read_statements(CHECKS_FILE)
        names = [s.meta.get("check") for s in stmts]
        self.assertEqual(len(names), len(set(names)), "duplicate check names")
        for s in stmts:
            with self.subTest(line=s.line):
                self.assertLessEqual({"check", "severity", "about"}, set(s.meta))
                self.assertIn(s.meta["severity"], ("error", "warn"))


class LanguageCleaningTest(unittest.TestCase):
    """The Python copy of core.split_languages cleans the real problem entries."""

    def test_problem_entries_are_cleaned(self):
        for raw, expected in LANGUAGE_CASES:
            with self.subTest(raw=raw[:40]):
                self.assertEqual(split_languages(raw), expected)

    def test_known_errors_survive_cleaning(self):
        for raw in KNOWN_LANGUAGE_ERRORS:
            with self.subTest(raw=raw):
                self.assertEqual(split_languages(raw), [raw])


class QaRulesTest(unittest.TestCase):
    """The rules in 02_checks.sql flag the known errors and leave clean names alone."""

    @classmethod
    def setUpClass(cls):
        sql = check_sql("language_name_suspicious")
        cls.language_ok = re.compile(sql_pattern(sql, "!~"))
        cls.max_length = int(re.search(r"length\(l\.name\)\s*>\s*(\d+)", sql).group(1))
        sql = check_sql("developer_name_is_company_suffix")
        cls.suffix_only = re.compile(sql_pattern(sql, "~*"), re.IGNORECASE)

    def language_flagged(self, name: str) -> bool:
        """Same logic as the WHERE clause of language_name_suspicious."""
        return not self.language_ok.search(name) or len(name) > self.max_length

    def test_language_rule_flags_known_errors(self):
        for name in KNOWN_LANGUAGE_ERRORS + ["lang_slovakian", "English amp;lt;strong"]:
            with self.subTest(name=name):
                self.assertTrue(self.language_flagged(name))

    def test_language_rule_passes_clean_names(self):
        for name in CLEAN_LANGUAGES:
            with self.subTest(name=name):
                self.assertFalse(self.language_flagged(name))

    def test_developer_rule_flags_known_errors(self):
        for name in KNOWN_DEVELOPER_ERRORS + ["inc.", "Ltd", "GmbH"]:
            with self.subTest(name=name):
                self.assertTrue(self.suffix_only.search(name))

    def test_developer_rule_passes_clean_names(self):
        for name in CLEAN_DEVELOPERS:
            with self.subTest(name=name):
                self.assertFalse(self.suffix_only.search(name))


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
