import json
import unittest
from pathlib import Path

from weekly_feedback import report
from weekly_feedback.analyze import Stats, build_report
from weekly_feedback.analyze import ProjectReport
from weekly_feedback.weeks import parse as parse_week

from .helpers import make_commit


WEEK = parse_week("2026-W35")


def sample(commits=None, name="my-app"):
    commits = commits if commits is not None else [make_commit()]
    return build_report(name, Path("/tmp") / name, WEEK, commits)


class TextTests(unittest.TestCase):
    def test_header_and_stats(self):
        text = report.render_text([sample()], WEEK)
        self.assertIn("Week 2026-W35", text)
        self.assertIn("my-app", text)
        self.assertIn("commits 1", text)
        self.assertIn("+10/-2", text)
        self.assertIn("feedback:", text)

    def test_output_is_ascii_only(self):
        commits = [make_commit(sha=str(i) * 40, files={"src/db.py": (900, 100)}) for i in range(3)]
        text = report.render_text([sample(commits)], WEEK)
        text.encode("ascii")  # raises if a stray unicode marker sneaks in

    def test_empty_week_says_so(self):
        text = report.render_text([sample([])], WEEK)
        self.assertIn("commits 0", text)
        self.assertIn("stalled", text)

    def test_details_are_indented_under_their_finding(self):
        commits = [make_commit(files={"src/a.py": (20, 0)})]
        lines = report.render_text([sample(commits)], WEEK).splitlines()
        message_index = next(i for i, line in enumerate(lines) if "no test file" in line)
        self.assertTrue(lines[message_index + 1].startswith("        src/a.py"))

    def test_error_is_reported_instead_of_stats(self):
        broken = ProjectReport(
            name="gone", path=Path("/nope"), week=WEEK, stats=Stats(), error="not a git repository"
        )
        text = report.render_text([broken], WEEK)
        self.assertIn("not a git repository", text)
        self.assertNotIn("feedback:", text)

    def test_several_projects_are_all_present(self):
        text = report.render_text([sample(name="api"), sample(name="web")], WEEK)
        self.assertIn("api", text)
        self.assertIn("web", text)

    def test_thousands_separator(self):
        commits = [make_commit(files={"src/a.py": (12345, 0)})]
        self.assertIn("+12,345", report.render_text([sample(commits)], WEEK))


class MarkdownTests(unittest.TestCase):
    def test_structure(self):
        text = report.render_markdown([sample()], WEEK)
        self.assertIn("# Weekly feedback -- 2026-W35", text)
        self.assertIn("## my-app", text)
        self.assertIn("### Feedback", text)
        self.assertIn("- **Commits:** 1", text)

    def test_details_are_code_spans(self):
        commits = [make_commit(files={"src/a.py": (20, 0)})]
        self.assertIn("  - `src/a.py`", report.render_markdown([sample(commits)], WEEK))


class JsonTests(unittest.TestCase):
    def test_payload_shape(self):
        payload = json.loads(report.render_json([sample()], WEEK))
        self.assertEqual(payload["week"], "2026-W35")
        self.assertEqual(payload["week_start"], "2026-08-24")
        self.assertEqual(payload["week_end"], "2026-08-30")
        self.assertEqual(len(payload["projects"]), 1)

        project = payload["projects"][0]
        self.assertEqual(project["project"], "my-app")
        self.assertEqual(project["stats"]["commits"], 1)
        self.assertEqual(project["stats"]["insertions"], 10)
        self.assertIn("files_by_kind", project["stats"])
        self.assertIsInstance(project["feedback"], list)

    def test_json_is_valid_for_an_error_report(self):
        broken = ProjectReport(
            name="gone", path=Path("/nope"), week=WEEK, stats=Stats(), error="boom"
        )
        payload = json.loads(report.render_json([broken], WEEK))
        self.assertEqual(payload["projects"][0]["error"], "boom")


class DispatchTests(unittest.TestCase):
    def test_render_dispatches_by_name(self):
        for fmt in ["text", "markdown", "json"]:
            with self.subTest(fmt=fmt):
                self.assertTrue(report.render([sample()], WEEK, fmt).strip())

    def test_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            report.render([sample()], WEEK, "yaml")


if __name__ == "__main__":
    unittest.main()
