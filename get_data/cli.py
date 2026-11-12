"""Command line entry point: argparse, logging, and the step-by-step run."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import get_password, offer_to_save, resolve_settings
from .kaggle_download import clear_tmp, download_dataset_file, parse_dataset_ref
from .prompts import PromptError, ask_text, ask_yes_no, set_auto_yes
from .transform import (GLUED_HEADER, SPLIT_HEADER, fix_shifted_header, header_is_shifted,
                        read_csv, read_header, sanity_check, tidy_column_names)

log = logging.getLogger("get_data")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / "tmp"
CONFIG_PATH = PROJECT_ROOT / "db_config.json"
DEFAULT_LOG = PROJECT_ROOT / "get_data.log"
DEFAULT_DATASET = "fronkongames/steam-games-dataset"
DEFAULT_FILE = "games.csv"
DEFAULT_TABLE = "games_raw"

EPILOG = f"""\
examples:
  python -m get_data                          ask for everything interactively
  python -m get_data -k {DEFAULT_DATASET} -U wlad -d steam
  python -m get_data -c path/to/games.csv -y  local file, answer yes to every y/n
  python -m get_data --dry-run                download and check only, no database

The password is never a flag. Set PGPASSWORD to skip the prompt.
Connection settings can be saved to {CONFIG_PATH.name}; delete it to forget them.
"""


# --- setup -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m get_data",
        description="Load the Kaggle Steam games dataset into Postgres.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_argument_group("data source (asked interactively if neither is given)")
    one = src.add_mutually_exclusive_group()
    one.add_argument("-k", "--kaggle", metavar="OWNER/NAME",
                     help=f"Kaggle dataset as owner/name or URL, e.g. {DEFAULT_DATASET}")
    one.add_argument("-c", "--csv", type=Path, help="use this local CSV instead of downloading")
    src.add_argument("-f", "--file", default=DEFAULT_FILE,
                     help=f"file to take from the Kaggle dataset (default: {DEFAULT_FILE})")

    db = p.add_argument_group("database (flag > saved config > prompt)")
    db.add_argument("-U", "--user", help="database username")
    db.add_argument("-d", "--database", help="database name")
    db.add_argument("-H", "--host", help="database host (default: localhost)")
    db.add_argument("-p", "--port", type=int, help="database port (default: 5433)")
    db.add_argument("--table", default=DEFAULT_TABLE, help=f"target table (default: {DEFAULT_TABLE})")

    run = p.add_argument_group("behaviour")
    run.add_argument("-y", "--yes", action="store_true",
                     help="answer yes to every y/n question (text values still need flags or prompts)")
    run.add_argument("--dry-run", action="store_true",
                     help="get, fix and check the data, but do not touch the database")
    run.add_argument("-v", "--verbose", action="store_true", help="show debug messages on screen")
    run.add_argument("--log-file", type=Path, default=DEFAULT_LOG,
                     help=f"debug log file (default: {DEFAULT_LOG.name}, git-ignored)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


class _ConsoleFormatter(logging.Formatter):
    """Plain messages for INFO, a level prefix for everything else."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return msg if record.levelno == logging.INFO else f"{record.levelname}: {msg}"


