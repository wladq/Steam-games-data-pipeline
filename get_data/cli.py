"""Command line entry point: argparse, logging, and the step-by-step run.

Two data paths share the same prompts, flags and logging.  CSV is the default;
JSON is used when the local file is a .json, when the Kaggle dataset has no CSV
at all, or when --json asks for it.

  CSV   games.csv  -> header fix -> tidy names -> sanity check -> games_raw
  JSON  games.json -> sanity check + jsonb landing table games_json
                   -> optional wide tables in schema staging (games, game_tags, ...)
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from . import __version__, json_load
from .config import offer_to_save, resolve_settings
from .kaggle_download import clear_tmp, download_dataset_file, list_dataset_files, parse_dataset_ref
from .logging_setup import DEBUG_LOG, register_connection, setup_logging
from .prompts import PromptError, ask_text, ask_yes_no, set_auto_yes
from .transform import (GLUED_HEADER, SPLIT_HEADER, fix_shifted_header, header_is_shifted,
                        read_csv, read_header, sanity_check, tidy_column_names)

log = logging.getLogger("get_data")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / "tmp"
CONFIG_PATH = PROJECT_ROOT / "db_config.json"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_DATASET = "fronkongames/steam-games-dataset"
PREFERRED_FILE = {"csv": "games.csv", "json": "games.json"}
DEFAULT_TABLE = {"csv": "games_raw", "json": json_load.LANDING_TABLE}

EPILOG = f"""\
examples:
  python -m get_data                          ask for everything interactively
  python -m get_data -k {DEFAULT_DATASET} -U wlad -d steam
  python -m get_data -c path/to/games.json -y local file (JSON by extension), yes to every y/n
  python -m get_data --json                   take the dataset's JSON file instead of its CSV
  python -m get_data --dry-run                download and check only, no database
  python -m get_data --normalise-only         rebuild the normalised tables from {DEFAULT_TABLE['json']}

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
    one.add_argument("-c", "--csv", "--local", dest="local_file", type=Path, metavar="FILE",
                     help="use this local file instead of downloading (.json is loaded as JSON)")
    src.add_argument("-f", "--file", help="exact file to take from the Kaggle dataset "
                                          "(default: its CSV, or its JSON if it has no CSV)")
    src.add_argument("-j", "--json", action="store_true",
                     help="take the Kaggle dataset's JSON file even when it also has a CSV")

    db = p.add_argument_group("database (flag > saved config > prompt)")
    db.add_argument("-U", "--user", help="database username")
    db.add_argument("-d", "--database", help="database name")
    db.add_argument("-H", "--host", help="database host (default: localhost)")
    db.add_argument("-p", "--port", type=int, help="database port (default: 5433)")
    db.add_argument("--table", help=f"target table (default: {DEFAULT_TABLE['csv']} for CSV, "
                                    f"landing table {DEFAULT_TABLE['json']} for JSON)")
    db.add_argument("--normalise-only", action="store_true",
                    help="skip download and loading; rebuild the tables in schema staging "
                         "from the existing JSON landing table")

    run = p.add_argument_group("behaviour")
    run.add_argument("-y", "--yes", action="store_true",
                     help="answer yes to every y/n question (text values still need flags or prompts)")
    run.add_argument("--dry-run", action="store_true",
                     help="get, fix and check the data, but do not touch the database")
    run.add_argument("-v", "--verbose", action="store_true", help="show debug messages on screen")
    run.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR,
                     help=f"folder for info.log and debug.log, both anonymised "
                          f"(default: {DEFAULT_LOG_DIR.name}/, git-ignored)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


# --- data source ---------------------------------------------------------------

SOURCE_QUESTION = "Kaggle dataset (owner/name or URL) or path to a local CSV/JSON file"


def classify_source(text: str) -> tuple[str, str | Path]:
    """Tell a local file from a Kaggle dataset: ('local', Path) or ('kaggle', 'owner/name')."""
    text = text.strip().strip('"').strip("'")
    if text.lower().endswith((".csv", ".json")) or Path(text).is_file():
        return "local", Path(text)
    return "kaggle", parse_dataset_ref(text)


