"""Profile the tables of a schema: size, keys, columns, value statistics, sample rows.

Run from the project root:
    python main.py                                      # choose 'analyse': lists schemas
                                                        # and tables, then asks
    python main.py analyse                              # every table in schema core
    python main.py analyse --table games,tags -n 5      # two tables of core, 5 random rows
    python main.py analyse --table public.games_json    # schema.table works too
    python main.py analyse --sample-from games          # random rows from games only
    python analyse_data/basic_check.py ...              # the same without main.py

Built like qa_data/run_qa.py: the same connection flags and prompts, the same logging
(console plus the anonymised logs/info.log and logs/debug.log), read-only, and the same
exit codes: 0 when every table was profiled, 1 when the schema or a table is missing or
a query fails.  Empty tables are reported as warnings.  Every statistic is computed in
the database, so a large table is never pulled into Python.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # so `get_data` imports when run as a script

# pylint: disable=wrong-import-position
from get_data.formatting import percent
from get_data.prompts import ask_int, ask_many, ask_one
from get_data.sql_runner import (
    add_common_args,
    autocommit,
    execute,
    run_as_script,
    run_step,
)

log = logging.getLogger("analyse_data")


def text(sql: str):
    """sqlalchemy.text, imported on first use so that loading this module stays fast."""
    from sqlalchemy import text as sa_text  # pylint: disable=import-outside-toplevel
    return sa_text(sql)

DEFAULT_SCHEMA = "core"
DEFAULT_ROWS = 3
WIDTH = max(72, min(shutil.get_terminal_size((110, 30)).columns - 1, 160))

NUMERIC = {"smallint", "integer", "bigint", "numeric", "real", "double precision"}
TEMPORAL = {"date", "timestamp without time zone", "timestamp with time zone"}
TEXTUAL = {"text", "character varying", "character"}
NO_DISTINCT = {"json", "jsonb", "bytea", "xml"}  # no equality, or too costly to count exactly
SHORT_TYPE = {"character varying": "varchar", "double precision": "double",
              "timestamp without time zone": "timestamp",
              "timestamp with time zone": "timestamptz"}


@dataclass
class Column:
    """One column as information_schema describes it."""
    name: str
    type: str


@dataclass
class TableReport:
    """The outcome for one table; status is ok, empty or ERROR."""
    name: str
    status: str = "ok"
    rows: int = 0
    columns: int = 0
    size: str = ""
    detail: str = ""


# --- formatting --------------------------------------------------------------------

def human(x) -> str:
    """Compact number: 1234567 -> 1.2M, 45678 -> 45.7k, 3.14159 -> 3.14, None -> ''."""
    if x is None:
        return ""
    a = abs(x)
    # (from, divide by, suffix): thousands only from 10,000, so 1,234 stays in full
    for threshold, unit, suffix in ((1e9, 1e9, "B"), (1e6, 1e6, "M"), (1e4, 1e3, "k")):
        if a >= threshold:
            return f"{x / unit:.1f}{suffix}"
    return f"{x:.0f}" if float(x).is_integer() else f"{x:.2f}"


def fit(value, width: int) -> str:
    """One line of at most `width` characters; longer text ends in '...'."""
    line = re.sub(r"\s+", " ", str(value)).strip()
    return line if len(line) <= width else line[: max(1, width - 3)] + "..."


def format_value(value) -> str:
    """A cell value as text: JSON for jsonb documents, <null> for NULL."""
    if value is None:
        return "<null>"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# --- catalog -----------------------------------------------------------------------

def split_table(spec: str, schema: str) -> tuple[str, str]:
    """'core.games' -> ('core', 'games'); a bare 'games' belongs to `schema`."""
    left, dot, right = spec.partition(".")
    return (left, right) if dot else (schema, spec)


def split_list(spec: str | None) -> list[str]:
    """'games, tags,' -> ['games', 'tags']."""
    return [part.strip() for part in (spec or "").split(",") if part.strip()]


def list_schemas(conn) -> list[tuple[str, int]]:
    """(schema, number of tables) for every schema that holds tables, system ones left out."""
    return [tuple(row) for row in conn.execute(text(
        "SELECT n.nspname, count(*) FROM pg_namespace AS n "
        "JOIN pg_class AS c ON c.relnamespace = n.oid AND c.relkind IN ('r', 'p') "
        "WHERE n.nspname <> 'information_schema' AND left(n.nspname, 3) <> 'pg_' "
        "GROUP BY n.nspname ORDER BY n.nspname")).all()]


def list_tables(conn, schema: str) -> list[tuple[str, int | None]]:
    """(table, estimated rows) for the tables of `schema`, alphabetically.

    The estimate comes from the planner statistics, so listing is instant; it is None
    when the table has never been analysed.
    """
    rows = conn.execute(text(
        "SELECT c.relname, c.reltuples::bigint FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :s AND c.relkind IN ('r', 'p') ORDER BY c.relname"),
        {"s": schema}).all()
    return [(name, estimate if estimate >= 0 else None) for name, estimate in rows]


def columns_of(conn, schema: str, table: str) -> list[Column]:
    """The table's columns in their defined order; empty when the table does not exist."""
    rows = conn.execute(text(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"),
        {"s": schema, "t": table}).all()
    return [Column(name, kind) for name, kind in rows]


def keys_of(conn, qualified: str) -> list[str]:
    """Primary, unique and foreign keys as Postgres writes them, plus a count of CHECKs."""
    rows = conn.execute(text(
        "SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = CAST(:t AS regclass) "
        "ORDER BY CASE contype WHEN 'p' THEN 1 WHEN 'u' THEN 2 WHEN 'f' THEN 3 ELSE 4 END, "
        "conname"), {"t": qualified}).all()
    keys = [definition for kind, definition in rows if kind in ("p", "u", "f")]
    checks = sum(kind == "c" for kind, _ in rows)
    if checks:
        keys.append(f"{checks} CHECK constraint{'s' if checks > 1 else ''}")
    return keys


def table_size(conn, qualified: str) -> str:
    """Size on disk including indexes, e.g. '52 MB'."""
    return conn.execute(text(
        "SELECT pg_size_pretty(pg_total_relation_size(CAST(:t AS regclass)))"),
        {"t": qualified}).scalar_one()


# --- statistics --------------------------------------------------------------------

def profile_sql(qualified: str, columns: list[Column], quote) -> str:
    """One SELECT returning row count, fill, distinct values and type-specific stats."""
    parts = ["count(*) AS n"]
    for i, col in enumerate(columns):
        q = quote(col.name)
        parts.append(f"count({q}) AS nn{i}")
        if col.type not in NO_DISTINCT:
            parts.append(f"count(DISTINCT {q}) AS nd{i}")
        if col.type in NUMERIC:
            parts += [f"min({q})::float8 AS lo{i}", f"max({q})::float8 AS hi{i}",
                      f"avg({q})::float8 AS av{i}",
                      f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {q}::float8) AS md{i}"]
        elif col.type == "boolean":
            parts.append(f"count(*) FILTER (WHERE {q}) AS yes{i}")
        elif col.type in TEMPORAL:
            parts += [f"min({q})::text AS lo{i}", f"max({q})::text AS hi{i}"]
        elif col.type in TEXTUAL:
            parts += [f"min(length({q})) AS lo{i}", f"max(length({q})) AS hi{i}"]
    return f"SELECT {', '.join(parts)} FROM {qualified}"


def describe(col: Column, stats, i: int) -> tuple[str, str, str]:
    """(filled share, distinct values, value summary) of column number i."""
    total, filled = stats["n"], stats[f"nn{i}"]
    distinct = "-" if col.type in NO_DISTINCT else human(stats[f"nd{i}"])
    values = ""
    if filled and col.type in NUMERIC:
        values = (f"min {human(stats[f'lo{i}'])}, median {human(stats[f'md{i}'])}, "
                  f"mean {human(stats[f'av{i}'])}, max {human(stats[f'hi{i}'])}")
    elif filled and col.type == "boolean":
        yes = stats[f"yes{i}"]
        values = f"{percent(yes, filled)} yes, {percent(filled - yes, filled)} no"
    elif filled and col.type in TEMPORAL:
        values = f"{stats[f'lo{i}']} to {stats[f'hi{i}']}"
    elif filled and col.type in TEXTUAL:
        lo, hi = stats[f"lo{i}"], stats[f"hi{i}"]
        values = f"length {lo}" if lo == hi else f"length {lo} to {hi}"
    return percent(filled, total), distinct, values


# --- report ------------------------------------------------------------------------

def log_columns(columns: list[Column], stats) -> None:
    """One aligned line per column: name, type, filled, distinct, values."""
    name_w = min(max(len(c.name) for c in columns), 28)
    type_w = min(max(len(SHORT_TYPE.get(c.type, c.type)) for c in columns), 12)
    values_w = max(10, WIDTH - name_w - type_w - 24)
    log.info("  %-*s  %-*s  %7s  %8s  %s", name_w, "column", type_w, "type", "filled",
             "distinct", "values")
    for i, col in enumerate(columns):
        filled, distinct, values = describe(col, stats, i)
        log.info("  %-*s  %-*s  %7s  %8s  %s", name_w, fit(col.name, name_w), type_w,
                 fit(SHORT_TYPE.get(col.type, col.type), type_w), filled, distinct,
                 fit(values, values_w))


def log_samples(conn, qualified: str, columns: list[Column], rows: int) -> None:
    """`rows` random rows, printed vertically like psql's expanded mode."""
    sample = execute(conn, f"SELECT * FROM {qualified} ORDER BY random() LIMIT {int(rows)}"
                     ).mappings().all()
    label_w = min(max(len(c.name) for c in columns), 24)
    for n, row in enumerate(sample, 1):
        log.info("  --- random row %d of %d ---", n, len(sample))
        for name, value in row.items():
            log.info("  %-*s : %s", label_w, fit(name, label_w),
                     fit(format_value(value), WIDTH - label_w - 5))


def profile_table(conn, schema: str, table: str, rows: int, quote) -> TableReport:
    """Log everything about one table and return its outcome."""
    report = TableReport(f"{schema}.{table}")
    columns = columns_of(conn, schema, table)
    if not columns:
        report.status, report.detail = "ERROR", "table not found"
        log.error("%s: table not found", report.name)
        return report

    qualified = f"{quote(schema)}.{quote(table)}"
    stats = execute(conn, profile_sql(qualified, columns, quote)).mappings().one()
    report.rows, report.columns = stats["n"], len(columns)
    report.size = table_size(conn, qualified)
    log.info("")
    log.info("=== %s %s", report.name, "=" * max(3, min(WIDTH, 90) - len(report.name) - 5))
    log.info("  %s rows, %d columns, %s", f"{report.rows:,}", report.columns, report.size)
    for key in keys_of(conn, qualified):
        log.info("  %s", fit(key, WIDTH - 2))
    if not report.rows:
        report.status, report.detail = "empty", "no rows"
        log.warning("%s is empty", report.name)
        return report
    log_columns(columns, stats)
    if rows > 0:
        log_samples(conn, qualified, columns, rows)
    return report


def summary(reports: list[TableReport]) -> bool:
    """Log one line per table; return True when no table failed."""
    log.info("")
    log.info("Tables profiled")
    name_w = max(len(r.name) for r in reports)
    for r in reports:
        log.info("  %-6s  %12s rows  %3d columns  %8s  %-*s  %s", r.status, f"{r.rows:,}",
                 r.columns, r.size, name_w, r.name, r.detail)
    tally = {s: sum(r.status == s for r in reports) for s in ("ok", "empty", "ERROR")}
    log.info("  %d profiled, %d empty, %d failed", tally["ok"], tally["empty"], tally["ERROR"])
    return not tally["ERROR"]


# --- command line --------------------------------------------------------------------

def targets(conn, args: argparse.Namespace) -> list[tuple[str, str]]:
    """(schema, table) pairs to profile: the --table list, else every table of --schema."""
    if args.table:
        return list(dict.fromkeys(split_table(s, args.schema) for s in split_list(args.table)))
    tables = list_tables(conn, args.schema)
    if not tables:
        raise RuntimeError(f"Schema {args.schema} has no tables or does not exist. "
                           "Build the model first: python main.py model")
    return [(args.schema, name) for name, _ in tables]


def sample_set(args: argparse.Namespace, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """The profiled tables that also show random rows: --sample-from, else all of them."""
    if args.rows <= 0:
        return set()
    if not args.sample_from:
        return set(pairs)
    wanted = {split_table(s, args.schema) for s in split_list(args.sample_from)}
    for schema, table in sorted(wanted - set(pairs)):
        log.warning("%s.%s is not among the profiled tables, so it shows no rows", schema, table)
    return wanted & set(pairs)


def choose_interactively(conn, args: argparse.Namespace) -> None:
    """Show the schemas, then the tables, and ask what to profile and to sample."""
    schemas = list_schemas(conn)
    if not schemas:
        raise RuntimeError("The database has no schema with tables. Load data first: "
                           "python main.py get json")
    names = [name for name, _ in schemas]
    print("Schemas with tables:")
    args.schema = ask_one(
        "Schema to profile, name or number", names,
        default=args.schema if args.schema in names else names[0],
        notes={name: f"{count} table{'s' if count != 1 else ''}" for name, count in schemas})

    tables = list_tables(conn, args.schema)
    every = [name for name, _ in tables]
    print(f"Tables in {args.schema}:")
    chosen = ask_many(
        "Tables to profile, names or numbers, comma separated (Enter = all)", every,
        default="all", shortcuts={"all": every},
        notes={name: f"about {human(rows)} rows" if rows is not None else "not analysed yet"
               for name, rows in tables})
    args.table = None if len(chosen) == len(every) else ",".join(
        f"{args.schema}.{name}" for name in chosen)

    args.rows = ask_int("Random sample rows per table", default=args.rows)
    args.sample_from = None
    if args.rows > 0 and len(chosen) > 1:
        print("Profiled tables:")
        sampled = ask_many(
            "Show random rows from which tables, comma separated (Enter = all, none = no rows)",
            chosen, default="all", shortcuts={"all": chosen, "none": []})
        if not sampled:
            args.rows = 0
        elif len(sampled) < len(chosen):
            args.sample_from = ",".join(f"{args.schema}.{name}" for name in sampled)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the flags of this step; main.py uses it for `python main.py analyse`."""
    place = parser.add_argument_group("place")
    place.add_argument("--schema", default=DEFAULT_SCHEMA,
                       help=f"schema whose tables are profiled (default: {DEFAULT_SCHEMA})")
    place.add_argument("--table",
                       help="profile only these tables, comma separated; 'schema.table' "
                            "or a table of --schema (default: every table of --schema)")
    place.add_argument("-n", "--rows", type=int, default=DEFAULT_ROWS,
                       help=f"random rows shown per table, 0 for none (default: {DEFAULT_ROWS})")
    place.add_argument("--sample-from", metavar="TABLES",
                       help="show random rows only from these profiled tables, comma "
                            "separated (default: every profiled table)")
    add_common_args(parser)


def run(args: argparse.Namespace) -> int:
    """Profile the chosen tables with parsed arguments; return the exit code.

    In an interactive run (python main.py, then 'analyse') the questions come after
    connecting, so that they can list the schemas and tables that exist.
    """

    def work(engine):
        quote = engine.dialect.identifier_preparer.quote
        # autocommit: a failing query must not abort the tables that follow it
        with autocommit(engine) as conn:
            if getattr(args, "interactive", False):
                choose_interactively(conn, args)
            pairs = targets(conn, args)
            sampled = sample_set(args, pairs)
            reports = [profile_or_error(conn, schema, table,
                                        args.rows if (schema, table) in sampled else 0, quote)
                       for schema, table in pairs]
        return 0 if summary(reports) else 1

    return run_step(args, work, log)


def profile_or_error(conn, schema: str, table: str, rows: int, quote) -> TableReport:
    """profile_table, but a failure becomes an ERROR report and the next table still runs."""
    try:
        return profile_table(conn, schema, table, rows, quote)
    except Exception as exc:  # pylint: disable=broad-except
        detail = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
        log.error("%s.%s: %s", schema, table, detail)
        log.debug("Traceback:", exc_info=True)
        return TableReport(f"{schema}.{table}", "ERROR", detail=detail)


if __name__ == "__main__":
    raise SystemExit(run_as_script(__doc__, add_arguments, run))
