"""Command-line entry point for weekly-feedback."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, gitlog, report
from .analyze import ProjectReport, Stats, Thresholds, build_report
from .gitlog import GitError
from .weeks import WeekSpecError, resolve

EXIT_OK = 0
EXIT_WARNINGS = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weekly-feedback",
        description="Generate weekly feedback for projects from their git activity.",
        epilog=(
            "examples:\n"
            "  weekly-feedback                                 # this week, current repo\n"
            "  weekly-feedback --week last                      # previous week\n"
            "  weekly-feedback --project ../api --project ../web --week 2026-W35\n"
            "  weekly-feedback --format markdown --out reports/week.md\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-p",
        "--project",
        dest="projects",
        action="append",
        metavar="PATH",
        help="path to a git repository; repeat for several projects (default: .)",
    )
    parser.add_argument(
        "-w",
        "--week",
        metavar="SPEC",
        help="ISO week (2026-W35), a date within the week (2026-08-31), "
        "or 'current'/'last' (default: current)",
    )
    parser.add_argument(
        "--weeks-ago",
        type=int,
        default=0,
        metavar="N",
        help="shift the selected week N weeks into the past",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=sorted(report.RENDERERS),
        default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "-o",
        "--out",
        metavar="FILE",
        help="write the report to FILE instead of stdout",
    )
    parser.add_argument(
        "--author",
        metavar="PATTERN",
        help="only consider commits whose author matches PATTERN (git --author)",
    )
    parser.add_argument(
        "--all-branches",
        action="store_true",
        help="consider commits on every branch, not just the checked-out one",
    )
    parser.add_argument(
        "--large-commit-lines",
        type=int,
        default=Thresholds.large_commit_lines,
        metavar="N",
        help=f"flag commits changing more than N lines (default: {Thresholds.large_commit_lines})",
    )
    parser.add_argument(
        "--min-commits",
        type=int,
        default=Thresholds.low_commit_count,
        metavar="N",
        help=f"flag weeks with fewer than N commits (default: {Thresholds.low_commit_count})",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when any project has a warning (useful in CI or cron)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _analyze_project(
    raw_path: str,
    week,
    limits: Thresholds,
    *,
    all_branches: bool,
    author: str | None,
) -> ProjectReport:
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return ProjectReport(
            name=path.name or raw_path,
            path=path,
            week=week,
            stats=Stats(),
            error=f"path does not exist: {raw_path}",
        )

    try:
        commits = gitlog.collect(
            resolved,
            week.start,
            week.end,
            all_branches=all_branches,
            author=author,
        )
    except GitError as exc:
        return ProjectReport(
            name=gitlog.project_name(resolved) or resolved.name,
            path=resolved,
            week=week,
            stats=Stats(),
            error=str(exc),
        )

    return build_report(
        name=gitlog.project_name(resolved),
        path=resolved,
        week=week,
        commits=commits,
        limits=limits,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        week = resolve(args.week, args.weeks_ago)
    except WeekSpecError as exc:
        parser.error(str(exc))
        return EXIT_ERROR  # unreachable; parser.error exits

    limits = Thresholds(
        large_commit_lines=args.large_commit_lines,
        low_commit_count=args.min_commits,
    )

    reports = [
        _analyze_project(
            raw_path,
            week,
            limits,
            all_branches=args.all_branches,
            author=args.author,
        )
        for raw_path in (args.projects or ["."])
    ]

    rendered = report.render(reports, week, args.format)

    if args.out:
        out_path = Path(args.out).expanduser()
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"weekly-feedback: cannot write {out_path}: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"wrote {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)

    if any(item.error for item in reports):
        return EXIT_ERROR
    if args.strict and any(item.warnings for item in reports):
        return EXIT_WARNINGS
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
