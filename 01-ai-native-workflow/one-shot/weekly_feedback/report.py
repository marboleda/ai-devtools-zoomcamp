"""Rendering week reports as text, markdown or JSON."""

from __future__ import annotations

import json

from .analyze import PRAISE, SUGGEST, WARN, ProjectReport
from .weeks import Week

_TEXT_MARKERS = {PRAISE: "+", SUGGEST: "~", WARN: "!"}
_MARKDOWN_MARKERS = {PRAISE: "**good**", SUGGEST: "**consider**", WARN: "**warning**"}


def _thousands(value: int) -> str:
    return f"{value:,}"


def _stat_line(report: ProjectReport) -> str:
    stats = report.stats
    parts = [
        f"commits {stats.commits}",
        f"files {stats.files_changed}",
        f"+{_thousands(stats.insertions)}/-{_thousands(stats.deletions)}",
        f"active days {stats.active_days}/7",
    ]
    if stats.merges:
        label = "PRs merged" if stats.pull_requests else "merges"
        parts.insert(1, f"{label} {stats.pull_requests or stats.merges}")
    return "   ".join(parts)


def _authors_line(report: ProjectReport) -> str:
    return ", ".join(f"{name} ({count})" for name, count in report.stats.authors)


def render_text(reports: list[ProjectReport], week: Week) -> str:
    lines = [f"Week {week.describe()}", ""]

    for report in reports:
        lines.append(f"{report.name}  [{report.path}]")
        if report.error:
            lines.append(f"  ! {report.error}")
            lines.append("")
            continue

        lines.append(f"  {_stat_line(report)}")
        if report.stats.authors:
            lines.append(f"  authors: {_authors_line(report)}")

        lines.append("  feedback:")
        if not report.findings:
            lines.append("    + Nothing to flag this week.")
        for finding in report.findings:
            marker = _TEXT_MARKERS.get(finding.level, "-")
            lines.append(f"    {marker} {finding.message}")
            for detail in finding.details:
                lines.append(f"        {detail}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_markdown(reports: list[ProjectReport], week: Week) -> str:
    lines = [f"# Weekly feedback -- {week.label}", "", f"_{week.describe()}_", ""]

    for report in reports:
        lines.append(f"## {report.name}")
        lines.append("")
        if report.error:
            lines.append(f"> **Error:** {report.error}")
            lines.append("")
            continue

        stats = report.stats
        lines.append(f"- **Commits:** {stats.commits}")
        if stats.merges:
            label = "PRs merged" if stats.pull_requests else "Merges"
            lines.append(f"- **{label}:** {stats.pull_requests or stats.merges}")
        lines.append(
            f"- **Churn:** +{_thousands(stats.insertions)} / -{_thousands(stats.deletions)} "
            f"across {stats.files_changed} files"
        )
        lines.append(f"- **Active days:** {stats.active_days}/7")
        if stats.authors:
            lines.append(f"- **Authors:** {_authors_line(report)}")
        lines.append("")

        lines.append("### Feedback")
        lines.append("")
        if not report.findings:
            lines.append("- Nothing to flag this week.")
        for finding in report.findings:
            marker = _MARKDOWN_MARKERS.get(finding.level, "note")
            lines.append(f"- {marker} -- {finding.message}")
            for detail in finding.details:
                lines.append(f"  - `{detail}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(reports: list[ProjectReport], week: Week) -> str:
    payload = {
        "week": week.label,
        "week_start": week.start.isoformat(),
        "week_end": week.end.isoformat(),
        "projects": [report.as_dict() for report in reports],
    }
    return json.dumps(payload, indent=2) + "\n"


RENDERERS = {
    "text": render_text,
    "markdown": render_markdown,
    "json": render_json,
}


def render(reports: list[ProjectReport], week: Week, fmt: str) -> str:
    try:
        renderer = RENDERERS[fmt]
    except KeyError:
        raise ValueError(f"unknown format {fmt!r}") from None
    return renderer(reports, week)
