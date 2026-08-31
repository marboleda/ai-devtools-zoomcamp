import unittest

from weekly_feedback import analyze
from weekly_feedback.analyze import SUGGEST, WARN, Thresholds, summarize
from weekly_feedback.weeks import parse as parse_week

from .helpers import make_commit


def codes(findings):
    return {finding.code for finding in findings}


def find(findings, code):
    for finding in findings:
        if finding.code == code:
            return finding
    raise AssertionError(f"no finding with code {code!r} in {sorted(codes(findings))}")


def review(commits, **kwargs):
    limits = Thresholds(**kwargs) if kwargs else None
    return analyze.review(commits, summarize(commits), limits)


class SummarizeTests(unittest.TestCase):
    def test_empty_week(self):
        stats = summarize([])
        self.assertEqual(stats.commits, 0)
        self.assertEqual(stats.churn, 0)
        self.assertEqual(stats.authors, [])

    def test_counts_are_aggregated(self):
        commits = [
            make_commit(sha="1" * 40, files={"src/a.py": (10, 5)}, when="2026-08-24T09:00:00+00:00"),
            make_commit(sha="2" * 40, files={"src/b.py": (3, 1)}, when="2026-08-25T09:00:00+00:00"),
        ]
        stats = summarize(commits)
        self.assertEqual(stats.commits, 2)
        self.assertEqual(stats.insertions, 13)
        self.assertEqual(stats.deletions, 6)
        self.assertEqual(stats.churn, 19)
        self.assertEqual(stats.files_changed, 2)
        self.assertEqual(stats.active_days, 2)

    def test_the_same_file_is_counted_once(self):
        commits = [
            make_commit(sha="1" * 40, files={"src/a.py": (10, 0)}),
            make_commit(sha="2" * 40, files={"src/a.py": (4, 0)}),
        ]
        self.assertEqual(summarize(commits).files_changed, 1)

    def test_authors_are_ranked(self):
        commits = [
            make_commit(sha="1" * 40, author="Marco"),
            make_commit(sha="2" * 40, author="Dana"),
            make_commit(sha="3" * 40, author="Marco"),
        ]
        self.assertEqual(summarize(commits).authors, [("Marco", 2), ("Dana", 1)])

    def test_pull_requests_are_read_from_merge_subjects(self):
        commits = [
            make_commit(
                sha="1" * 40,
                subject="Merge pull request #12 from feature/x",
                parents=("a" * 40, "b" * 40),
                files={},
            ),
            make_commit(
                sha="2" * 40,
                subject="Merge pull request #12 from feature/x",
                parents=("a" * 40, "b" * 40),
                files={},
            ),
            make_commit(
                sha="3" * 40,
                subject="Merge pull request #13 from feature/y",
                parents=("a" * 40, "b" * 40),
                files={},
            ),
        ]
        stats = summarize(commits)
        self.assertEqual(stats.merges, 3)
        self.assertEqual(stats.pull_requests, 2)  # distinct PR numbers

    def test_merges_without_pr_numbers_fall_back_to_merge_count(self):
        commits = [
            make_commit(sha="1" * 40, subject="Merge branch 'main'", parents=("a" * 40, "b" * 40)),
        ]
        self.assertEqual(summarize(commits).pull_requests, 1)


class ActivityRuleTests(unittest.TestCase):
    def test_no_commits_warns(self):
        findings = review([])
        self.assertIn("no-activity", codes(findings))
        self.assertEqual(find(findings, "no-activity").level, WARN)

    def test_no_commits_does_not_also_emit_cadence_noise(self):
        self.assertEqual(codes(review([])), {"no-activity"})

    def test_low_activity_suggests(self):
        findings = review([make_commit()])
        self.assertEqual(find(findings, "low-activity").level, SUGGEST)

    def test_healthy_week_has_no_activity_complaint(self):
        commits = [
            make_commit(sha=str(i) * 40, when=f"2026-08-2{4 + i}T09:00:00+00:00")
            for i in range(4)
        ]
        self.assertNotIn("low-activity", codes(review(commits)))
        self.assertNotIn("no-activity", codes(review(commits)))


