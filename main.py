"""Steam-games-data-pipeline: one entry point for every step.

Interactive:  python main.py            numbered menu of parts, then that part's questions
One part:     python main.py PART -m    that part's questions straight away (--manual)
Parameters:   python main.py PART ...   anything not given is asked or defaulted
Help:         python main.py -h         and  python main.py PART -h

The parts are the same code as the per-folder scripts (python -m get_data,
model_data/build.py, qa_data/run_qa.py, analyse_data/basic_check.py,
dashboard/export.py, tests/*.py),
which still work on their own.
"""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

from analyse_data import basic_check
from dashboard import export as dashboard_export
from get_data import PROJECT_NAME, __version__
from get_data import cli as get_cli
from get_data.config import resolve_settings
from get_data.logging_setup import setup_logging
from get_data.prompts import PromptError, ask_choice, ask_text, ask_yes_no
from model_data import build
from qa_data import run_qa

PROJECT_ROOT = Path(__file__).resolve().parent
TESTS_DIR = PROJECT_ROOT / "tests"

PARTS = {
    "get": "download or read games.csv / games.json and load it into PostgreSQL",
    "model": "build the normalised core schema from the JSON landing table",
    "qa": "add the data-quality constraints and run the checks",
    "analyse": "profile the tables of a schema: keys, columns, statistics, sample rows",
    "dashboard": "export a snapshot to docs/: report.md for GitHub, index.html with charts",
    "debug": "run the unit tests in tests/ (no database needed)",
}

EXAMPLES = f"""\
examples:
  python main.py                        numbered menu: choose a part, then answer its questions
  python main.py analyse -m             the analyse questions straight away (works for every part)
  python main.py get csv -k {get_cli.DEFAULT_DATASET} -U me -d steam
  python main.py get json -l tmp/games.json --table games_json -y
  python main.py model -y
  python main.py qa --checks-only
  python main.py analyse --table games,tags --sample-from games
  python main.py dashboard
  python main.py debug

The password is never a flag. Set PGPASSWORD to skip the prompt.
"""

MANUAL_HELP = "ask this part's questions step by step, as when it is chosen from the menu"


# --- parser ------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Top level: -h, --version and one sub-command per part of processing."""
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description=f"{PROJECT_NAME}: get, model, qa, analyse, dashboard, debug.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"{PROJECT_NAME} {__version__}")
    parts = parser.add_subparsers(title="part of processing", dest="part",
                                  metavar="PART", required=True)
    add_get_parser(parts)
    add_part(parts, "model", build.add_arguments, build.run)
    add_part(parts, "qa", run_qa.add_arguments, run_qa_part)
    # analyse asks its own questions after connecting, so it can list schemas and tables
    add_part(parts, "analyse", basic_check.add_arguments, basic_check.run)
    add_part(parts, "dashboard", dashboard_export.add_arguments, dashboard_export.run)
    sub = parts.add_parser("debug", help=PARTS["debug"],
                           description=PARTS["debug"].capitalize() + ".")
    add_manual(sub)
    sub.add_argument("test", nargs="?", choices=list_tests(),
                     help="run only this test (default: all)")
    sub.set_defaults(handler=run_debug)
    return parser


def add_manual(sub: argparse.ArgumentParser) -> None:
    """-m/--manual: run this part interactively without going through the menu."""
    sub.add_argument("-m", "--manual", action="store_true", help=MANUAL_HELP)


def add_part(parts, name: str, add_arguments, handler) -> None:
    """Add one sub-command whose flags come from the part's own module."""
    sub = parts.add_parser(name, help=PARTS[name],
                           description=PARTS[name].capitalize() + ".")
    add_manual(sub)
    add_arguments(sub)
    sub.set_defaults(handler=handler)


def add_get_parser(parts) -> None:
    """The get part; its flags are defined once, in get_data/cli.py."""
    sub = parts.add_parser(
        "get", help=PARTS["get"], usage=get_cli.usage("python main.py get", manual=True),
        description=get_cli.DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_manual(sub)
    get_cli.add_arguments(sub)
    sub.set_defaults(handler=run_get)


# --- interactive questions ---------------------------------------------------------

def ask_part() -> str:
    """Show the numbered parts; accept a number, a name or a unique beginning such as 'm'."""
    names = list(PARTS)
    width = max(map(len, names))
    print("What part of processing?")
    for number, (name, about) in enumerate(PARTS.items(), 1):
        print(f"  {number}  {name:<{width}}  {about}")
    while True:
        answer = ask_text(f"Part (number 1-{len(names)} or name)").strip().lower()
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        matches = [name for name in names if name.startswith(answer)]
        if answer in names or len(matches) == 1:
            return answer if answer in names else matches[0]
        print(f"Please answer a number from 1 to {len(names)} or one of: {', '.join(names)}.")


def ask_get_details(args: argparse.Namespace) -> None:
    """Ask in the same order as the parameters: what, where from, where to, place."""
    args.what = ask_choice("What to load", get_cli.FORMATS, default="csv")
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


def list_tests() -> list[str]:
    """Names of the test files in tests/, without .py."""
    return sorted(p.stem for p in TESTS_DIR.glob("*.py") if not p.name.startswith("_"))


def run_test_file(path: Path) -> int:
    """Run one test file as a script; return its exit code (a crash counts as 1)."""
    try:
        runpy.run_path(str(path), run_name="__main__")
    except SystemExit as exc:  # unittest.main() exits with True / False
        if exc.code is None or isinstance(exc.code, int):
            return int(exc.code or 0)
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
        print(f"--- {name}", flush=True)  # unittest writes to stderr; keep the order
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
        args.interactive = interactive or args.manual
        return args.handler(args)
    except PromptError as exc:
        print(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
