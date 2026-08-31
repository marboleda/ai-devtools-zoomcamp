"""Turning a week of commits into statistics and written feedback."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .gitlog import Commit
from .weeks import Week

PRAISE = "praise"
SUGGEST = "suggest"
WARN = "warn"

_LEVEL_ORDER = {WARN: 0, SUGGEST: 1, PRAISE: 2}

_PR_NUMBER_RE = re.compile(r"Merge pull request #(\d+)", re.IGNORECASE)
_MERGE_REQUEST_RE = re.compile(r"See merge request [^!]*!(\d+)", re.IGNORECASE)

# Subjects that describe nothing on their own.
_VAGUE_SUBJECTS = frozenset(
    {
        "wip", "fix", "fixes", "fixed", "fixup", "update", "updates", "updated",
        "change", "changes", "changed", "misc", "stuff", "things", "tmp", "temp",
        "cleanup", "clean up", "refactor", "minor", "small fix", "quick fix",
        "test", "tests", "testing", "commit", "final", "done", "asdf", "foo",
        "more", "again", "oops", "typo", "bugfix", "patch", "revert",
    }
)


@dataclass(frozen=True)
class Thresholds:
    """Tunable limits for the feedback rules."""

    large_commit_lines: int = 800
    low_commit_count: int = 3
    steady_active_days: int = 4
    hotspot_commits: int = 3
    short_subject_chars: int = 12
    docs_worthy_source_files: int = 8


@dataclass(frozen=True)
class Finding:
    """One piece of feedback."""

    level: str
    code: str
    message: str
    details: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "details": list(self.details),
        }


@dataclass
class Stats:
    commits: int = 0
    merges: int = 0
    pull_requests: int = 0
    insertions: int = 0
    deletions: int = 0
    files_changed: int = 0
    active_days: int = 0
    authors: list[tuple[str, int]] = field(default_factory=list)
    kinds: dict[str, int] = field(default_factory=dict)

    @property
    def churn(self) -> int:
        return self.insertions + self.deletions

    def as_dict(self) -> dict:
        return {
            "commits": self.commits,
            "merges": self.merges,
            "pull_requests": self.pull_requests,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "churn": self.churn,
            "files_changed": self.files_changed,
            "active_days": self.active_days,
            "authors": [{"name": name, "commits": count} for name, count in self.authors],
            "files_by_kind": dict(self.kinds),
        }


@dataclass
class ProjectReport:
    name: str
    path: Path
    week: Week
    stats: Stats
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None

    @property
    def warnings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.level == WARN]

    def as_dict(self) -> dict:
        return {
            "project": self.name,
            "path": str(self.path),
            "week": self.week.label,
            "week_start": self.week.start.isoformat(),
            "week_end": self.week.end.isoformat(),
            "error": self.error,
            "stats": self.stats.as_dict(),
            "feedback": [finding.as_dict() for finding in self.findings],
        }


def _count_pull_requests(commits: list[Commit]) -> int:
    numbers: set[str] = set()
    merges_without_number = 0
    for commit in commits:
        if not commit.is_merge:
            continue
        text = f"{commit.subject}\n{commit.body}"
        found = _PR_NUMBER_RE.findall(text) + _MERGE_REQUEST_RE.findall(text)
        if found:
            numbers.update(found)
        else:
            merges_without_number += 1
    return len(numbers) if numbers else merges_without_number


def summarize(commits: list[Commit]) -> Stats:
    """Aggregate raw counts for a week's commits."""
    authors = Counter(commit.author_name or commit.author_email for commit in commits)
    touched = {path for commit in commits for path in commit.paths}
    kinds = Counter(paths.kind(path) for path in touched)

    return Stats(
        commits=len(commits),
        merges=sum(1 for commit in commits if commit.is_merge),
        pull_requests=_count_pull_requests(commits),
        insertions=sum(commit.insertions for commit in commits),
        deletions=sum(commit.deletions for commit in commits),
        files_changed=len(touched),
        active_days=len({commit.local_date for commit in commits}),
        authors=authors.most_common(),
        kinds=dict(sorted(kinds.items())),
    )


