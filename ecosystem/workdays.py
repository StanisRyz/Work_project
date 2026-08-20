"""Working-day arithmetic.

Deliberately calendar-free: Saturday and Sunday are the only non-working days,
and there is no holiday table to fall out of date. One function, so every
deadline rule in the project counts days the same way instead of each caller
re-deriving `weekday()` arithmetic.
"""

from datetime import timedelta


SATURDAY = 5


def add_working_days(start_date, working_days):
    """`start_date` plus `working_days` whole working days.

    Counting starts the day *after* `start_date` and skips weekends, so with
    two working days Monday → Wednesday, Thursday → Monday, and a submission
    made on a Saturday or a Sunday lands on the following Tuesday — the
    weekend is stepped over rather than counted.
    """
    if working_days < 0:
        raise ValueError('working_days must not be negative')
    result = start_date
    remaining = working_days
    while remaining > 0:
        result += timedelta(days=1)
        if result.weekday() < SATURDAY:
            remaining -= 1
    return result
