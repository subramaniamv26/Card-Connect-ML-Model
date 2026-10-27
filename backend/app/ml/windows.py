"""Time windows used by training, aggregation and serving.

Definitions (all dates are Asia/Colombo local dates):
* data_cutoff        - the last calendar day that has transaction data (max transaction date).
* window_end         - data_cutoff + 1 day (exclusive upper bound for timestamps).
* history window     - [window_end - history_months calendar months, window_end).
* aggregation period - "last three months": the three calendar months up to and including the cutoff,
                       i.e. [window_end - 3 calendar months, window_end). With cutoff 2026-10-12 this is
                       2026-07-13 00:00:00 .. 2026-10-12 23:59:59, reported as start 2026-07-13, end 2026-10-12.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta


def add_months(d: date, months: int) -> date:
    """Calendar-month arithmetic, clamping the day (e.g. 31 Mar - 1 month = 28/29 Feb)."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class Windows:
    data_cutoff: date
    window_start: date
    window_end: date            # exclusive
    aggregation_start: date
    aggregation_end: date       # inclusive (= data_cutoff)

    @property
    def window_start_dt(self) -> datetime:
        return datetime.combine(self.window_start, datetime.min.time())

    @property
    def window_end_dt(self) -> datetime:
        return datetime.combine(self.window_end, datetime.min.time())

    @property
    def aggregation_start_dt(self) -> datetime:
        return datetime.combine(self.aggregation_start, datetime.min.time())

    def days_before_end(self, days: int) -> datetime:
        return self.window_end_dt - timedelta(days=days)


def compute_windows(data_cutoff: date, history_months: int = 12, aggregation_months: int = 3) -> Windows:
    end = data_cutoff + timedelta(days=1)
    return Windows(
        data_cutoff=data_cutoff,
        window_start=add_months(end, -history_months),
        window_end=end,
        aggregation_start=add_months(end, -aggregation_months),
        aggregation_end=data_cutoff,
    )