def _normalize_subject(subject: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", subject.lower()).strip()


def _is_vague(subject: str, limit: int) -> bool:
    normalized = _normalize_subject(subject)
    if not normalized:
        return True
    if normalized in _VAGUE_SUBJECTS:
        return True
    if normalized.startswith("wip"):
        return True
    return len(normalized) < limit


def _plural(count: int, noun: str, plural: str | None = None) -> str:
    word = noun if count == 1 else (plural or f"{noun}s")
    return f"{count} {word}"


def _verb(count: int, singular: str, plural: str) -> str:
    """Pick the verb form agreeing with ``count``."""
    return singular if count == 1 else plural


def _rule_activity(commits: list[Commit], stats: Stats, limits: Thresholds) -> list[Finding]:
    if stats.commits == 0:
        return [
            Finding(
                WARN,
                "no-activity",
                "No commits were authored this week -- the project looks stalled.",
            )
        ]
    if stats.commits < limits.low_commit_count:
        return [
            Finding(
                SUGGEST,
                "low-activity",
                f"Only {_plural(stats.commits, 'commit')} this week; "
                "steady weekly progress is easier to review than a late rush.",
            )
        ]
    return []


def _rule_cadence(commits: list[Commit], stats: Stats, limits: Thresholds) -> list[Finding]:
    if stats.commits == 0:
        return []
    if stats.active_days >= limits.steady_active_days:
        return [
            Finding(
                PRAISE,
                "steady-cadence",
                f"Steady cadence: work landed on {_plural(stats.active_days, 'separate day')} "
                "of the week.",
            )
        ]
    if stats.active_days == 1 and stats.commits >= limits.low_commit_count:
        day = commits[0].local_date.isoformat()
        return [
            Finding(
                SUGGEST,
                "bursty-cadence",
                f"All {_plural(stats.commits, 'commit')} landed on a single day ({day}); "
                "spreading work out makes progress easier to unblock.",
            )
        ]
    return []


def _rule_large_commits(commits: list[Commit], stats: Stats, limits: Thresholds) -> list[Finding]:
    large = [
        commit
        for commit in commits
        if not commit.is_merge and commit.churn > limits.large_commit_lines
    ]
    if not large:
        return []
    large.sort(key=lambda commit: commit.churn, reverse=True)
    details = tuple(
        f"{commit.short_sha} +{commit.insertions}/-{commit.deletions} "
        f"across {_plural(len(commit.changes), 'file')}: {commit.subject or '(no subject)'}"
        for commit in large[:5]
    )
    return [
        Finding(
            WARN,
            "large-commits",
            f"{_plural(len(large), 'commit')} changed more than "
            f"{limits.large_commit_lines} lines; smaller commits are far easier to review.",
            details,
        )
    ]


def _rule_tests(commits: list[Commit], stats: Stats, limits: Thresholds) -> list[Finding]:
    source_files = stats.kinds.get("source", 0)
    test_files = stats.kinds.get("test", 0)
    if source_files == 0:
        return []
    if test_files == 0:
        changed_sources = sorted(
            {path for commit in commits for path in commit.paths if paths.is_source(path)}
        )
        return [
            Finding(
                WARN,
                "no-tests-touched",
                f"{_plural(source_files, 'source file')} changed but no test file was touched.",
                tuple(changed_sources[:5]),
            )
        ]
    return [
        Finding(
            PRAISE,
            "tests-alongside-code",
            f"Tests moved with the code: {_plural(test_files, 'test file')} changed "
            f"alongside {_plural(source_files, 'source file')}.",
        )
    ]


def _rule_docs(commits: list[Commit], stats: Stats, limits: Thresholds) -> list[Finding]:
    source_files = stats.kinds.get("source", 0)
    if source_files < limits.docs_worthy_source_files:
        return []
    if stats.kinds.get("docs", 0) == 0:
        return [
            Finding(
                SUGGEST,
                "no-docs-touched",
                f"{_plural(source_files, 'source file')} changed with no update to the "
                "README or docs; a short note on what changed helps reviewers.",
            )
        ]
    return []


def _rule_subjects(commits: list[Commit], stats: Stats, limits: Thresholds) -> list[Finding]:
    vague = [
        commit
        for commit in commits
        if not commit.is_merge and _is_vague(commit.subject, limits.short_subject_chars)
    ]
    if not vague:
        return []
    details = tuple(
        f"{commit.short_sha} {commit.subject or '(empty subject)'}" for commit in vague[:5]
    )
    return [
        Finding(
            WARN if len(vague) * 2 >= stats.commits else SUGGEST,
            "vague-commit-messages",
            f"{_plural(len(vague), 'commit message')} "
            f"{_verb(len(vague), 'says', 'say')} little about the change; "
            "write what changed and why.",
            details,
        )
    ]


def _rule_hotspots(commits: list[Commit], stats: Stats, limits: Thresholds) -> list[Finding]:
    counter: Counter[str] = Counter()
    for commit in commits:
        counter.update(set(commit.paths))
    hotspots = [
        (path, count) for path, count in counter.most_common(5) if count >= limits.hotspot_commits
    ]
    if not hotspots:
        return []
    return [
        Finding(
            SUGGEST,
            "churn-hotspot",
            f"{_plural(len(hotspots), 'file')} "
            f"{_verb(len(hotspots), 'was', 'were')} revisited repeatedly; "
            "repeated edits often point at a design that wants splitting.",
            tuple(f"{path} ({_plural(count, 'commit')})" for path, count in hotspots),
        )
    ]


_RULES = (
    _rule_activity,
    _rule_cadence,
    _rule_large_commits,
    _rule_tests,
    _rule_docs,
    _rule_subjects,
    _rule_hotspots,
)


def review(commits: list[Commit], stats: Stats, limits: Thresholds | None = None) -> list[Finding]:
    """Run every feedback rule, warnings first."""
    limits = limits or Thresholds()
    findings: list[Finding] = []
    for rule in _RULES:
        findings.extend(rule(commits, stats, limits))
    findings.sort(key=lambda finding: _LEVEL_ORDER.get(finding.level, 99))
    return findings


def build_report(
    name: str,
    path: Path,
    week: Week,
    commits: list[Commit],
    limits: Thresholds | None = None,
) -> ProjectReport:
    stats = summarize(commits)
    return ProjectReport(
        name=name,
        path=path,
        week=week,
        stats=stats,
        findings=review(commits, stats, limits),
    )