def step_get_file(args) -> Path:
    """Resolve the data source to a local file, downloading into tmp/ if needed."""
    if args.local_file:
        path = args.local_file
    elif args.kaggle:
        path = _from_kaggle(parse_dataset_ref(args.kaggle), args)
    else:
        kind, value = classify_source(ask_text(SOURCE_QUESTION, default=DEFAULT_DATASET,
                                               flag_hint="--kaggle or --local"))
        path = value if kind == "local" else _from_kaggle(value, args)

    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    if args.what and data_format(path) != args.what:
        raise ValueError(f"Asked for {args.what.upper()}, but {path.name} is a "
                         f"{data_format(path).upper()} file")
    log.info("Using %s (%.1f MB)", path, path.stat().st_size / 1e6)
    return path


def _choose_kaggle_file(files: list[tuple[str, int]], args) -> str:
    """Pick the file to download: --file, else the requested format, else CSV, else JSON."""
    names = [n for n, _ in files]
    listing = ", ".join(f"{n} ({b / 1e6:.0f} MB)" for n, b in files)
    log.info("Dataset files: %s", listing)
    if args.file:
        if args.file not in names:
            raise FileNotFoundError(f"{args.file} is not in the dataset. Available: {', '.join(names)}")
        return args.file

    by_fmt = {fmt: [n for n in names if n.lower().endswith("." + fmt)] for fmt in ("csv", "json")}
    if args.what:
        fmt = args.what
        if not by_fmt[fmt]:
            raise FileNotFoundError(f"{fmt.upper()} requested, but the dataset has no "
                                    f".{fmt} file. Available: {', '.join(names)}")
    elif by_fmt["csv"]:
        fmt = "csv"
    elif by_fmt["json"]:
        fmt = "json"
        log.info("The dataset has no CSV file; using its JSON file")
    else:
        raise FileNotFoundError(f"The dataset has neither a CSV nor a JSON file. Available: {', '.join(names)}")

    candidates = by_fmt[fmt]
    if PREFERRED_FILE[fmt] in candidates or len(candidates) == 1:
        return PREFERRED_FILE[fmt] if PREFERRED_FILE[fmt] in candidates else candidates[0]
    return ask_text(f"Several {fmt.upper()} files; which one? ({', '.join(candidates)})",
                    default=candidates[0], flag_hint="--file")


def _from_kaggle(dataset: str, args) -> Path:
    from .kaggle_download import _make_api  # one authenticated client for listing and download

    api = _make_api()
    file_name = _choose_kaggle_file(list_dataset_files(dataset, api), args)
    target = TMP_DIR / file_name
    if target.is_file() and ask_yes_no(
            f"{target.relative_to(PROJECT_ROOT)} is left over from an earlier run. Reuse it instead of downloading?"):
        return target
    return download_dataset_file(dataset, file_name, TMP_DIR, api)


def data_format(path: Path) -> str:
    return "json" if path.suffix.lower() == ".json" else "csv"


# --- shared steps --------------------------------------------------------------

def connect(args):
    """Resolve settings, connect with up to three password attempts.  Caller disposes the engine."""
    from .db import connect_engine

    settings, saved = resolve_settings(args, CONFIG_PATH)
    log.info("Connecting to %s ...", settings.describe())
    engine, version = connect_engine(settings)
    try:
        log.info("Connected: %s", version)
        offer_to_save(settings, saved, CONFIG_PATH)
    except Exception:
        engine.dispose()
        raise
    return engine


def step_cleanup(used: Path | None) -> None:
    """Offer to clear tmp/ only when this run worked from a file inside it."""
    if used is None or not used.resolve().is_relative_to(TMP_DIR.resolve()):
        return
    if not TMP_DIR.is_dir() or not any(TMP_DIR.iterdir()):
        return
    size_mb = sum(f.stat().st_size for f in TMP_DIR.rglob("*") if f.is_file()) / 1e6
    if ask_yes_no(f"Delete the downloaded files in tmp/ ({size_mb:.1f} MB)?"):
        clear_tmp(TMP_DIR, PROJECT_ROOT)
    else:
        log.info("Kept tmp/ (it is git-ignored and will be offered for reuse next run)")


