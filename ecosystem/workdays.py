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


def working_days_between(start_date, end_date):
    """How many working days lie after `start_date` up to `end_date` inclusive.

    The same counting as `add_working_days()` — `add_working_days(d, n)` is
    `n` working days after `d` — so `working_days_between(d,
    add_working_days(d, n)) == n`. The same day is 0; Friday → Monday is 1,
    Friday → Saturday 0. Negative when `end_date` is before `start_date`: the
    working days after `end_date` up to `start_date` inclusive, with a minus.
    Counted by whole weeks plus the remainder, so a long span costs no loop
    over its days.
    """
    if end_date < start_date:
        return -working_days_between(end_date, start_date)
    days = (end_date - start_date).days
    weeks, rest = divmod(days, 7)
    count = weeks * 5
    weekday = start_date.weekday()
    for offset in range(1, rest + 1):
        if (weekday + offset) % 7 < SATURDAY:
            count += 1
    return count
