"""Unit tests of main.py: argument order, dispatch to each part, interactive questions.

Nothing here touches the database, the network or the project's logs/ folder: each
part's run function, the logging setup and the connection settings are mocks, and
typed answers come from a fake input().

Run:  python main.py debug                 (every test in tests/)
      python tests/test_main.py            (this file only)
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# pylint: disable=wrong-import-position
import main  # noqa: E402
from get_data.config import DbSettings  # noqa: E402

SETTINGS = DbSettings(username="me", database="steam", host="localhost", port=5433)


def run_main(argv, answers=(), events=None):
    """Run main.main(argv) typing `answers`; return (exit code, prompts, stdout).

    Every prompt is also appended to `events` when given, so a test can see the
    order of questions next to other calls that it records there.
    """
    prompts, queue = [], list(answers)

    def fake_input(prompt=""):
        prompts.append(prompt)
        if events is not None:
            events.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    out = io.StringIO()
    with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(out):
        code = main.main(argv)
    return code, prompts, out.getvalue()


def parse(argv):
    """Parse argv with main's parser; argparse errors raise SystemExit."""
    with contextlib.redirect_stderr(io.StringIO()):
        return main.build_parser().parse_args(argv)


class PartMocks(unittest.TestCase):
    """Replace every part's run function, the logging setup and the settings lookup."""

    def setUp(self):
        patches = {
            "run_cli": mock.patch.object(main.get_cli, "run_cli", return_value=0),
            "model": mock.patch.object(main.build, "run", return_value=0),
            "qa": mock.patch.object(main.run_qa, "run", return_value=0),
            "analyse": mock.patch.object(main.basic_check, "run", return_value=0),
            "logging": mock.patch.object(main, "setup_logging"),
            "settings": mock.patch.object(main, "resolve_settings",
                                          return_value=(SETTINGS, {})),
        }
        self.mocks = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)

    def called_args(self, name):
        """The argparse Namespace that the mocked part was called with."""
        self.mocks[name].assert_called_once()
        return self.mocks[name].call_args.args[0]