class CadenceRuleTests(unittest.TestCase):
    def test_four_active_days_is_praised(self):
        commits = [
            make_commit(sha=str(i) * 40, when=f"2026-08-2{4 + i}T09:00:00+00:00")
            for i in range(4)
        ]
        self.assertEqual(find(review(commits), "steady-cadence").level, analyze.PRAISE)

    def test_all_work_on_one_day_is_flagged(self):
        commits = [
            make_commit(sha=str(i) * 40, when="2026-08-24T09:00:00+00:00") for i in range(5)
        ]
        finding = find(review(commits), "bursty-cadence")
        self.assertEqual(finding.level, SUGGEST)
        self.assertIn("2026-08-24", finding.message)


class LargeCommitRuleTests(unittest.TestCase):
    def test_large_commit_is_flagged_with_detail(self):
        commits = [
            make_commit(sha="1" * 40, subject="Rewrite storage", files={"src/db.py": (900, 100)}),
            make_commit(sha="2" * 40, files={"src/a.py": (5, 1)}),
            make_commit(sha="3" * 40, files={"src/b.py": (5, 1)}),
        ]
        finding = find(review(commits), "large-commits")
        self.assertEqual(finding.level, WARN)
        self.assertTrue(any("+900/-100" in detail for detail in finding.details))

    def test_threshold_is_configurable(self):
        commits = [make_commit(files={"src/db.py": (100, 0)})]
        self.assertIn("large-commits", codes(review(commits, large_commit_lines=50)))
        self.assertNotIn("large-commits", codes(review(commits, large_commit_lines=500)))

    def test_merge_commits_are_never_large(self):
        commits = [
            make_commit(
                sha="1" * 40,
                subject="Merge pull request #1 from x",
                parents=("a" * 40, "b" * 40),
                files={"src/db.py": (5000, 5000)},
            )
        ]
        self.assertNotIn("large-commits", codes(review(commits)))


class TestCoverageRuleTests(unittest.TestCase):
    def test_source_without_tests_warns(self):
        commits = [make_commit(files={"src/a.py": (20, 0)})]
        finding = find(review(commits), "no-tests-touched")
        self.assertEqual(finding.level, WARN)
        self.assertIn("src/a.py", finding.details)

    def test_source_with_tests_is_praised(self):
        commits = [make_commit(files={"src/a.py": (20, 0), "tests/test_a.py": (30, 0)})]
        self.assertEqual(find(review(commits), "tests-alongside-code").level, analyze.PRAISE)

    def test_docs_only_week_is_not_scolded_about_tests(self):
        commits = [make_commit(files={"README.md": (20, 0)})]
        self.assertNotIn("no-tests-touched", codes(review(commits)))


class DocsRuleTests(unittest.TestCase):
    def test_many_source_files_without_docs_suggests(self):
        files = {f"src/mod{i}.py": (10, 0) for i in range(9)}
        files["tests/test_all.py"] = (10, 0)
        commits = [make_commit(files=files)]
        self.assertEqual(find(review(commits), "no-docs-touched").level, SUGGEST)

    def test_docs_touched_clears_the_rule(self):
        files = {f"src/mod{i}.py": (10, 0) for i in range(9)}
        files["README.md"] = (5, 0)
        self.assertNotIn("no-docs-touched", codes(review([make_commit(files=files)])))

    def test_small_change_is_not_asked_for_docs(self):
        commits = [make_commit(files={"src/a.py": (5, 0)})]
        self.assertNotIn("no-docs-touched", codes(review(commits)))


