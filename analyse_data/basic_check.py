"""Quick look at the loaded Steam table: size, columns, sample rows, basic stats.

Run from the project root:
    python analyse_data/basic_check.py                 # games_raw, 5 random rows
    python analyse_data/basic_check.py -n 10 --table games_raw

Connection settings come from db_config.json (saved by get_data), otherwise you
are prompted.  Password: PGPASSWORD environment variable or a hidden prompt.
Output is sized to the terminal width.
"""

import argparse
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))  # so `get_data` imports when run as a script

# pylint: disable=wrong-import-position
from get_data.config import get_password, resolve_settings
from get_data.db import make_engine

NUMERIC_TYPES = {"smallint", "integer", "bigint", "real", "double precision", "numeric"}
WIDTH = max(60, shutil.get_terminal_size((100, 30)).columns - 1)


# --- formatting helpers --------------------------------------------------------

def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(3, min(WIDTH, 80) - len(title) - 5))


def short(value, width: int) -> str:
    """One-line, truncated text for any cell value."""
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return "<null>"
    s = re.sub(r"\s+", " ", str(value)).strip()
    return s if len(s) <= width else s[: max(1, width - 3)] + "..."


def human(x) -> str:
    """Compact number: 1234567 -> 1.2M, 45678 -> 45.7k, 3.14159 -> 3.14."""
    if pd.isna(x):
        return ""
    a = abs(x)
    if a >= 1e9:
        return f"{x / 1e9:.1f}B"
    if a >= 1e6:
        return f"{x / 1e6:.1f}M"
    if a >= 1e4:
        return f"{x / 1e3:.1f}k"
    return f"{x:.0f}" if float(x).is_integer() else f"{x:.2f}"


def print_records(df: pd.DataFrame) -> None:
    """Print rows vertically, one 'column : value' line each, like psql's \\x mode."""
    label_w = min(max(len(c) for c in df.columns), 24)
    value_w = WIDTH - label_w - 3
    for n, (_, row) in enumerate(df.iterrows(), 1):
        print(f"\n--- row {n} of {len(df)} ---")
        for col, val in row.items():
            print(f"{short(col, label_w):<{label_w}} : {short(val, value_w)}")


# --- main ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", default="games_raw", help="table to inspect (default: games_raw)")
    parser.add_argument("-n", "--rows", type=int, default=5, help="random sample rows to show (default: 5)")
    args = parser.parse_args()

    no_flags = SimpleNamespace(user=None, database=None, host=None, port=None)
    settings, _ = resolve_settings(no_flags, PROJECT_ROOT / "db_config.json")
    engine = make_engine(settings, get_password(settings))
    quote = engine.dialect.identifier_preparer.quote
    table = quote(args.table)

    pd.set_option("display.width", WIDTH)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.max_colwidth", 30)

    try:
        with engine.connect() as conn:
            # --- table overview ------------------------------------------------
            cols = pd.read_sql(
                text("SELECT column_name, data_type FROM information_schema.columns "
                     "WHERE table_schema = current_schema() AND table_name = :t "
                     "ORDER BY ordinal_position"),
                conn, params={"t": args.table})
            if cols.empty:
                print(f"Table {args.table} not found in {settings.describe()}")
                return 1

            n_rows = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            size = conn.execute(text("SELECT pg_size_pretty(pg_total_relation_size(CAST(:t AS regclass)))"),
                                {"t": table}).scalar_one()
            section("Table")
            print(f"{args.table} in {settings.describe()}")
            print(f"{n_rows:,} rows x {len(cols)} columns, {size} on disk")

            # --- raw examples --------------------------------------------------
            sample = pd.read_sql(text(f"SELECT * FROM {table} ORDER BY random() LIMIT :n"),
                                 conn, params={"n": args.rows})
            section(f"{len(sample)} random rows (long text cut to fit)")
            if len(sample):
                print_records(sample)
            else:
                print("(table is empty)")

            # --- per-column nulls and distinct values ---------------------------
            parts = [f"count({quote(c)}) AS nn{i}, count(DISTINCT {quote(c)}) AS ud{i}"
                     for i, c in enumerate(cols["column_name"])]
            counts = conn.execute(text(f"SELECT {', '.join(parts)} FROM {table}")).mappings().one()
            overview = cols.rename(columns={"column_name": "column", "data_type": "type"})
            overview["type"] = overview["type"].replace({"double precision": "double", "character varying": "varchar"})
            overview["non_null"] = [counts[f"nn{i}"] for i in range(len(cols))]
            overview["null_%"] = ((1 - overview["non_null"] / n_rows) * 100).round(1) if n_rows else 0.0
            overview["distinct"] = [counts[f"ud{i}"] for i in range(len(cols))]
            section("Columns: type, nulls, distinct values")
            print(overview.to_string(index=False))

            # --- numeric stats --------------------------------------------------
            num = cols.loc[cols["data_type"].isin(NUMERIC_TYPES), "column_name"]
            if len(num):
                values = pd.read_sql(text(f"SELECT {', '.join(quote(c) for c in num)} FROM {table}"), conn)
                stats = values.describe().T.drop(columns="count").map(human)
                section("Numeric stats (k = thousand, M = million)")
                print(stats.to_string())

            # --- boolean columns: x% yes - y% no --------------------------------
            bools = cols.loc[cols["data_type"] == "boolean", "column_name"].tolist()
            if bools and n_rows:
                parts = [f"count(*) FILTER (WHERE {quote(c)}) AS y{i}, "
                         f"count(*) FILTER (WHERE NOT {quote(c)}) AS n{i}"
                         for i, c in enumerate(bools)]
                yn = conn.execute(text(f"SELECT {', '.join(parts)} FROM {table}")).mappings().one()
                section("Boolean columns")
                width = max(len(c) for c in bools)
                for i, c in enumerate(bools):
                    yes, no = yn[f"y{i}"] / n_rows * 100, yn[f"n{i}"] / n_rows * 100
                    null = 100 - yes - no
                    line = f"{c:<{width}}  {yes:5.1f}% yes - {no:5.1f}% no"
                    print(line + (f" - {null:.1f}% null" if null > 0.05 else ""))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
