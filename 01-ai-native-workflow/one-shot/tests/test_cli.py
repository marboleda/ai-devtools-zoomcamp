import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from weekly_feedback import cli

from .helpers import TempRepo


def run(argv):
    """Run the CLI, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class WeekSelectionTests(unittest.TestCase):
    def test_explicit_week_is_used(self):
        with TempRepo() as repo:
            repo.commit("Add the loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            code, out, _ = run(["--project", str(repo.path), "--week", "2026-W35"])
            self.assertEqual(code, 0)
            self.assertIn("2026-W35", out)
            self.assertIn("commits 1", out)

    def test_a_week_with_no_work_reports_zero(self):
        with TempRepo() as repo:
            repo.commit("Add the loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            code, out, _ = run(["--project", str(repo.path), "--week", "2026-W28"])
            self.assertEqual(code, 0)
            self.assertIn("commits 0", out)
            self.assertIn("stalled", out)

    def test_weeks_ago_shifts_the_window(self):
        with TempRepo() as repo:
            repo.commit("Add the loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            _, out, _ = run(
                ["--project", str(repo.path), "--week", "2026-W36", "--weeks-ago", "1"]
            )
            self.assertIn("2026-W35", out)
            self.assertIn("commits 1", out)

    def test_bad_week_spec_exits_with_usage_error(self):
        with self.assertRaises(SystemExit) as caught:
            run(["--week", "nonsense"])
        self.assertEqual(caught.exception.code, 2)


class OutputTests(unittest.TestCase):
    def test_json_format(self):
        with TempRepo() as repo:
            repo.commit("Add the loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            code, out, _ = run(
                ["--project", str(repo.path), "--week", "2026-W35", "--format", "json"]
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["week"], "2026-W35")
            self.assertEqual(payload["projects"][0]["stats"]["commits"], 1)

    def test_markdown_format(self):
        with TempRepo() as repo:
            repo.commit("Add the loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            _, out, _ = run(
                ["--project", str(repo.path), "--week", "2026-W35", "--format", "markdown"]
            )
            self.assertIn("# Weekly feedback -- 2026-W35", out)

    def test_out_writes_a_file_and_keeps_stdout_clean(self):
        with TempRepo() as repo:
            repo.commit("Add the loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            target = repo.path / "reports" / "week.md"
            code, out, err = run(
                [
                    "--project", str(repo.path),
                    "--week", "2026-W35",
                    "--format", "markdown",
                    "--out", str(target),
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(out, "")
            self.assertIn("wrote", err)
            self.assertIn("# Weekly feedback", target.read_text(encoding="utf-8"))


class MultiProjectTests(unittest.TestCase):
    def test_two_projects_appear_in_one_report(self):
        with TempRepo() as api, TempRepo() as web:
            api.commit("Add the API loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            web.commit("Add the web shell", {"src/b.py": "y = 1\n"}, "2026-08-25T10:00:00+00:00")
            code, out, _ = run(
                [
                    "--project", str(api.path),
                    "--project", str(web.path),
                    "--week", "2026-W35",
                    "--format", "json",
                ]
            )
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(len(payload["projects"]), 2)
            self.assertEqual({p["stats"]["commits"] for p in payload["projects"]}, {1})

    def test_a_broken_project_does_not_stop_the_others(self):
        with TempRepo() as repo:
            repo.commit("Add the loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            code, out, _ = run(
                [
                    "--project", str(repo.path),
                    "--project", str(repo.path / "does-not-exist"),
                    "--week", "2026-W35",
                    "--format", "json",
                ]
            )
            self.assertEqual(code, cli.EXIT_ERROR)
            payload = json.loads(out)
            self.assertEqual(len(payload["projects"]), 2)
            errors = [p["error"] for p in payload["projects"] if p["error"]]
            self.assertEqual(len(errors), 1)
            self.assertIn("does not exist", errors[0])


class ExitCodeTests(unittest.TestCase):
    def test_clean_run_exits_zero_even_with_warnings(self):
        with TempRepo() as repo:
            repo.commit("wip", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            code, _, _ = run(["--project", str(repo.path), "--week", "2026-W35"])
            self.assertEqual(code, 0)

    def test_strict_exits_one_on_a_warning(self):
        with TempRepo() as repo:
            repo.commit("wip", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            code, _, _ = run(["--project", str(repo.path), "--week", "2026-W35", "--strict"])
            self.assertEqual(code, cli.EXIT_WARNINGS)

    def test_strict_exits_zero_on_a_healthy_week(self):
        with TempRepo() as repo:
            for day in range(24, 29):
                repo.commit(
                    f"Add the parser stage number {day}",
                    {f"src/mod{day}.py": "x = 1\n", f"tests/test_mod{day}.py": "assert True\n"},
                    f"2026-08-{day}T10:00:00+00:00",
                )
            code, out, _ = run(["--project", str(repo.path), "--week", "2026-W35", "--strict"])
            self.assertEqual(code, 0, msg=out)


class ThresholdTests(unittest.TestCase):
    def test_large_commit_threshold_is_honoured(self):
        with TempRepo() as repo:
            repo.commit(
                "Add a generated fixture file",
                {"src/a.py": "\n".join(str(i) for i in range(50)) + "\n"},
                "2026-08-24T10:00:00+00:00",
            )
            _, strict_out, _ = run(
                [
                    "--project", str(repo.path),
                    "--week", "2026-W35",
                    "--large-commit-lines", "10",
                ]
            )
            self.assertIn("more than 10 lines", strict_out)

            _, loose_out, _ = run(
                [
                    "--project", str(repo.path),
                    "--week", "2026-W35",
                    "--large-commit-lines", "10000",
                ]
            )
            self.assertNotIn("more than", loose_out)

    def test_min_commits_threshold_is_honoured(self):
        with TempRepo() as repo:
            for day in (24, 25):
                repo.commit(
                    f"Add the loader stage {day}",
                    {f"src/a{day}.py": "x = 1\n"},
                    f"2026-08-{day}T10:00:00+00:00",
                )
            _, out, _ = run(
                ["--project", str(repo.path), "--week", "2026-W35", "--min-commits", "10"]
            )
            self.assertIn("Only 2 commits", out)


class ParserTests(unittest.TestCase):
    def test_defaults(self):
        args = cli.build_parser().parse_args([])
        self.assertIsNone(args.projects)
        self.assertIsNone(args.week)
        self.assertEqual(args.format, "text")
        self.assertEqual(args.weeks_ago, 0)
        self.assertFalse(args.strict)

    def test_project_flag_accumulates(self):
        args = cli.build_parser().parse_args(["-p", "a", "-p", "b"])
        self.assertEqual(args.projects, ["a", "b"])

    def test_unknown_format_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--format", "yaml"])


if __name__ == "__main__":
    unittest.main()
