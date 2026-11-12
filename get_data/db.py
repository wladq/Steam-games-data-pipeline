"""Postgres access: engine, connection test, and an all-or-nothing upload."""

from __future__ import annotations

import importlib.util
import logging

import pandas as pd
from sqlalchemy import URL, Engine, create_engine, text

from .config import DbSettings

log = logging.getLogger(__name__)


def make_engine(settings: DbSettings, password: str) -> Engine:
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
    # connect_timeout keeps a dead tunnel / unreachable host from hanging for minutes
    return create_engine(url, connect_args={"connect_timeout": 10})


def test_connection(engine: Engine) -> str:
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
        df.to_sql(table, conn, if_exists="replace", index=False, chunksize=chunksize, method="multi")
        log.info("Adding primary key on %s ...", pk_column)
        conn.execute(text(f"ALTER TABLE {quote(table)} ADD PRIMARY KEY ({quote(pk_column)})"))
    log.info("Committed: table %s now holds %d rows with primary key %s", table, len(df), pk_column)
