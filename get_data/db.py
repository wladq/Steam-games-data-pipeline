"""Postgres access: engine, connection with password retries, and an all-or-nothing upload."""

from __future__ import annotations

import importlib.util
import logging
import os
import socket

import pandas as pd
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.exc import OperationalError

from .config import DbSettings, get_password

log = logging.getLogger(__name__)

PASSWORD_ATTEMPTS = 3
REACH_TIMEOUT = 5  # seconds to wait for host:port before giving up


def check_reachable(settings: DbSettings) -> None:
    """Fail fast, before any password prompt, when nothing answers at host:port."""
    try:
        socket.create_connection((settings.host, settings.port), timeout=REACH_TIMEOUT).close()
    except OSError as exc:
        raise RuntimeError(f"Nothing answers at {settings.host}:{settings.port} ({exc}). "
                           "Is the SSH tunnel running?") from None


def _reason(exc: Exception) -> str:
    """The last line of the driver's message, which holds the server's own reason."""
    lines = [line.strip() for line in str(getattr(exc, "orig", exc)).splitlines() if line.strip()]
    return lines[-1] if lines else type(exc).__name__


def connect_engine(settings: DbSettings, attempts: int = PASSWORD_ATTEMPTS) -> tuple[Engine, str]:
    """Ask for the password and connect; return (engine, server version).

    A failed login is retried up to `attempts` times with a fresh password prompt.  The
    driver reports login errors only as text in the server's language, with no error
    code, so every failed login counts; the reachability check above has already ruled
    out a dead tunnel.  With PGPASSWORD set there is one attempt: retrying would send
    the same password again.
    """
    check_reachable(settings)
    tries = 1 if os.environ.get("PGPASSWORD") else attempts
    for attempt in range(1, tries + 1):
        engine = make_engine(settings, get_password(settings))
        try:
            return engine, test_connection(engine)
        except OperationalError as exc:
            engine.dispose()
            if attempt == tries:
                raise RuntimeError(f"Could not connect to {settings.describe()} after "
                                   f"{tries} attempt{'s' if tries > 1 else ''}: "
                                   f"{_reason(exc)}") from exc
            log.warning("Could not connect (attempt %d of %d): %s", attempt, tries, _reason(exc))
    raise AssertionError("unreachable")  # the loop either returns or raises


def make_engine(settings: DbSettings, password: str) -> Engine:
    """A Postgres engine through psycopg; nothing connects until the first use."""
    if importlib.util.find_spec("psycopg") is None:
        raise RuntimeError('The Postgres driver is missing. Run: pip install "psycopg[binary]"')
    url = URL.create(
        "postgresql+psycopg",
        username=settings.username,
        password=password,
        host=settings.host,
        port=settings.port,
        database=settings.database,
    )
    # connect_timeout keeps a dead tunnel / unreachable host from hanging for minutes;
    # pool_pre_ping replaces a pooled connection that died while the loader spent minutes
    # downloading and repairing the file after connecting
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})


def test_connection(engine: Engine) -> str:
    """Open one connection and return the server's version string."""
    with engine.connect() as conn:
        return conn.execute(text("SELECT version()")).scalar_one()


def upload_with_primary_key(df: pd.DataFrame, engine: Engine, table: str, pk_column: str,
                            chunksize: int = 1000) -> None:
    """Replace `table` with `df` and add a primary key, in ONE transaction.

    If any part fails, Postgres rolls back the DROP as well, so an existing
    table is left exactly as it was.
    """
    quote = engine.dialect.identifier_preparer.quote
    with engine.begin() as conn:
        log.info("Uploading %d rows to table %s (replacing it if it exists) ...", len(df), table)
        df.to_sql(table, conn, if_exists="replace", index=False, chunksize=chunksize,
                  method="multi")
        log.info("Adding primary key on %s ...", pk_column)
        conn.execute(text(f"ALTER TABLE {quote(table)} ADD PRIMARY KEY ({quote(pk_column)})"))
    log.info("Committed: table %s now holds %d rows with primary key %s", table, len(df), pk_column)
