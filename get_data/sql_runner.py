"""Shared helpers for every step: get, model, qa, analyse, dashboard.

- common command-line flags, logging and connection
- the frame every step runs in: run_step / guarded turn the step's work into an exit
  code (0 done, 1 error with the traceback in debug.log, 130 interrupted)
- run_as_script: what a step does when it is run as its own script
- split a .sql file into statements, keeping each statement's leading comments
- execute raw SQL without any parameter parsing, so '%' and ':' are literal
"""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import offer_to_save, resolve_settings
from .logging_setup import DEBUG_LOG, register_connection, setup_logging
from .prompts import set_auto_yes

if TYPE_CHECKING:  # type hints only: importing SQLAlchemy at startup costs half a second
    from sqlalchemy import Connection, Engine

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "db_config.json"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")
_META = re.compile(r"^([a-z_]+):\s*(.+)$")


# --- command line and connection -------------------------------------------------

def add_db_args(parser: argparse.ArgumentParser,
                title: str = "database (flag > saved config > prompt)"):
    """Add -U -d -H -p, the same connection flags as `python -m get_data`."""
    db = parser.add_argument_group(title)
    db.add_argument("-U", "--user", help="database username")
    db.add_argument("-d", "--database", help="database name")
    db.add_argument("-H", "--host", help="database host (default: localhost)")
    db.add_argument("-p", "--port", type=int, help="database port (default: 5433)")
    return db


