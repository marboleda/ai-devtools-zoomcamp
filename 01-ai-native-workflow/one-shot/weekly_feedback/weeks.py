"""Resolving a week specification into a concrete ISO week."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

# "2026-W35", "2026W35", "2026-w5"
_ISO_WEEK_RE = re.compile(r"^(?P<year>\d{4})-?[Ww](?P<week>\d{1,2})$")
# "2026-08-31"
_ISO_DATE_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")

_CURRENT_ALIASES = frozenset({"current", "this", "now", "thisweek", "this-week"})
_LAST_ALIASES = frozenset({"last", "previous", "prev", "lastweek", "last-week"})


class WeekSpecError(ValueError):
    """Raised when a week specification cannot be understood."""


@dataclass(frozen=True)
class Week:
    """An ISO-8601 week, Monday through Sunday inclusive."""

    year: int
    week: int
    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.year}-W{self.week:02d}"

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=offset) for offset in range(7)]

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def describe(self) -> str:
        return f"{self.label} ({self.start.isoformat()} to {self.end.isoformat()})"


def week_of(day: date) -> Week:
    """The ISO week containing ``day``."""
    year, week, _weekday = day.isocalendar()
    return from_iso(year, week)


def from_iso(year: int, week: int) -> Week:
    """Build a :class:`Week` from an ISO year and week number."""
    try:
        start = date.fromisocalendar(year, week, 1)
    except ValueError as exc:  # week 53 in a 52-week year, week 0, week 54, ...
        raise WeekSpecError(f"{year}-W{week:02d} is not a valid ISO week: {exc}") from exc
    return Week(year=year, week=week, start=start, end=start + timedelta(days=6))


def shift(week: Week, weeks: int) -> Week:
    """The week ``weeks`` before (negative) or after (positive) ``week``."""
    return week_of(week.start + timedelta(weeks=weeks))


def parse(spec: str, *, today: date | None = None) -> Week:
    """Parse a week specification.

    Accepts an ISO week (``2026-W35``), an ISO date (``2026-08-31``, resolved to
    the week containing it), or the aliases ``current``/``last``.
    """
    today = today or date.today()
    cleaned = spec.strip()
    if not cleaned:
        raise WeekSpecError("week specification is empty")

    alias = cleaned.lower().replace("_", "-")
    if alias in _CURRENT_ALIASES:
        return week_of(today)
    if alias in _LAST_ALIASES:
        return week_of(today - timedelta(weeks=1))

    iso_week = _ISO_WEEK_RE.match(cleaned)
    if iso_week:
        return from_iso(int(iso_week["year"]), int(iso_week["week"]))

    iso_date = _ISO_DATE_RE.match(cleaned)
    if iso_date:
        try:
            day = date(int(iso_date["year"]), int(iso_date["month"]), int(iso_date["day"]))
        except ValueError as exc:
            raise WeekSpecError(f"{cleaned!r} is not a real calendar date: {exc}") from exc
        return week_of(day)

    raise WeekSpecError(
        f"cannot understand week {cleaned!r}; "
        "expected an ISO week like 2026-W35, a date like 2026-08-31, or 'current'/'last'"
    )


def resolve(spec: str | None, weeks_ago: int = 0, *, today: date | None = None) -> Week:
    """Resolve the CLI's ``--week``/``--weeks-ago`` pair into one week."""
    today = today or date.today()
    base = parse(spec, today=today) if spec else week_of(today)
    return shift(base, -weeks_ago) if weeks_ago else base
