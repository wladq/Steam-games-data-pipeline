"""Build the normalised `core` schema from the JSON landing table public.games_json.

Run from the project root:
    python model_data/build.py          # asks before dropping an existing core schema
    python model_data/build.py -y       # no questions

Runs every .sql file in this folder, in name order, inside ONE transaction: if any
statement fails, the previous core schema is left exactly as it was.
"""

import argparse
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # so `get_data` imports when run as a script

# pylint: disable=wrong-import-position
from get_data.prompts import ask_yes_no
from get_data.sql_runner import (
    add_common_args,
    execute,
    open_engine,
    read_statements,
    start,
)

log = logging.getLogger("model_data")

SOURCE_TABLE = "games_json"
REQUIRED_COLUMNS = {"appid": "bigint", "data": "jsonb"}
TABLES = (
    "games",
    "tags",
    "game_tags",
    "genres",
    "game_genres",
    "developers",
    "game_developers",
    "languages",
    "game_languages",
)


def check_source(conn) -> None:
    """Stop with a clear message if the landing table is missing or has another shape."""
    rows = dict(
        execute(
            conn,
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema = 'public' AND table_name = '{SOURCE_TABLE}'",
        ).all()
    )
    if not rows:
        raise RuntimeError(
            f"public.{SOURCE_TABLE} does not exist. "
            "Load the JSON first: python main.py get json"
        )
    wrong = {c: t for c, t in REQUIRED_COLUMNS.items() if rows.get(c) != t}
    if wrong:
        raise RuntimeError(
            f"public.{SOURCE_TABLE} needs the columns {REQUIRED_COLUMNS}, found {rows}. "
            "Reload it: python main.py get json"
        )


def build(engine) -> None:
    """Build the core schema from the public.games_json landing table."""
    files = sorted(HERE.glob("*.sql"))
    with engine.connect() as conn:
        check_source(conn)
        exists = execute(
            conn, "SELECT to_regnamespace('core') IS NOT NULL"
        ).scalar_one()
    if exists and not ask_yes_no(
        "Schema core already exists. Drop it and rebuild it from "
        f"public.{SOURCE_TABLE}?"
    ):
        log.info("Nothing changed")
        return

    with engine.begin() as conn:
        for path in files:
            statements = read_statements(path)
            log.info("Running %s (%d statements) ...", path.name, len(statements))
            for stmt in statements:
                log.debug("%s:%d %s", path.name, stmt.line, stmt.summary)
                try:
                    execute(conn, stmt.sql)
                except Exception as exc:
                    raise RuntimeError(
                        f"{path.name} line {stmt.line} failed, nothing was changed: "
                        f"{str(exc).splitlines()[0]}"
                    ) from exc
        counts = {
            t: execute(conn, f"SELECT count(*) FROM core.{t}").scalar_one()
            for t in TABLES
        }

    log.info("Committed: schema core rebuilt")
    for table, n in counts.items():
        log.info("  core.%-16s %10d rows", table, n)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the flags of this step; main.py uses it for `python main.py model`."""
    add_common_args(parser)


def run(args: argparse.Namespace) -> int:
    """Build the core schema with parsed arguments; return the exit code."""
    start(args)
    try:
        engine = open_engine(args)
        try:
            build(engine)
        finally:
            engine.dispose()
    except KeyboardInterrupt:
        log.error("Interrupted")
        return 130
    except Exception as exc:  # pylint: disable=broad-except
        log.error("%s", exc)
        log.debug("Traceback:", exc_info=True)
        return 1
    log.info("Done")
    return 0


def main() -> int:
    """Run the build process."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