def setup_logging(verbose: bool, log_file: Path | None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(_ConsoleFormatter("%(message)s"))
    root.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
        root.addHandler(fh)

    for noisy in ("urllib3", "requests", "kagglesdk", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --- steps -------------------------------------------------------------------

def step_get_csv(args) -> Path:
    """Resolve the data source to a local CSV path, downloading into tmp/ if needed."""
    if args.csv:
        csv_path = args.csv
    elif args.kaggle:
        csv_path = _from_kaggle(parse_dataset_ref(args.kaggle), args.file)
    else:
        answer = ask_text("Kaggle dataset (owner/name or URL) or path to a local CSV",
                          default=DEFAULT_DATASET, flag_hint="--kaggle or --csv")
        answer = answer.strip().strip('"').strip("'")
        if answer.lower().endswith(".csv") or Path(answer).is_file():
            csv_path = Path(answer)
        else:
            csv_path = _from_kaggle(parse_dataset_ref(answer), args.file)

    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    log.info("Using %s", csv_path)
    return csv_path


def _from_kaggle(dataset: str, file_name: str) -> Path:
    target = TMP_DIR / file_name
    if target.is_file() and ask_yes_no(
            f"{target.relative_to(PROJECT_ROOT)} is left over from an earlier run. Reuse it instead of downloading?"):
        return target
    return download_dataset_file(dataset, file_name, TMP_DIR)


def step_fix_header(csv_path: Path):
    """Read the CSV, offering to repair the shifted 'DiscountDLC count' header first."""
    columns = read_header(csv_path)
    if not header_is_shifted(columns):
        log.info("Header check: no shifted header found, reading the file as is")
        return read_csv(csv_path)

    log.warning("Header check: '%s' is two names glued together, so every column after it is shifted",
                GLUED_HEADER)
    if ask_yes_no(f"Fix the header by splitting it into {SPLIT_HEADER}?"):
        df = read_csv(csv_path, names=fix_shifted_header(columns))
        log.info("Header fixed")
        return df

    log.warning("Header left as is. The sanity check will most likely stop the run")
    return read_csv(csv_path)


def step_tidy_columns(df):
    """Offer snake_case column names, show the shape before and after, then confirm."""
    if not ask_yes_no("Tidy column names to snake_case (e.g. 'Estimated owners' -> estimated_owners)?"):
        log.info("Keeping the original column names")
        return df

    log.info("Shape before tidying: %s", df.shape)
    tidied, changes = tidy_column_names(df)
    log.info("Shape after tidying:  %s", tidied.shape)
    if not changes:
        log.info("All column names are already tidy")
        return df

    log.info("%d of %d column names would change:", len(changes), df.shape[1])
    width = max(len(old) for old, _ in changes)
    for old, new in changes:
        log.info("  %-*s -> %s", width, old, new)

    if ask_yes_no("Apply these column name changes?"):
        log.info("Column names updated")
        return tidied
    log.info("Changes discarded, keeping the original column names")
    return df


def step_sanity_check(df) -> str:
    summary = sanity_check(df)
    blanks = summary["blank_discounts"]
    log.info("Sanity check passed: %d rows x %d columns, every appid is numeric and unique, "
             "every discount is within 0-100%s",
             summary["rows"], summary["columns"], f" ({blanks} blank)" if blanks else "")
    return summary["appid_column"]


def step_upload(df, pk_column: str, args) -> None:
    from .db import make_engine, test_connection, upload_with_primary_key  # needs sqlalchemy

    settings, saved = resolve_settings(args, CONFIG_PATH)
    engine = make_engine(settings, get_password(settings))
    try:
        log.info("Connecting to %s ...", settings.describe())
        log.info("Connected: %s", test_connection(engine))
        offer_to_save(settings, saved, CONFIG_PATH)
        upload_with_primary_key(df, engine, args.table, pk_column)
    finally:
        engine.dispose()


def step_cleanup() -> None:
    if not TMP_DIR.is_dir() or not any(TMP_DIR.iterdir()):
        return
    size_mb = sum(f.stat().st_size for f in TMP_DIR.rglob("*") if f.is_file()) / 1e6
    if ask_yes_no(f"Delete the downloaded files in tmp/ ({size_mb:.1f} MB)?"):
        clear_tmp(TMP_DIR, PROJECT_ROOT)
    else:
        log.info("Kept tmp/ (it is git-ignored and will be offered for reuse next run)")


# --- main --------------------------------------------------------------------

def run(args) -> None:
    csv_path = step_get_csv(args)
    df = step_fix_header(csv_path)
    df = step_tidy_columns(df)
    pk_column = step_sanity_check(df)
    if args.dry_run:
        log.info("Dry run: skipping the database upload")
        return
    step_upload(df, pk_column, args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.log_file)
    set_auto_yes(args.yes)
    log.debug("get_data %s started with %s", __version__,
              {k: v for k, v in vars(args).items()})

    exit_code = 0
    try:
        run(args)
        log.info("Done")
    except KeyboardInterrupt:
        log.error("Interrupted")
        return 130  # skip the cleanup question; files stay for reuse
    except Exception as exc:
        log.error("%s", exc)
        log.debug("Traceback:", exc_info=True)
        if args.log_file:
            log.info("Details in %s", args.log_file)
        exit_code = 1

    try:
        step_cleanup()
    except (PromptError, KeyboardInterrupt):
        pass
    return exit_code
