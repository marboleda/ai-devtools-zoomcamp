"""Classifying changed files as test, docs, config or source."""

from __future__ import annotations

import posixpath
import re

_TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "__tests__", "testing", "e2e"})

_TEST_BASENAME_RES = (
    re.compile(r"^test_.+\.py$"),
    re.compile(r"^.+_test\.(py|go|rb)$"),
    re.compile(r"^.+\.(test|spec)\.[jt]sx?$"),
    re.compile(r"^.+(Test|Tests|Spec)\.(java|kt|cs|scala)$"),
    re.compile(r"^conftest\.py$"),
)

_DOC_DIRS = frozenset({"doc", "docs", "documentation", "wiki"})
_DOC_EXTS = frozenset({".md", ".rst", ".adoc", ".txt"})
_DOC_STEMS = frozenset(
    {"readme", "changelog", "contributing", "license", "licence", "authors", "notice"}
)

_CONFIG_BASENAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
        "poetry.lock",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "makefile",
        "justfile",
        ".gitignore",
        ".editorconfig",
        ".pre-commit-config.yaml",
    }
)
_CONFIG_EXTS = frozenset({".toml", ".ini", ".cfg", ".yml", ".yaml", ".json", ".env", ".lock"})

_SOURCE_EXTS = frozenset(
    {
        ".py", ".pyi", ".ipynb",
        ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
        ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".cs",
        ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm",
        ".rb", ".php", ".swift", ".dart", ".ex", ".exs", ".erl",
        ".sh", ".bash", ".ps1", ".sql", ".r",
        ".html", ".css", ".scss", ".sass", ".vue", ".svelte",
    }
)


def _parts(path: str) -> list[str]:
    return [part for part in path.replace("\\", "/").split("/") if part and part != "."]


def _basename(path: str) -> str:
    return posixpath.basename(path.replace("\\", "/"))


def _ext(path: str) -> str:
    return posixpath.splitext(_basename(path))[1].lower()


def is_test(path: str) -> bool:
    parts = _parts(path)
    if any(part.lower() in _TEST_DIRS for part in parts[:-1]):
        return True
    basename = _basename(path)
    return any(pattern.match(basename) for pattern in _TEST_BASENAME_RES)


def is_docs(path: str) -> bool:
    if is_test(path):
        return False
    parts = _parts(path)
    if any(part.lower() in _DOC_DIRS for part in parts[:-1]):
        return True
    basename = _basename(path)
    stem = posixpath.splitext(basename)[0].lower()
    if stem in _DOC_STEMS:
        return True
    return _ext(path) in _DOC_EXTS and stem not in {"requirements", "requirements-dev"}


def is_config(path: str) -> bool:
    if is_test(path) or is_docs(path):
        return False
    basename = _basename(path).lower()
    if basename in _CONFIG_BASENAMES or basename.startswith("requirements"):
        return True
    parts = _parts(path)
    if parts and parts[0] in {".github", ".gitlab", ".circleci"}:
        return True
    return _ext(path) in _CONFIG_EXTS


def is_source(path: str) -> bool:
    """Production code: a known source extension that is not a test or doc."""
    if is_test(path) or is_docs(path):
        return False
    if _ext(path) not in _SOURCE_EXTS:
        return False
    return not is_config(path)


def kind(path: str) -> str:
    """One of ``test``, ``docs``, ``config``, ``source`` or ``other``."""
    if is_test(path):
        return "test"
    if is_docs(path):
        return "docs"
    if is_source(path):
        return "source"
    if is_config(path):
        return "config"
    return "other"
