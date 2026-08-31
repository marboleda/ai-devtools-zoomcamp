import unittest
from datetime import date

from weekly_feedback import weeks


class ParseTests(unittest.TestCase):
    def test_iso_week(self):
        week = weeks.parse("2026-W35")
        self.assertEqual(week.label, "2026-W35")
        self.assertEqual(week.start, date(2026, 8, 24))
        self.assertEqual(week.end, date(2026, 8, 30))

    def test_iso_week_without_dash_and_lowercase(self):
        self.assertEqual(weeks.parse("2026w35"), weeks.parse("2026-W35"))

    def test_single_digit_week_is_padded_in_label(self):
        self.assertEqual(weeks.parse("2026-W5").label, "2026-W05")

    def test_date_resolves_to_containing_week(self):
        # 2026-08-31 is a Monday, so it opens the *next* week.
        week = weeks.parse("2026-08-31")
        self.assertEqual(week.label, "2026-W36")
        self.assertEqual(week.start, date(2026, 8, 31))

    def test_current_and_last_aliases(self):
        today = date(2026, 8, 31)
        self.assertEqual(weeks.parse("current", today=today).label, "2026-W36")
        self.assertEqual(weeks.parse("last", today=today).label, "2026-W35")
        self.assertEqual(weeks.parse("this-week", today=today).label, "2026-W36")

    def test_week_starts_on_monday_for_any_day(self):
        week = weeks.week_of(date(2026, 8, 30))  # a Sunday
        self.assertEqual(week.start, date(2026, 8, 24))
        self.assertEqual(week.end, date(2026, 8, 30))

    def test_invalid_specs_raise(self):
        for spec in ["", "   ", "nonsense", "2026-W99", "2026-13-01", "W35"]:
            with self.subTest(spec=spec), self.assertRaises(weeks.WeekSpecError):
                weeks.parse(spec)

    def test_week_53_in_a_52_week_year_raises(self):
        # 2025 has 52 ISO weeks; 2026 has 53.
        with self.assertRaises(weeks.WeekSpecError):
            weeks.parse("2025-W53")

    def test_week_53_is_valid_in_a_53_week_year(self):
        self.assertEqual(weeks.parse("2026-W53").start, date(2026, 12, 28))
        self.assertEqual(weeks.parse("2020-W53").start, date(2020, 12, 28))


class WeekTests(unittest.TestCase):
    def test_contains(self):
        week = weeks.parse("2026-W35")
        self.assertTrue(week.contains(date(2026, 8, 24)))
        self.assertTrue(week.contains(date(2026, 8, 30)))
        self.assertFalse(week.contains(date(2026, 8, 23)))
        self.assertFalse(week.contains(date(2026, 8, 31)))

    def test_days_covers_seven_dates(self):
        self.assertEqual(len(weeks.parse("2026-W35").days), 7)

    def test_describe_mentions_both_bounds(self):
        text = weeks.parse("2026-W35").describe()
        self.assertIn("2026-08-24", text)
        self.assertIn("2026-08-30", text)


class ResolveTests(unittest.TestCase):
    def test_default_is_current_week(self):
        self.assertEqual(weeks.resolve(None, today=date(2026, 8, 31)).label, "2026-W36")

    def test_weeks_ago_shifts_back(self):
        self.assertEqual(weeks.resolve("2026-W35", 2).label, "2026-W33")

    def test_weeks_ago_crosses_year_boundary(self):
        self.assertEqual(weeks.resolve("2026-W02", 3).label, "2025-W51")

    def test_shift_forward(self):
        self.assertEqual(weeks.shift(weeks.parse("2026-W35"), 1).label, "2026-W36")


if __name__ == "__main__":
    unittest.main()