# --- CSV steps -----------------------------------------------------------------

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


def run_csv(args, csv_path: Path, table: str) -> None:
    from .db import upload_with_primary_key

    df = step_fix_header(csv_path)
    df = step_tidy_columns(df)
    pk_column = step_sanity_check(df)
    if args.dry_run:
        log.info("Dry run: skipping the database upload")
        return
    engine = connect(args)
    try:
        upload_with_primary_key(df, engine, table, pk_column)
    finally:
        engine.dispose()


# --- JSON steps ----------------------------------------------------------------

def step_normalise(engine, landing_table: str) -> None:
    """Offer to rebuild the staging tables, then to drop their old copies in public."""
    schema = json_load.STAGING_SCHEMA
    if not ask_yes_no(f"Build the {len(json_load.NORMALISED_TABLES)} wide tables in schema "
                      f"{schema} (games, game_tags, ...) from {landing_table}? Existing "
                      f"tables in {schema} are replaced."):
        log.info("Staging tables not built. Rerun with --normalise-only to build them later")
        return
    json_load.normalise(engine, landing_table)

    legacy = json_load.legacy_tables(engine)
    if not legacy:
        return
    old = json_load.LEGACY_SCHEMA
    log.info("Earlier versions built the same tables in %s: %s", old, ", ".join(legacy))
    if ask_yes_no(f"Drop these old copies from {old}? They are now in {schema}.", default=False):
        json_load.drop_legacy(engine, legacy)
    else:
        log.info("Kept the old copies in %s", old)


def run_json(args, json_path: Path, table: str) -> None:
    if args.dry_run:
        inv = json_load.scan(json_path)
        inv.log_summary()
        log.info("Dry run: skipping the database upload")
        return

    engine = connect(args)
    try:
        # sanity check and landing load happen in one pass; any failure rolls everything back
        inv = json_load.load_landing(json_path, engine, table)
        inv.log_summary()
        step_normalise(engine, table)
    finally:
        engine.dispose()


def run_normalise_only(args) -> None:
    table = args.table or DEFAULT_TABLE["json"]
    if args.dry_run:
        log.info("Dry run with --normalise-only: nothing to do")
        return
    engine = connect(args)
    try:
        step_normalise(engine, table)
    finally:
        engine.dispose()


# --- main --------------------------------------------------------------------

def run(args, state: dict) -> None:
    if args.normalise_only:
        run_normalise_only(args)
        return

    path = step_get_file(args)
    state["used"] = path  # cleanup looks at this, even if a later step fails
    fmt = data_format(path)
    table = args.table or DEFAULT_TABLE[fmt]
    log.info("Format: %s -> table %s", fmt.upper(), table)
    if fmt == "json":
        run_json(args, path, table)
    else:
        run_csv(args, path, table)


def main(argv: list[str] | None = None) -> int:
    """Entry point of ``python -m get_data``."""
    return run_cli(build_parser().parse_args(argv))


def run_cli(args) -> int:
    """Run the loader with parsed arguments, from this module's parser or from main.py.

    ``args.what`` is 'csv', 'json' or None (CSV unless the file or dataset is JSON only);
    this module's own parser sets it through --json.
    """
    args.what = getattr(args, "what", None) or ("json" if getattr(args, "json", False) else None)
    setup_logging(args.verbose, args.log_dir)
    register_connection(args.user, args.host, args.database)
    set_auto_yes(args.yes)
    log.debug("get_data %s started with %s", __version__, vars(args))

    state: dict = {"used": None}
    exit_code = 0
    try:
        run(args, state)
        log.info("Done")
    except KeyboardInterrupt:
        log.error("Interrupted")
        return 130  # skip the cleanup question; files stay for reuse
    except Exception as exc:
        log.error("%s", exc)
        log.debug("Traceback:", exc_info=True)
        if args.log_dir:
            log.info("Details in %s", args.log_dir / DEBUG_LOG)
        exit_code = 1

    try:
        step_cleanup(state["used"])
    except (PromptError, KeyboardInterrupt):
        pass
    return exit_code
