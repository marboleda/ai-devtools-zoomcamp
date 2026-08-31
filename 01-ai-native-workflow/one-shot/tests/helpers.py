"""Shared test helpers: synthetic commits and throwaway git repositories."""

from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from weekly_feedback.gitlog import Commit, FileChange


def make_commit(
    sha: str = "a" * 40,
    *,
    subject: str = "Add a reasonably descriptive change",
    body: str = "",
    when: str = "2026-08-24T10:00:00+00:00",
    author: str = "Marco",
    email: str = "marco@example.com",
    parents: tuple[str, ...] = ("b" * 40,),
    files: dict[str, tuple[int, int]] | None = None,
) -> Commit:
    """Build a :class:`Commit` without touching git.

    ``files`` maps a path to an ``(insertions, deletions)`` pair.
    """
    files = files if files is not None else {"src/app.py": (10, 2)}
    changes = tuple(
        FileChange(path=path, insertions=ins, deletions=dels)
        for path, (ins, dels) in files.items()
    )
    return Commit(
        sha=sha,
        author_name=author,
        author_email=email,
        authored_at=datetime.fromisoformat(when),
        subject=subject,
        body=body,
        parents=parents,
        changes=changes,
    )


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


class TempRepo:
    """A real git repository in a temporary directory."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Marco")
        self._git("config", "user.email", "marco@example.com")
        self._git("config", "commit.gpgsign", "false")

    def _git(self, *args: str, env_extra: dict[str, str] | None = None) -> str:
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        result = subprocess.run(
            ["git", *args],
            cwd=str(self.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout

    def write(self, relative: str, content: str) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, subject: str, files: dict[str, str], when: str) -> str:
        """Commit ``files`` with a fixed author/committer date (ISO 8601)."""
        for relative, content in files.items():
            self.write(relative, content)
        self._git("add", "-A")
        self._git(
            "commit",
            "-m",
            subject,
            env_extra={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
        )
        return self._git("rev-parse", "HEAD").strip()

    def cleanup(self) -> None:
        # Windows keeps handles on read-only objects inside .git; make them writable.
        for item in self.path.rglob("*"):
            try:
                item.chmod(0o700)
            except OSError:
                pass
        self._tmp.cleanup()

    def __enter__(self) -> "TempRepo":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()