class ParserTest(unittest.TestCase):
    """Parameters are accepted in the documented order and land in the right fields."""

    def test_get_parses_every_parameter(self):
        args = parse(["get", "json", "-k", "owner/name", "-f", "games.json",
                      "-U", "me", "-d", "steam", "-H", "DebianServ", "-p", "5432",
                      "--table", "landing", "-y", "-v", "--log-dir", "out", "--dry-run"])
        self.assertEqual(
            (args.part, args.what, args.kaggle, args.file, args.local_file),
            ("get", "json", "owner/name", "games.json", None))
        self.assertEqual((args.user, args.database, args.host, args.port),
                         ("me", "steam", "DebianServ", 5432))
        self.assertEqual(args.table, "landing")
        self.assertTrue(args.yes and args.verbose and args.dry_run)
        self.assertEqual(args.log_dir, Path("out"))

    def test_get_without_parameters_leaves_everything_open(self):
        args = parse(["get"])
        for name in ("what", "kaggle", "local_file", "file", "user", "database",
                     "host", "port", "table"):
            with self.subTest(field=name):
                self.assertIsNone(getattr(args, name))
        self.assertFalse(args.yes or args.dry_run or args.normalise_only)

    def test_get_help_lists_the_groups_in_order(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main.build_parser().parse_args(["get", "-h"])
        text = out.getvalue()
        positions = [text.index(title) for title in
                     ("what:", "where from", "where to", "place:", "behaviour:")]
        self.assertEqual(positions, sorted(positions))

    def test_kaggle_and_local_file_exclude_each_other(self):
        with self.assertRaises(SystemExit):
            parse(["get", "-k", "owner/name", "-l", "games.csv"])

    def test_unknown_format_is_rejected(self):
        with self.assertRaises(SystemExit):
            parse(["get", "xml"])

    def test_options_without_a_part_are_rejected(self):
        with self.assertRaises(SystemExit):
            parse(["-y"])

    def test_each_part_has_its_handler(self):
        expected = {"get": main.run_get, "model": main.build.run,
                    "qa": main.run_qa_part, "analyse": main.run_analyse,
                    "debug": main.run_debug}
        for part, handler in expected.items():
            with self.subTest(part=part):
                self.assertIs(parse([part]).handler, handler)


class ParameterModeTest(PartMocks):
    """With parameters, main.py asks nothing itself and calls the part directly."""

    def test_get_calls_the_loader_without_questions(self):
        code, prompts, _ = run_main(["get", "csv", "-l", "tmp/games.csv",
                                     "-U", "me", "-d", "steam"])
        self.assertEqual((code, prompts), (0, []))
        args = self.called_args("run_cli")
        self.assertEqual((args.what, args.local_file, args.kaggle, args.interactive),
                         ("csv", Path("tmp/games.csv"), None, False))
        self.mocks["settings"].assert_not_called()

    def test_model_qa_and_analyse_reach_their_parts(self):
        run_main(["model", "-y"])
        self.assertTrue(self.called_args("model").yes)
        run_main(["qa", "--checks-only"])
        self.assertTrue(self.called_args("qa").checks_only)
        run_main(["analyse", "--table", "games_json", "-n", "2"])
        args = self.called_args("analyse")
        self.assertEqual((args.table, args.rows), ("games_json", 2))

    def test_exit_code_of_the_part_is_returned(self):
        self.mocks["model"].return_value = 1
        code, _, _ = run_main(["model"])
        self.assertEqual(code, 1)


class InteractiveModeTest(PartMocks):
    """Without arguments, main.py asks for the part first, then for that part's details."""

    def test_part_is_asked_first_and_parts_are_listed(self):
        code, prompts, out = run_main([], ["model"])
        self.assertEqual(code, 0)
        self.assertTrue(prompts[0].startswith("Part (get/model/qa/analyse/debug)"))
        for part in main.PARTS:
            self.assertIn(part, out)
        self.assertTrue(self.called_args("model").interactive)

    def test_a_unique_beginning_selects_the_part(self):
        run_main([], ["m"])
        self.mocks["model"].assert_called_once()

    def test_an_unknown_part_is_asked_again(self):
        _, prompts, out = run_main([], ["x", "qa", "y"])
        self.assertIn("Please answer one of", out)
        self.assertEqual(sum(p.startswith("Part") for p in prompts), 2)
        self.assertFalse(self.called_args("qa").checks_only)

    def test_get_asks_what_where_from_where_to_place_in_order(self):
        events = []
        self.mocks["settings"].side_effect = lambda *_: (events.append("WHERE TO"),
                                                         (SETTINGS, {}))[1]
        code, _, _ = run_main([], ["get", "json", "tmp/games.json", ""], events)
        self.assertEqual(code, 0)
        order = [e.split(" ")[0] for e in events]
        self.assertEqual(order, ["Part", "What", "Kaggle", "WHERE", "Target"])
        args = self.called_args("run_cli")
        self.assertEqual((args.what, args.local_file, args.kaggle),
                         ("json", Path("tmp/games.json"), None))
        self.assertEqual((args.user, args.database, args.host, args.port),
                         ("me", "steam", "localhost", 5433))
        self.assertEqual(args.table, "games_json")
        self.mocks["logging"].assert_called_once()

    def test_get_defaults_are_csv_from_the_kaggle_dataset(self):
        run_main([], ["get", "", "", ""])
        args = self.called_args("run_cli")
        self.assertEqual((args.what, args.kaggle, args.local_file, args.table),
                         ("csv", main.get_cli.DEFAULT_DATASET, None, "games_raw"))

    def test_qa_no_means_read_only_checks(self):
        run_main([], ["qa", "n"])
        self.assertTrue(self.called_args("qa").checks_only)

    def test_analyse_asks_table_and_a_whole_number_of_rows(self):
        _, _, out = run_main([], ["analyse", "games_json", "x", "3"])
        self.assertIn("whole number", out)
        args = self.called_args("analyse")
        self.assertEqual((args.table, args.rows), ("games_json", 3))

    def test_no_input_available_is_an_error(self):
        code, _, out = run_main([], [])
        self.assertEqual(code, 1)
        self.assertIn("ERROR", out)

    def test_ctrl_c_returns_130(self):
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.main([]), 130)


class DebugTest(unittest.TestCase):
    """The debug part runs every file in a tests folder and reports each result."""

    FILES = {
        "ok": "raise SystemExit(0)",
        "quiet": "x = 1",                     # no exit at all counts as a pass
        "unit_pass": "raise SystemExit(False)",  # what unittest.main() does on success
        "fails": "raise SystemExit(1)",
        "crashes": "raise ValueError('boom')",
    }

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, body in self.FILES.items():
            Path(tmp.name, f"{name}.py").write_text(body + "\n", encoding="utf-8")
        patch = mock.patch.object(main, "TESTS_DIR", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_all_tests_run_and_failures_are_counted(self):
        code, _, out = run_main(["debug"])
        self.assertEqual(code, 1)
        self.assertIn("3 of 5 tests passed", out)
        self.assertIn("--- crashes: FAIL", out)
        self.assertIn("ERROR ValueError: boom", out)

    def test_one_named_test(self):
        code, _, out = run_main(["debug", "ok"])
        self.assertEqual(code, 0)
        self.assertIn("1 of 1 tests passed", out)

    def test_interactive_debug_asks_which_test(self):
        code, prompts, out = run_main([], ["debug", "fails"])
        self.assertTrue(prompts[1].startswith("Which test"))
        self.assertEqual(code, 1)
        self.assertIn("0 of 1 tests passed", out)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
