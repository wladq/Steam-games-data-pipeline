"""Offline check, no database needed: the SQL files still split into statements, and the
cleaning regexes of core.split_languages and core.split_developers, mirrored in Python,
clean the real problem strings found in the Kaggle CSV.

Run:  python main.py debug            (every test in tests/)
      python tests/check_cleaning.py  (this file only)

Exit code 0 when every case passes, 1 otherwise.  The Python copies only approximate
PostgreSQL's regular expressions; the real test is `python main.py model` followed by
`python main.py qa`.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# pylint: disable=wrong-import-position
from get_data.sql_runner import read_statements  # noqa: E402

SQL_FILES = (
    "model_data/01_schema.sql",
    "model_data/02_populate.sql",
    "qa_data/01_constraints.sql",
    "qa_data/02_checks.sql",
)
SUFFIX = (
    r"(inc|incorporated|ltd|limited|llc|l\.l\.c|co|corp|corporation|gmbh"
    r"|s\.a|s\.r\.o|srl|pty|plc)"
)

# (raw text as it appears in games.csv, expected result)
LANGUAGE_CASES = [
    (
        "['English&amp;amp;amp;amp;lt;strong&amp;amp;amp;amp;gt;"
        "&amp;amp;amp;amp;lt;/strong&amp;amp;amp;amp;gt;']",
        ["English"],
    ),
    (
        r"['English\r\nRussian\r\nSpanish - Spain\r\nJapanese\r\nCzech']",
        ["Czech", "English", "Japanese", "Russian", "Spanish - Spain"],
    ),
    (r"['English[b][/b]', 'Russian\r\n[b][/b]']", ["English", "Russian"]),
    (
        "['English.', 'German;', 'Spanish - Latin America']",
        ["English", "German", "Spanish - Latin America"],
    ),
    ("['Korean', '#lang_slovakian', 'Slovak']", ["Korean", "Slovak"]),
]
DEVELOPER_CASES = [
    ("CAPCOM Co., Ltd.", ["CAPCOM Co., Ltd."]),
    ("Studio Name,Inc.", ["Studio Name, Inc."]),
    ("A,B,C", ["A", "B", "C"]),
    ("Foo,Co.,Ltd.,Bar Games", ["Foo, Co., Ltd.", "Bar Games"]),
    ("Alpha,Co-op Games", ["Alpha", "Co-op Games"]),
    ("Gamma,INC.,Delta", ["Gamma, INC.", "Delta"]),
]


def split_languages(text: str) -> list[str]:
    """Mirror of core.split_languages in model_data/01_schema.sql."""
    t = re.sub(r"&(amp;)+", "&", text).replace("&lt;", "<").replace("&gt;", ">")
    t = re.sub(r"&#?[a-z0-9]+;", " ", t, flags=re.I)
    out = set()
    for part in re.split(r",|;|(?:\\[rn])+|<br\s*/?>", t.strip("[]"), flags=re.I):
        lang = re.sub(r"\\[rnt]|<[^>]*>|\[/?[a-z]+\]|[*#]", " ", part, flags=re.I)
        lang = re.sub(r"\s+", " ", lang).strip(" '\".")
        if lang:
            out.add("Slovak" if lang == "lang_slovakian" else lang)
    return sorted(out)


def split_developers(text: str) -> list[str]:
    """Mirror of core.split_developers in model_data/01_schema.sql."""
    parts = re.split(r",(?! )(?!" + SUFFIX + r"\.?(,|$))", text, flags=re.I)
    parts = parts[::3]  # re.split also returns the pattern's two groups
    return [re.sub(r",(?=\S)", ", ", p.strip()) for p in parts if p and p.strip()]


def check_sql_files() -> list[str]:
    """Every SQL file splits into balanced statements; every check has its headers."""
    problems = []
    for rel in SQL_FILES:
        stmts = read_statements(ROOT / rel)
        bad = [s.line for s in stmts
               if s.sql.count("'") % 2 or s.sql.count("(") != s.sql.count(")")]
        print(f"  {rel}: {len(stmts)} statements")
        if not stmts or bad:
            problems.append(f"{rel}: no statements or unbalanced at lines {bad}")
        if rel.endswith("02_checks.sql"):
            names = [s.meta.get("check") for s in stmts]
            missing = [s.line for s in stmts
                       if not {"check", "severity", "about"} <= set(s.meta)]
            if missing or len(names) != len(set(names)):
                problems.append(f"{rel}: headers missing at {missing} or duplicate names")
    return problems


def check_cases(label: str, func, cases) -> list[str]:
    """Run each (input, expected) pair through func and report the mismatches."""
    problems = []
    for raw, expected in cases:
        got = func(raw)
        if got != expected:
            problems.append(f"{label}: {raw[:50]!r} gave {got}, expected {expected}")
    print(f"  {label}: {len(cases) - len(problems)} of {len(cases)} cases pass")
    return problems


def main() -> int:
    """Run every check; print the problems; return 0 when there are none."""
    problems = check_sql_files()
    problems += check_cases("split_languages", split_languages, LANGUAGE_CASES)
    problems += check_cases("split_developers", split_developers, DEVELOPER_CASES)
    for problem in problems:
        print(f"  FAIL {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