class SubjectRuleTests(unittest.TestCase):
    def test_vague_subjects_are_flagged(self):
        commits = [
            make_commit(sha="1" * 40, subject="wip"),
            make_commit(sha="2" * 40, subject="fix"),
            make_commit(sha="3" * 40, subject="Update"),
            make_commit(sha="4" * 40, subject="stuff"),
        ]
        finding = find(review(commits), "vague-commit-messages")
        self.assertEqual(finding.level, WARN)  # majority of commits
        self.assertIn("4 commit messages", finding.message)

    def test_descriptive_subjects_pass(self):
        commits = [
            make_commit(sha="1" * 40, subject="Add retry handling to the upload client"),
            make_commit(sha="2" * 40, subject="Fix off-by-one in the week boundary check"),
        ]
        self.assertNotIn("vague-commit-messages", codes(review(commits)))

    def test_a_single_bad_message_among_many_only_suggests(self):
        commits = [make_commit(sha="0" * 40, subject="wip")]
        commits += [
            make_commit(sha=str(i) * 40, subject=f"Add feature number {i} to the parser")
            for i in range(1, 6)
        ]
        self.assertEqual(find(review(commits), "vague-commit-messages").level, SUGGEST)

    def test_merge_subjects_are_exempt(self):
        commits = [
            make_commit(sha="1" * 40, subject="Merge branch 'main'", parents=("a" * 40, "b" * 40))
        ]
        self.assertNotIn("vague-commit-messages", codes(review(commits)))

    def test_singular_message_reads_grammatically(self):
        commits = [make_commit(sha="0" * 40, subject="wip")]
        commits += [
            make_commit(sha=str(i) * 40, subject=f"Add feature number {i} to the parser")
            for i in range(1, 6)
        ]
        finding = find(review(commits), "vague-commit-messages")
        self.assertIn("1 commit message says little", finding.message)

    def test_empty_subject_is_vague(self):
        self.assertIn("vague-commit-messages", codes(review([make_commit(subject="")])))


class HotspotRuleTests(unittest.TestCase):
    def test_repeatedly_touched_file_is_flagged(self):
        commits = [
            make_commit(sha=str(i) * 40, files={"src/god_object.py": (30, 10)})
            for i in range(4)
        ]
        finding = find(review(commits), "churn-hotspot")
        self.assertTrue(any("src/god_object.py" in detail for detail in finding.details))
        self.assertTrue(any("4 commits" in detail for detail in finding.details))

    def test_singular_hotspot_reads_grammatically(self):
        commits = [
            make_commit(sha=str(i) * 40, files={"src/god_object.py": (30, 10)})
            for i in range(4)
        ]
        self.assertIn("1 file was revisited", find(review(commits), "churn-hotspot").message)

    def test_a_file_touched_twice_is_not_a_hotspot(self):
        commits = [make_commit(sha=str(i) * 40, files={"src/a.py": (5, 0)}) for i in range(2)]
        self.assertNotIn("churn-hotspot", codes(review(commits)))


class OrderingTests(unittest.TestCase):
    def test_warnings_come_first(self):
        commits = [make_commit(sha="1" * 40, files={"src/db.py": (900, 100)})]
        levels = [finding.level for finding in review(commits)]
        self.assertEqual(levels, sorted(levels, key=lambda level: {"warn": 0, "suggest": 1, "praise": 2}[level]))


class ReportTests(unittest.TestCase):
    def test_build_report_carries_identity_and_serializes(self):
        from pathlib import Path

        week = parse_week("2026-W35")
        report = analyze.build_report("my-app", Path("/tmp/my-app"), week, [make_commit()])
        self.assertEqual(report.name, "my-app")
        self.assertEqual(report.week.label, "2026-W35")

        payload = report.as_dict()
        self.assertEqual(payload["project"], "my-app")
        self.assertEqual(payload["week_start"], "2026-08-24")
        self.assertEqual(payload["stats"]["commits"], 1)
        self.assertIsNone(payload["error"])
        self.assertTrue(all("level" in item for item in payload["feedback"]))

    def test_warnings_property_filters(self):
        week = parse_week("2026-W35")
        report = analyze.build_report("empty", __import__("pathlib").Path("."), week, [])
        self.assertEqual([finding.code for finding in report.warnings], ["no-activity"])


if __name__ == "__main__":
    unittest.main()
