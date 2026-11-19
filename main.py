"""One entry point for every step of the Steam games pipeline.

Interactive:  python main.py            asks which part to run, then everything else
Parameters:   python main.py PART ...   anything not given is asked or defaulted
Help:         python main.py -h         and  python main.py PART -h

The parts are the same code as the per-folder scripts (python -m get_data,
model_data/build.py, qa_data/run_qa.py, analyse_data/basic_check.py, tests/*.py),
which still work on their own.
"""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

from analyse_data import basic_check
from get_data import __version__
from get_data import cli as get_cli
from get_data.config import resolve_settings
from get_data.logging_setup import setup_logging
from get_data.prompts import PromptError, ask_choice, ask_text, ask_yes_no
from get_data.sql_runner import add_behaviour_args, add_db_args
from model_data import build
from qa_data import run_qa

PROJECT_ROOT = Path(__file__).resolve().parent
TESTS_DIR = PROJECT_ROOT / "tests"
FORMATS = ["csv", "json"]

PARTS = {
    "get": "download or read games.csv / games.json and load it into PostgreSQL",
    "model": "build the normalised core schema from the CSV landing table",
    "qa": "add the data-quality constraints and run the checks",
    "analyse": "show sample rows and basic statistics of one table",
    "debug": "run the offline tests in tests/ (no database needed)",
}

EXAMPLES = f"""\
examples:
  python main.py                        ask for the part, then for everything else
  python main.py get csv -k {get_cli.DEFAULT_DATASET} -U me -d steam
  python main.py get json -l tmp/games.json --table games_json -y
  python main.py model -y
  python main.py qa --checks-only
  python main.py analyse --table games_json -n 3
  python main.py debug

The password is never a flag. Set PGPASSWORD to skip the prompt.
"""

GET_USAGE = """\
python main.py get [-h] [{csv,json}]
                          [-k OWNER/NAME | -l FILE] [-f NAME]
                          [-U USER] [-d DATABASE] [-H HOST] [-p PORT]
                          [--table TABLE] [--normalise-only]
                          [-y] [-v] [--log-dir LOG_DIR] [--dry-run]"""


# --- parser ------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Top level: -h, --version and one sub-command per part of processing."""
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Steam games pipeline: get, model, qa, analyse, debug.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parts = parser.add_subparsers(title="part of processing", dest="part",
                                  metavar="PART", required=True)
    add_get_parser(parts)
    add_part(parts, "model", build.add_arguments, build.run)
    add_part(parts, "qa", run_qa.add_arguments, run_qa_part)
    add_part(parts, "analyse", basic_check.add_arguments, run_analyse)
    sub = parts.add_parser("debug", help=PARTS["debug"],
                           description=PARTS["debug"].capitalize() + ".")
    sub.add_argument("test", nargs="?", choices=list_tests(),
                     help="run only this test (default: all)")
    sub.set_defaults(handler=run_debug)
    return parser


def add_part(parts, name: str, add_arguments, handler) -> None:
    """Add one sub-command whose flags come from the part's own module."""
    sub = parts.add_parser(name, help=PARTS[name],
                           description=PARTS[name].capitalize() + ".")
    add_arguments(sub)
    sub.set_defaults(handler=handler)


