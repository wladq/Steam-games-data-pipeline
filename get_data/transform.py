"""Pure data steps: read the CSV, repair its header, tidy names, sanity-check.

Nothing here prompts the user; cli.py decides whether each step runs.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# The Kaggle file is missing a comma between these two header names, so the
# header has one name fewer than every data row has fields.
GLUED_HEADER = "DiscountDLC count"
SPLIT_HEADER = ["Discount", "DLC count"]


class SanityError(ValueError):
    pass


# --- reading -----------------------------------------------------------------

def read_header(csv_path: Path) -> list[str]:
    return pd.read_csv(csv_path, nrows=0).columns.tolist()


def read_csv(csv_path: Path, names: list[str] | None = None) -> pd.DataFrame:
    """Read the whole file.  `names` replaces the header row when given.

    Only empty cells count as missing.  pandas would otherwise turn text such as
    'None', 'NA', 'N/A' or 'null' into NaN, erasing a game or company with that name.
    """
    kwargs = dict(index_col=False, low_memory=False, keep_default_na=False, na_values=[""])
    if names is not None:
        return pd.read_csv(csv_path, header=0, names=names, **kwargs)
    return pd.read_csv(csv_path, **kwargs)


# --- header repair -------------------------------------------------------------

def header_is_shifted(columns: list[str]) -> bool:
    return GLUED_HEADER in columns


def fix_shifted_header(columns: list[str]) -> list[str]:
    """Split the glued 'DiscountDLC count' name in two.  Returns the list unchanged if absent."""
    if GLUED_HEADER not in columns:
        return list(columns)
    i = columns.index(GLUED_HEADER)
    return columns[:i] + SPLIT_HEADER + columns[i + 1:]


# --- column names --------------------------------------------------------------

def snake_case(names) -> list[str]:
    """'Estimated owners' -> 'estimated_owners'; 'DLC count' -> 'dlc_count'."""
    idx = pd.Index(names).astype(str)
    return (idx.str.strip().str.lower()
            .str.replace(r"[^a-z0-9]+", "_", regex=True)
            .str.strip("_")).tolist()


def tidy_column_names(df: pd.DataFrame) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """Return (renamed copy, [(old, new), ...] for names that change).  `df` is untouched."""
    new_names = snake_case(df.columns)
    dupes = sorted({n for n in new_names if new_names.count(n) > 1})
    if dupes:
        raise ValueError(f"Tidying would create duplicate column names: {dupes}")
    if "" in new_names:
        raise ValueError("Tidying would create an empty column name")
    changes = [(old, new) for old, new in zip(df.columns, new_names) if old != new]
    return df.set_axis(new_names, axis=1), changes


# --- sanity check --------------------------------------------------------------

def find_column(df: pd.DataFrame, wanted: str) -> str | None:
    """Find a column by its snake_case name, so checks work with or without tidying."""
    for col, norm in zip(df.columns, snake_case(df.columns)):
        if norm == wanted:
            return col
    return None


def _examples(series: pd.Series, n: int = 5) -> str:
    return ", ".join(repr(v) for v in series.head(n).tolist())


def sanity_check(df: pd.DataFrame) -> dict:
    """Raise SanityError if the columns look shifted or appid can't be a primary key.

    Returns a summary dict (including the real appid column name) on success.
    """
    appid_col = find_column(df, "appid")
    if appid_col is None:
        raise SanityError("No AppID column found.")
    ids = pd.to_numeric(df[appid_col], errors="coerce")
    bad_ids = df.loc[ids.isna(), appid_col]
    if len(bad_ids):
        raise SanityError(f"{len(bad_ids)} rows have a missing or non-numeric appid, e.g. {_examples(bad_ids)}")
    dup_ids = df.loc[ids.duplicated(keep=False), appid_col].drop_duplicates()
    if len(dup_ids):
        raise SanityError(f"{len(dup_ids)} appids appear more than once, e.g. {_examples(dup_ids)}")

    disc_col = find_column(df, "discount")
    if disc_col is None:
        raise SanityError("No Discount column found. The header is probably still shifted; "
                          "rerun and accept the header fix.")
    disc = df[disc_col]
    disc_num = pd.to_numeric(disc, errors="coerce")
    bad_disc = disc[disc.notna() & ~disc_num.between(0, 100)]
    if len(bad_disc):
        raise SanityError(f"{len(bad_disc)} discount values are not numbers in 0-100, e.g. {_examples(bad_disc)}. "
                          "The columns are probably shifted.")

    return {"appid_column": appid_col, "rows": len(df), "columns": df.shape[1],
            "blank_discounts": int(disc.isna().sum())}
