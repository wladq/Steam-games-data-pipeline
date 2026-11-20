"""Run the data-quality constraints and checks against the core schema and print a report.

Run from the project root:
    python qa_data/run_qa.py                 # constraints, then checks
    python qa_data/run_qa.py --checks-only   # read-only: skips the ALTER TABLE statements

Exit code 0 when every constraint is valid and no check of severity 'error' fails;
1 otherwise.  Checks of severity 'warn' are reported but never fail the run.
"""

import argparse
import logging
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # so `get_data` imports when run as a script

# pylint: disable=wrong-import-position
from get_data.sql_runner import add_common_args, execute, open_engine, read_statements, start

log = logging.getLogger("qa_data")

CONSTRAINTS_FILE = HERE / "01_constraints.sql"
CHECKS_FILE = HERE / "02_checks.sql"
_CONSTRAINT_NAME = re.compile(r"CONSTRAINT\s+(?:IF\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_TABLE_NAME = re.compile(r"^[\w.]+$")

# SQLSTATE of "check constraint ... is violated by some row".  The message text follows
# the server's language setting (lc_messages); the five-character code never changes.
CHECK_VIOLATION = "23514"


def first_line(exc: Exception) -> str:
    """The database's own message, without SQLAlchemy's wrapper and the SQL text."""
    return str(getattr(exc, "orig", exc)).strip().splitlines()[0]


def sqlstate(exc: Exception) -> str | None:
    """The SQLSTATE error code of a database error, e.g. '23514'; None for other errors."""
    return getattr(getattr(exc, "orig", exc), "sqlstate", None)


# --- constraints -------------------------------------------------------------------

def run_constraints(conn) -> dict[str, tuple[str, str]]:
    """Execute every statement on its own; return {constraint: (status, detail)}."""
    results: dict[str, tuple[str, str]] = {}
    for stmt in read_statements(CONSTRAINTS_FILE):
        match = _CONSTRAINT_NAME.search(stmt.sql)
        name = match.group(1) if match else f"line {stmt.line}"
        validating = "VALIDATE CONSTRAINT" in stmt.sql.upper()
        log.debug("%s:%d %s", CONSTRAINTS_FILE.name, stmt.line, stmt.summary)
        try:
            execute(conn, stmt.sql)
        except Exception as exc:  # pylint: disable=broad-except
            status = "VIOLATED" if validating and sqlstate(exc) == CHECK_VIOLATION else "ERROR"
            results.setdefault(name, (status, first_line(exc)))  # keep the first problem
            log.debug("%s failed: %s", name, first_line(exc))
        else:
            if validating:
                results.setdefault(name, ("valid", ""))
    return results


# --- checks ------------------------------------------------------------------------

def run_checks(conn) -> list[dict]:
    """Count the rule-breaking rows of every check; return one result dict per check."""
    results = []
    for stmt in read_statements(CHECKS_FILE):
        meta = stmt.meta
        name = meta.get("check")
        if not name:
            log.warning("%s line %d has no '-- check:' header, skipped", CHECKS_FILE.name, stmt.line)
            continue
        result = {"check": name, "severity": meta.get("severity", "error").lower(),
                  "about": meta.get("about", ""), "failed": None, "status": "", "detail": ""}
        results.append(result)

        needs = [t.strip() for t in meta.get("requires", "").split(",") if t.strip()]
        bad = [t for t in needs if not _TABLE_NAME.match(t)]
        if bad:
            result.update(status="ERROR", detail=f"bad 'requires' value {bad}")
            continue
        missing = [t for t in needs
                   if not execute(conn, f"SELECT to_regclass('{t}') IS NOT NULL").scalar_one()]
        if missing:
            result.update(status="skip", detail=f"{', '.join(missing)} does not exist")
            continue
        try:
            failed = execute(conn, f"SELECT count(*) FROM (\n{stmt.sql}\n) AS broken_rows").scalar_one()
        except Exception as exc:  # pylint: disable=broad-except
            result.update(status="ERROR", detail=first_line(exc))
            continue

        result["failed"] = failed
        if not failed:
            result["status"] = "pass"
            continue
        result["status"] = "FAIL" if result["severity"] == "error" else "warn"
        sample = execute(conn, f"SELECT * FROM (\n{stmt.sql}\n) AS broken_rows LIMIT 3").mappings().all()
        log.debug("%s: %d rows break the rule, e.g. %s", name, failed, [dict(row) for row in sample])
    return results


# --- report ------------------------------------------------------------------------

def report(constraints: dict[str, tuple[str, str]] | None, checks: list[dict]) -> bool:
    """Log the report; return True when nothing of severity 'error' is wrong."""
    ok = True
    if constraints is not None:
        log.info("")
        log.info("Constraints (%s)", CONSTRAINTS_FILE.name)
        for name, (status, detail) in constraints.items():
            log.info("  %-8s  %s%s", status, name, f"   {detail}" if detail else "")
        bad = sum(status != "valid" for status, _ in constraints.values())
        log.info("  %d valid, %d violated or failed", len(constraints) - bad, bad)
        ok = ok and not bad

    log.info("")
    log.info("Checks (%s): rows that break each rule", CHECKS_FILE.name)
    width = max((len(c["check"]) for c in checks), default=10)
    log.info("  %-6s  %-8s  %10s  %-*s  %s", "status", "severity", "rows", width, "check", "about")
    for c in checks:
        rows = "" if c["failed"] is None else f"{c['failed']:,}"
        log.info("  %-6s  %-8s  %10s  %-*s  %s", c["status"], c["severity"], rows, width, c["check"],
                 c["detail"] or c["about"])
    tally = {s: sum(c["status"] == s for c in checks) for s in ("pass", "warn", "FAIL", "ERROR", "skip")}
    log.info("  %d passed, %d warnings, %d failed, %d could not run, %d skipped",
             tally["pass"], tally["warn"], tally["FAIL"], tally["ERROR"], tally["skip"])
    return ok and not tally["FAIL"] and not tally["ERROR"]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the flags of this step; main.py uses it for `python main.py qa`."""
    parser.add_argument("--checks-only", action="store_true",
                        help="run only the read-only queries, do not add constraints")
    add_common_args(parser)


def run(args: argparse.Namespace) -> int:
    """Run constraints and checks with parsed arguments; return the exit code."""
    start(args)
    try:
        engine = open_engine(args)
        try:
            # autocommit: every statement stands alone, so one violated constraint
            # or broken check cannot roll back or block the others
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                if not execute(conn, "SELECT to_regclass('core.games') IS NOT NULL").scalar_one():
                    raise RuntimeError("core.games does not exist. Build the model first: "
                                       "python main.py model")
                constraints = None if args.checks_only else run_constraints(conn)
                checks = run_checks(conn)
        finally:
            engine.dispose()
    except KeyboardInterrupt:
        log.error("Interrupted")
        return 130
    except Exception as exc:  # pylint: disable=broad-except
        log.error("%s", exc)
        log.debug("Traceback:", exc_info=True)
        return 1

    ok = report(constraints, checks)
    log.info("")
    log.info("Data quality: %s", "OK" if ok else "PROBLEMS FOUND (see FAIL / VIOLATED / ERROR above)")
    return 0 if ok else 1


def main() -> int:
    """Entry point of `python qa_data/run_qa.py`."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(parser)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