def add_behaviour_args(parser: argparse.ArgumentParser, title: str = "behaviour"):
    """Add -y -v --log-dir; return the group so a caller can add its own flags to it."""
    group = parser.add_argument_group(title)
    group.add_argument("-y", "--yes", action="store_true",
                       help="answer yes to every y/n question")
    group.add_argument("-v", "--verbose", action="store_true",
                       help="show debug messages on screen")
    group.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR,
                       help="folder for info.log and debug.log (default: logs/)")
    return group


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Connection flags, then behaviour flags."""
    add_db_args(parser)
    add_behaviour_args(parser)


def start(args: argparse.Namespace) -> None:
    """Set up logging, redaction and auto-yes from the parsed flags."""
    setup_logging(args.verbose, args.log_dir)
    register_connection(args.user, args.host, args.database)
    set_auto_yes(args.yes)


def open_engine(args: argparse.Namespace, offer_save: bool = False) -> Engine:
    """Resolve the settings, then connect with up to three password attempts.

    With offer_save, settings that differ from db_config.json are offered for saving
    after the connection works (the loader does this).  The caller disposes the engine.
    """
    from .db import connect_engine  # pylint: disable=import-outside-toplevel

    settings, saved = resolve_settings(args, CONFIG_PATH)
    log.info("Connecting to %s ...", settings.describe())
    engine, version = connect_engine(settings)
    try:
        log.info("Connected: %s", version)
        if offer_save:
            offer_to_save(settings, saved, CONFIG_PATH)
    except BaseException:
        engine.dispose()
        raise
    return engine


def autocommit(engine: Engine) -> Connection:
    """A connection where every statement stands alone (use it in a with block).

    One failing query then cannot abort the ones after it, which the read-only steps
    rely on when they keep going after a broken check or table.
    """
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


# --- the frame every step runs in -------------------------------------------------

def guarded(work: Callable[[], int | None], logger: logging.Logger | None = None,
            log_dir: Path | None = None) -> int:
    """Run work() and turn the outcome into an exit code.

    work's own return value (None counts as 0), 130 on Ctrl+C, or 1 on any other
    error: its message is logged, the traceback goes to debug.log, and with log_dir
    a hint says where to find it.
    """
    logger = logger or log
    try:
        code = work()
    except KeyboardInterrupt:
        logger.error("Interrupted")
        return 130
    except Exception as exc:  # pylint: disable=broad-except
        logger.error("%s", exc)
        logger.debug("Traceback:", exc_info=True)
        if log_dir:
            logger.info("Details in %s", log_dir / DEBUG_LOG)
        return 1
    return 0 if code is None else int(code)


def run_step(args: argparse.Namespace, work: Callable[[Engine], int | None],
             logger: logging.Logger | None = None) -> int:
    """The whole frame of a database step: logging and auto-yes from args, a
    connection, work(engine), the engine closed again, and an exit code."""
    start(args)

    def with_engine():
        engine = open_engine(args)
        try:
            return work(engine)
        finally:
            engine.dispose()

    return guarded(with_engine, logger, args.log_dir)


def run_as_script(doc: str, add_arguments: Callable[[argparse.ArgumentParser], None],
                  run: Callable[[argparse.Namespace], int],
                  argv: list[str] | None = None) -> int:
    """`python <folder>/<step>.py`: parse the step's own flags, then return run(args).

    The help's description is the first line of `doc`, the step's module docstring.
    """
    parser = argparse.ArgumentParser(description=doc.splitlines()[0])
    add_arguments(parser)
    return run(parser.parse_args(argv))


# --- SQL files -------------------------------------------------------------------

@dataclass
class Statement:
    """One SQL statement plus the comment lines written directly above it."""
    sql: str
    line: int
    comments: list[str] = field(default_factory=list)

    @property
    def meta(self) -> dict[str, str]:
        """'-- key: value' comment lines as a dict, e.g. {'check': ..., 'severity': ...}."""
        found = {}
        for comment in self.comments:
            match = _META.match(comment)
            if match:
                found[match.group(1)] = match.group(2).strip()
        return found

    @property
    def summary(self) -> str:
        """The SQL on one line, cut to 90 characters, for log messages."""
        return re.sub(r"\s+", " ", self.sql)[:90]


def _to_statement(text: str, line: int) -> Statement | None:
    """One piece between semicolons as a Statement, the comment lines directly above
    its SQL as its comments; None when the piece holds no SQL."""
    comments, body = [], []
    for raw in text.strip().splitlines():
        stripped = raw.strip()
        if not body and stripped.startswith("--"):
            comments.append(stripped[2:].strip())
        elif not body and not stripped:
            comments.clear()  # a blank line ends a file header or section title
        else:
            body.append(raw)
    return Statement("\n".join(body).strip(), line, comments) if body else None


def split_statements(sql: str) -> list[Statement]:
    """Split on ';' outside quotes, $$ blocks and -- comments.  /* */ comments are not
    supported."""
    statements: list[Statement] = []
    buf: list[str] = []
    line, start_line = 1, 1
    i, n = 0, len(sql)
    in_quote, dollar = False, None

    def emit() -> None:
        statement = _to_statement("".join(buf), start_line)
        buf.clear()
        if statement:
            statements.append(statement)

    while i < n:
        ch = sql[i]
        if not buf and ch.isspace():
            line += ch == "\n"
            i += 1
            continue
        if not buf:
            start_line = line
        if dollar:
            if sql.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
        elif in_quote:
            in_quote = ch != "'"
        elif ch == "'":
            in_quote = True
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            buf.append(sql[i:end])
            i = end
            continue
        elif ch == "$" and (tag := _DOLLAR_TAG.match(sql, i)):
            dollar = tag.group(0)
            buf.append(dollar)
            i += len(dollar)
            continue
        elif ch == ";":
            emit()
            i += 1
            continue
        line += ch == "\n"
        buf.append(ch)
        i += 1
    emit()
    return statements


def read_statements(path: Path) -> list[Statement]:
    """The statements of a UTF-8 .sql file, split as split_statements does."""
    return split_statements(path.read_text(encoding="utf-8"))


def execute(conn: Connection, sql: str):
    """Run raw SQL exactly as written: no bind parameters, so '%' and ':name' stay literal."""
    return conn.execution_options(no_parameters=True).exec_driver_sql(sql)
