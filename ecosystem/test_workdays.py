"""`ecosystem.workdays`: `add_working_days()` and `working_days_between()`."""

import datetime

from django.test import SimpleTestCase

from .workdays import add_working_days, working_days_between


MONDAY = datetime.date(2026, 10, 5)
FRIDAY = datetime.date(2026, 10, 9)
SATURDAY = datetime.date(2026, 10, 10)
SUNDAY = datetime.date(2026, 10, 11)
NEXT_MONDAY = datetime.date(2026, 10, 12)


class WorkingDaysBetweenTests(SimpleTestCase):
    def test_the_same_day_is_zero(self):
        for day in (MONDAY, FRIDAY, SATURDAY, SUNDAY):
            with self.subTest(day=day):
                self.assertEqual(working_days_between(day, day), 0)

    def test_weekends_are_stepped_over(self):
        self.assertEqual(working_days_between(FRIDAY, SATURDAY), 0)
        self.assertEqual(working_days_between(FRIDAY, SUNDAY), 0)
        self.assertEqual(working_days_between(FRIDAY, NEXT_MONDAY), 1)
        self.assertEqual(working_days_between(SATURDAY, NEXT_MONDAY), 1)
        self.assertEqual(working_days_between(MONDAY, FRIDAY), 4)
        self.assertEqual(working_days_between(MONDAY, NEXT_MONDAY), 5)
        self.assertEqual(working_days_between(MONDAY, MONDAY + datetime.timedelta(days=28)), 20)

    def test_a_negative_interval(self):
        self.assertEqual(working_days_between(NEXT_MONDAY, FRIDAY), -1)
        self.assertEqual(working_days_between(FRIDAY, MONDAY), -4)
        self.assertEqual(working_days_between(SUNDAY, SATURDAY), 0)

    def test_the_inverse_of_add_working_days(self):
        start = datetime.date(2026, 1, 1)
        for offset in range(0, 21):
            day = start + datetime.timedelta(days=offset)
            for days in (0, 1, 2, 3, 5, 9, 23):
                with self.subTest(day=day, days=days):
                    self.assertEqual(working_days_between(day, add_working_days(day, days)), days)
