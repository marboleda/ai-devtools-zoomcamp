import unittest
from datetime import date
from pathlib import Path

from weekly_feedback import gitlog
from weekly_feedback.gitlog import GitError

from .helpers import TempRepo

RS = "\x1e"
FS = "\x1f"


def record(
    sha="a" * 40,
    author="Marco",
    email="marco@example.com",
    when="2026-08-24T10:00:00+02:00",
    parents="b" * 40,
    subject="Add the thing",
    body="",
    numstat="",
):
    head = FS.join([sha, author, email, when, parents, subject, body]) + FS
    return RS + head + ("\n" + numstat if numstat else "")


class ParseLogTests(unittest.TestCase):
    def test_single_commit(self):
        commits = gitlog.parse_log(record(numstat="10\t2\tsrc/app.py"))
        self.assertEqual(len(commits), 1)
        commit = commits[0]
        self.assertEqual(commit.sha, "a" * 40)
        self.assertEqual(commit.short_sha, "aaaaaaaa")
        self.assertEqual(commit.author_name, "Marco")
        self.assertEqual(commit.subject, "Add the thing")
        self.assertEqual(commit.insertions, 10)
        self.assertEqual(commit.deletions, 2)
        self.assertEqual(commit.churn, 12)
        self.assertEqual(commit.paths, ("src/app.py",))
        self.assertFalse(commit.is_merge)

    def test_author_local_date_uses_the_authors_offset(self):
        # 00:30 on the 25th in +02:00 is still the 24th in UTC; the author's
        # own calendar date is what counts.
        commits = gitlog.parse_log(record(when="2026-08-25T00:30:00+02:00"))
        self.assertEqual(commits[0].local_date, date(2026, 8, 25))

    def test_multiple_files(self):
        commits = gitlog.parse_log(
            record(numstat="10\t2\tsrc/app.py\n0\t5\ttests/test_app.py")
        )
        commit = commits[0]
        self.assertEqual(len(commit.changes), 2)
        self.assertEqual(commit.insertions, 10)
        self.assertEqual(commit.deletions, 7)

    def test_binary_files_report_zero_churn(self):
        commits = gitlog.parse_log(record(numstat="-\t-\tassets/logo.png"))
        change = commits[0].changes[0]
        self.assertTrue(change.binary)
        self.assertEqual(change.churn, 0)

    def test_merge_commit_is_detected(self):
        commits = gitlog.parse_log(record(parents=f"{'b' * 40} {'c' * 40}"))
        self.assertTrue(commits[0].is_merge)
        self.assertEqual(len(commits[0].parents), 2)

    def test_multiline_body_is_kept_out_of_numstat(self):
        commits = gitlog.parse_log(
            record(body="Why: the old path broke.\n\nRefs #12", numstat="1\t1\tsrc/app.py")
        )
        commit = commits[0]
        self.assertIn("Refs #12", commit.body)
        self.assertEqual(commit.paths, ("src/app.py",))

    def test_several_records(self):
        text = record(sha="1" * 40, numstat="1\t0\ta.py") + record(sha="2" * 40, numstat="2\t0\tb.py")
        self.assertEqual(len(gitlog.parse_log(text)), 2)

    def test_empty_output(self):
        self.assertEqual(gitlog.parse_log(""), [])
        self.assertEqual(gitlog.parse_log("\n"), [])

    def test_malformed_records_are_skipped(self):
        self.assertEqual(gitlog.parse_log(RS + "not-a-record"), [])
        self.assertEqual(gitlog.parse_log(record(when="not-a-date")), [])

    def test_paths_with_spaces(self):
        commits = gitlog.parse_log(record(numstat="3\t1\tdocs/my notes.md"))
        self.assertEqual(commits[0].paths, ("docs/my notes.md",))


class RepoTests(unittest.TestCase):
    def test_non_repo_directory(self):
        with TempRepo() as repo:
            outside = repo.path / "plain"
            outside.mkdir()
            # A directory inside a repo is still in the repo; check a real non-repo instead.
            self.assertTrue(gitlog.is_repo(repo.path))
        self.assertFalse(gitlog.is_repo(Path("/definitely/not/here/at/all")))

    def test_collect_filters_by_week(self):
        with TempRepo() as repo:
            repo.commit("Add the loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            repo.commit("Add the parser", {"src/b.py": "y = 2\n"}, "2026-08-26T10:00:00+00:00")
            repo.commit("Add next week's work", {"src/c.py": "z = 3\n"}, "2026-09-02T10:00:00+00:00")

            commits = gitlog.collect(repo.path, date(2026, 8, 24), date(2026, 8, 30))
            subjects = [commit.subject for commit in commits]
            self.assertEqual(subjects, ["Add the loader", "Add the parser"])

    def test_collect_reads_churn_and_paths(self):
        with TempRepo() as repo:
            repo.commit(
                "Add three lines",
                {"src/a.py": "1\n2\n3\n"},
                "2026-08-24T10:00:00+00:00",
            )
            commits = gitlog.collect(repo.path, date(2026, 8, 24), date(2026, 8, 30))
            self.assertEqual(commits[0].insertions, 3)
            self.assertEqual(commits[0].paths, ("src/a.py",))

    def test_collect_returns_commits_in_chronological_order(self):
        with TempRepo() as repo:
            repo.commit("First change here", {"a.py": "1\n"}, "2026-08-24T10:00:00+00:00")
            repo.commit("Second change here", {"b.py": "1\n"}, "2026-08-25T10:00:00+00:00")
            commits = gitlog.collect(repo.path, date(2026, 8, 24), date(2026, 8, 30))
            self.assertEqual(
                [c.subject for c in commits], ["First change here", "Second change here"]
            )

    def test_empty_week_returns_nothing(self):
        with TempRepo() as repo:
            repo.commit("Add the loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            self.assertEqual(gitlog.collect(repo.path, date(2026, 7, 6), date(2026, 7, 12)), [])

    def test_repository_without_commits_is_an_empty_week(self):
        with TempRepo() as repo:
            self.assertEqual(gitlog.collect(repo.path, date(2026, 8, 24), date(2026, 8, 30)), [])

    def test_collect_on_a_non_repo_raises(self):
        with self.assertRaises(GitError):
            gitlog.collect(Path("/definitely/not/here/at/all"), date(2026, 8, 24), date(2026, 8, 30))

    def test_author_filter(self):
        with TempRepo() as repo:
            repo.commit("Add the loader", {"src/a.py": "x = 1\n"}, "2026-08-24T10:00:00+00:00")
            self.assertEqual(
                len(gitlog.collect(repo.path, date(2026, 8, 24), date(2026, 8, 30), author="Marco")),
                1,
            )
            self.assertEqual(
                gitlog.collect(repo.path, date(2026, 8, 24), date(2026, 8, 30), author="Nobody"),
                [],
            )

    def test_project_name_is_the_repo_directory(self):
        with TempRepo() as repo:
            self.assertEqual(gitlog.project_name(repo.path), repo.path.name)


if __name__ == "__main__":
    unittest.main()
