"""Offline check, no database needed: the SQL files still split into statements, and the
cleaning regexes of core.split_languages and core.split_developers, mirrored in Python,
clean the real problem strings found in the Kaggle CSV.

Run from anywhere:  python tests/check_cleaning.py

The Python copies only approximate PostgreSQL's regular expressions; the real test is
python model_data/build.py followed by python qa_data/run_qa.py.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from get_data.sql_runner import read_statements  # noqa: E402  pylint: disable=wrong-import-position

for rel in ("model_data/01_schema.sql", "model_data/02_populate.sql", "qa_data/01_constraints.sql",
            "qa_data/02_checks.sql"):
    stmts = read_statements(ROOT / rel)
    bad = [s.line for s in stmts if s.sql.count("'") % 2 or s.sql.count("(") != s.sql.count(")")]
    print(f"{rel}: {len(stmts)} statements, unbalanced at: {bad or 'none'}")


def split_languages(text):
    """Mirror of core.split_languages in model_data/01_schema.sql."""
    t = re.sub(r"&(amp;)+", "&", text).replace("&lt;", "<").replace("&gt;", ">")
    t = re.sub(r"&#?[a-z0-9]+;", " ", t, flags=re.I)
    out = set()
    for part in re.split(r",|;|(?:\\[rn])+|<br\s*/?>", t.strip("[]"), flags=re.I):
        lang = re.sub(r"\s+", " ", re.sub(r"\\[rnt]|<[^>]*>|\[/?[a-z]+\]|[*#]", " ", part, flags=re.I))
        lang = lang.strip(" '\".")
        if lang:
            out.add("Slovak" if lang == "lang_slovakian" else lang)
    return sorted(out)


SUFFIX = r"(inc|incorporated|ltd|limited|llc|l\.l\.c|co|corp|corporation|gmbh|s\.a|s\.r\.o|srl|pty|plc)"


def split_developers(text):
    """Mirror of core.split_developers in model_data/01_schema.sql."""
    parts = re.split(r",(?! )(?!" + SUFFIX + r"\.?(,|$))", text, flags=re.I)
    parts = parts[::3]  # re.split also returns the pattern's two groups; keep the text pieces
    return [re.sub(r",(?=\S)", ", ", p.strip()) for p in parts if p and p.strip()]


LANGUAGES = [
    "['English&amp;amp;amp;amp;lt;strong&amp;amp;amp;amp;gt;&amp;amp;amp;amp;lt;/strong&amp;amp;amp;amp;gt;',"
    " 'French&amp;amp;amp;amp;lt;strong&amp;amp;amp;amp;gt;&amp;amp;amp;amp;lt;/strong&amp;amp;amp;amp;gt;']",
    r"['English\r\nRussian\r\nSpanish - Spain\r\nJapanese\r\nCzech']",
    r"['English[b][/b]', 'Russian\r\n[b][/b]']",
    "['English.', 'German;', 'Spanish - Latin America', 'Traditional Chinese']",
    "['Korean', '#lang_slovakian', 'Slovak', 'Dutch']",
]
for s in LANGUAGES:
    print("LANG", split_languages(s))

DEVELOPERS = ["CAPCOM Co., Ltd.", "Studio Name,Inc.", "A,B,C", "Foo,Co.,Ltd.,Bar Games", "Alpha,Co-op Games",
              "Beta,LLC", "Gamma,INC.,Delta"]
for s in DEVELOPERS:
    print("DEV ", s, "->", split_developers(s))