def add_get_parser(parts) -> None:
    """The get part, in order: what, where from, where to, place, behaviour."""
    sub = parts.add_parser(
        "get", help=PARTS["get"], usage=GET_USAGE,
        description="Load the Kaggle Steam games dataset into PostgreSQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    what = sub.add_argument_group("what")
    what.add_argument("what", nargs="?", choices=FORMATS,
                      help="data format (default: csv, or json when the file is "
                           ".json or the dataset has no CSV)")

    source = sub.add_argument_group("where from (asked when neither is given)")
    one = source.add_mutually_exclusive_group()
    one.add_argument("-k", "--kaggle", metavar="OWNER/NAME",
                     help=f"Kaggle dataset, e.g. {get_cli.DEFAULT_DATASET}")
    one.add_argument("-l", "--local", dest="local_file", type=Path, metavar="FILE",
                     help="local .csv or .json file instead of a download")
    source.add_argument("-f", "--file", metavar="NAME",
                        help="exact file to take from the Kaggle dataset")

    add_db_args(sub, "where to (flag > saved config > prompt)")

    place = sub.add_argument_group("place")
    place.add_argument("--table",
                       help=f"target table (default: {get_cli.DEFAULT_TABLE['csv']} "
                            f"for CSV, {get_cli.DEFAULT_TABLE['json']} for JSON)")
    place.add_argument("--normalise-only", action="store_true",
                       help="skip loading; rebuild the normalised tables from the "
                            "existing JSON landing table")

    behaviour = add_behaviour_args(sub)
    behaviour.add_argument("--dry-run", action="store_true",
                           help="get, fix and check the data, but do not touch "
                                "the database")
    sub.set_defaults(handler=run_get)


# --- interactive questions ---------------------------------------------------------

def ask_part() -> str:
    """Show the parts and ask which one to run."""
    print("What part of processing?")
    width = max(map(len, PARTS))
    for name, about in PARTS.items():
        print(f"  {name:<{width}}  {about}")
    return ask_choice("Part", list(PARTS))


def ask_int(question: str, default: int) -> int:
    """Ask for a whole number; Enter keeps the default."""
    while True:
        answer = ask_text(question, default=str(default))
        if answer.isdigit():
            return int(answer)
        print("Please type a whole number.")


def ask_get_details(args: argparse.Namespace) -> None:
    """Ask in the same order as the parameters: what, where from, where to, place."""
    args.what = ask_choice("What to load", FORMATS, default="csv")
    kind, value = get_cli.classify_source(
        ask_text(get_cli.SOURCE_QUESTION, default=get_cli.DEFAULT_DATASET))
    if kind == "local":
        args.local_file = value
    else:
        args.kaggle = value
    settings, _ = resolve_settings(args, get_cli.CONFIG_PATH)
    args.user, args.database = settings.username, settings.database
    args.host, args.port = settings.host, settings.port
    args.table = ask_text("Target table", default=get_cli.DEFAULT_TABLE[args.what])


# --- handlers: one per part, each returns an exit code ------------------------------

def run_get(args: argparse.Namespace) -> int:
    """Load a dataset file; interactive runs first ask for its details."""
    if args.interactive:
        setup_logging(args.verbose, args.log_dir)
        ask_get_details(args)
    return get_cli.run_cli(args)


def run_qa_part(args: argparse.Namespace) -> int:
    """Run the QA step; interactive runs ask whether to touch the constraints."""
    if args.interactive:
        args.checks_only = not ask_yes_no(
            "Add and validate the CHECK constraints too? (no = read-only checks)")
    return run_qa.run(args)


def run_analyse(args: argparse.Namespace) -> int:
    """Show one table; interactive runs ask which table and how many rows."""
    if args.interactive:
        args.table = ask_text("Table to inspect", default=args.table)
        args.rows = ask_int("Random sample rows", default=args.rows)
    return basic_check.run(args)


def list_tests() -> list[str]:
    """Names of the test files in tests/, without .py."""
    return sorted(p.stem for p in TESTS_DIR.glob("*.py") if not p.name.startswith("_"))


def run_test_file(path: Path) -> int:
    """Run one test file as a script; return its exit code (a crash counts as 1)."""
    try:
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:
        if exc.code is None or isinstance(exc.code, int):
            return exc.code or 0
        return 1
    except Exception as exc:  # pylint: disable=broad-except  # keep running the rest
        print(f"  ERROR {type(exc).__name__}: {exc}")
        return 1
    return 0


def run_debug(args: argparse.Namespace) -> int:
    """Run the offline tests and print one result line per file."""
    names = list_tests()
    if args.test:
        names = [args.test]
    elif args.interactive and len(names) > 1:
        choice = ask_choice("Which test", ["all", *names], default="all")
        names = names if choice == "all" else [choice]

    failed = []
    for name in names:
        print(f"--- {name}")
        code = run_test_file(TESTS_DIR / f"{name}.py")
        print(f"--- {name}: {'pass' if code == 0 else f'FAIL (exit code {code})'}")
        if code:
            failed.append(name)
    print(f"{len(names) - len(failed)} of {len(names)} tests passed")
    return 1 if failed else 0


# --- main ------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Run the part named on the command line, or ask for it when there is none."""
    argv = sys.argv[1:] if argv is None else argv
    interactive = not argv
    try:
        args = build_parser().parse_args([ask_part()] if interactive else argv)
        args.interactive = interactive
        return args.handler(args)
    except PromptError as exc:
        print(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
