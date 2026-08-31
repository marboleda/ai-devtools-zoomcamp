"""Reading commit activity out of a git repository.

Commits are attributed to a week by their *author* date as the author saw it --
the date part of ``%aI``, in the author's own UTC offset. That keeps the answer
to "which week was this done in?" independent of the machine running the report.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

# git's --since/--until are coarse (and filter on commit date, not author date),
# so they are used only to bound the log; the exact window is applied in Python.
_QUERY_BUFFER_DAYS = 7

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_PRETTY_FORMAT = (
    f"{_RECORD_SEP}%H{_FIELD_SEP}%an{_FIELD_SEP}%ae{_FIELD_SEP}"
    f"%aI{_FIELD_SEP}%P{_FIELD_SEP}%s{_FIELD_SEP}%b{_FIELD_SEP}"
)


class GitError(RuntimeError):
    """Raised when git cannot be run, or a path is not a usable repository."""


@dataclass(frozen=True)
class FileChange:
    path: str
    insertions: int
    deletions: int
    binary: bool = False

    @property
    def churn(self) -> int:
        return self.insertions + self.deletions


@dataclass(frozen=True)
class Commit:
    sha: str
    author_name: str
    author_email: str
    authored_at: datetime
    subject: str
    body: str
    parents: tuple[str, ...] = ()
    changes: tuple[FileChange, ...] = field(default=())

    @property
    def short_sha(self) -> str:
        return self.sha[:8]

    @property
    def local_date(self) -> date:
        """The calendar date in the author's own timezone."""
        return self.authored_at.date()

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def insertions(self) -> int:
        return sum(change.insertions for change in self.changes)

    @property
    def deletions(self) -> int:
        return sum(change.deletions for change in self.changes)

    @property
    def churn(self) -> int:
        return self.insertions + self.deletions

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(change.path for change in self.changes)


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:  # git missing from PATH
        raise GitError("git executable not found on PATH") from exc
    except OSError as exc:
        raise GitError(f"could not run git in {cwd}: {exc}") from exc


def is_repo(path: Path) -> bool:
    if not path.is_dir():
        return False
    result = _run_git(["rev-parse", "--git-dir"], cwd=path)
    return result.returncode == 0


def toplevel(path: Path) -> Path:
    """The repository root containing ``path``."""
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=path)
    if result.returncode != 0:
        return path
    root = result.stdout.strip()
    return Path(root) if root else path


def project_name(path: Path) -> str:
    """A human label for the project: the repository directory name."""
    root = toplevel(path)
    name = root.name
    return name or str(root)


def _parse_numstat(line: str) -> FileChange | None:
    fields = line.split("\t")
    if len(fields) < 3:
        return None
    raw_insertions, raw_deletions, path = fields[0], fields[1], "\t".join(fields[2:])
    path = path.strip()
    if not path:
        return None
    if raw_insertions == "-" or raw_deletions == "-":  # binary file
        return FileChange(path=path, insertions=0, deletions=0, binary=True)
    try:
        return FileChange(path=path, insertions=int(raw_insertions), deletions=int(raw_deletions))
    except ValueError:
        return None


def _parse_record(record: str) -> Commit | None:
    parts = record.split(_FIELD_SEP)
    if len(parts) < 8:
        return None
    sha, author_name, author_email, authored, parents, subject, body = parts[:7]
    if not sha.strip():
        return None

    try:
        authored_at = datetime.fromisoformat(authored.strip())
    except ValueError:
        return None

    changes: list[FileChange] = []
    for line in _FIELD_SEP.join(parts[7:]).splitlines():
        if not line.strip():
            continue
        change = _parse_numstat(line)
        if change is not None:
            changes.append(change)

    return Commit(
        sha=sha.strip(),
        author_name=author_name.strip(),
        author_email=author_email.strip(),
        authored_at=authored_at,
        subject=subject.strip(),
        body=body.strip(),
        parents=tuple(p for p in parents.split() if p),
        changes=tuple(changes),
    )


def parse_log(output: str) -> list[Commit]:
    """Parse the output of the ``git log`` invocation built by :func:`collect`."""
    commits = []
    for record in output.split(_RECORD_SEP):
        if not record.strip():
            continue
        commit = _parse_record(record)
        if commit is not None:
            commits.append(commit)
    return commits


def collect(
    path: Path,
    start: date,
    end: date,
    *,
    all_branches: bool = False,
    author: str | None = None,
) -> list[Commit]:
    """Commits authored in ``[start, end]`` (inclusive) in the repo at ``path``.

    Merge commits carry no per-file numbers, which is deliberate: their content
    is already counted on the commits being merged in.
    """
    if not is_repo(path):
        raise GitError(f"{path} is not a git repository")

    args = [
        "log",
        "--numstat",
        "--no-renames",
        f"--pretty=format:{_PRETTY_FORMAT}",
        f"--since={(start - timedelta(days=_QUERY_BUFFER_DAYS)).isoformat()}",
        f"--until={(end + timedelta(days=_QUERY_BUFFER_DAYS + 1)).isoformat()}",
    ]
    if all_branches:
        args.append("--all")
    if author:
        args.append(f"--author={author}")

    result = _run_git(args, cwd=path)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        # A repository with no commits yet is an empty week, not a failure.
        if "does not have any commits yet" in stderr or "bad default revision" in stderr:
            return []
        raise GitError(f"git log failed in {path}: {stderr or 'unknown error'}")

    commits = parse_log(result.stdout)
    within = [commit for commit in commits if start <= commit.local_date <= end]
    within.sort(key=lambda commit: commit.authored_at)
    return within
