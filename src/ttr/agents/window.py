"""`TimeWindow` — a reporting window whose CALENDAR DAYS are local (§5.4).

Instants are stored in UTC; **calendar days are local** (``Europe/London`` by
default — the camera's wall clock). The distinction is not cosmetic. Capture time
is decoded from the camera's local clock, and the burned-in pixel overlay, the
animal's actual day, and the reader's mental model are all local. Bucketing a
capture by its **UTC** day would re-file anything between 00:00 and 00:59 local
(BST) onto the previous day — a smaller re-run of the send-time off-by-one this
project just corrected, arriving from the timezone instead of from delivery lag.

Window bounds and day bucketing therefore move together: a window of
``2026-07-06 .. 2026-07-06`` means local midnight to local 23:59:59.999999, and
every event inside it is counted under its **local** date. Splitting those two
(local bucketing over UTC bounds, or the reverse) would let an event be selected
by one calendar and counted under another, which the report's consistency guard
would then report as irreconcilable figures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

_SPEC = re.compile(r"^\s*(\d+)\s*([dhw])\s*$", re.IGNORECASE)
_UNIT = {"h": "hours", "d": "days", "w": "weeks"}

#: Timezone whose calendar days the report counts by. Must match the camera's
#: wall clock (``sources.capture_time.CAMERA_TIMEZONE``) — capture times are
#: decoded from that clock, so days are only meaningful in it. Kept as a separate
#: constant because agents may not import from ``sources/`` (CLAUDE.md §5).
REPORT_TIMEZONE = "Europe/London"


@dataclass(frozen=True)
class TimeWindow:
    start_utc: datetime
    end_utc: datetime
    tz: str = REPORT_TIMEZONE          # calendar-day semantics; instants stay UTC

    @property
    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def local_date(self, moment: datetime) -> date:
        """The LOCAL calendar day a UTC instant falls on — the single definition
        of 'which day does this event belong to', used for all bucketing."""
        return moment.astimezone(self._zone).date()

    def local_hour(self, moment: datetime) -> int:
        """The LOCAL hour-of-day (0–23) a UTC instant falls in — for the
        time-of-day distribution, on the same clock as day bucketing."""
        return moment.astimezone(self._zone).hour

    @classmethod
    def parse(cls, spec: str, *, now: datetime | None = None,
              tz: str = REPORT_TIMEZONE) -> "TimeWindow":
        """Parse a window like ``7d`` / ``24h`` / ``2w`` ending at ``now`` (UTC)."""
        m = _SPEC.match(spec)
        if not m:
            raise ValueError(f"invalid window spec: {spec!r} (expected e.g. '7d', '24h', '2w')")
        now = now or datetime.now(timezone.utc)
        delta = timedelta(**{_UNIT[m.group(2).lower()]: int(m.group(1))})
        return cls(start_utc=now - delta, end_utc=now, tz=tz)

    @classmethod
    def from_dates(cls, start: date, end: date, *, tz: str = REPORT_TIMEZONE) -> "TimeWindow":
        """Build an INCLUSIVE window from two LOCAL calendar dates: ``start`` at
        local 00:00:00, ``end`` at local 23:59:59.999999, both converted to UTC
        instants. A naive UTC midnight-to-midnight range would both drop the end
        day and shift every boundary by the local offset."""
        if start > end:
            raise ValueError(f"start date {start.isoformat()} is after end date {end.isoformat()}")
        zone = ZoneInfo(tz)
        return cls(
            start_utc=datetime.combine(start, time.min, tzinfo=zone).astimezone(timezone.utc),
            end_utc=datetime.combine(end, time.max, tzinfo=zone).astimezone(timezone.utc),
            tz=tz,
        )

    def days(self) -> list[date]:
        """Every LOCAL calendar day the window touches, inclusive of both ends."""
        first, last = self.local_date(self.start_utc), self.local_date(self.end_utc)
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    def previous_period(self) -> "TimeWindow":
        """The equal-length calendar period IMMEDIATELY preceding this one — the
        period-over-period baseline (Change 2). If this window spans L days ending
        the day before ``start``, the baseline is the L days before that, day
        aligned and NON-overlapping. Day-granular by construction, matching the
        send-time caveat (comparisons are only ever honest at day granularity)."""
        span = self.days()
        length = len(span)
        prior_end = span[0] - timedelta(days=1)
        prior_start = prior_end - timedelta(days=length - 1)
        return TimeWindow.from_dates(prior_start, prior_end, tz=self.tz)

    def label(self) -> str:
        return (f"{self.local_date(self.start_utc).isoformat()} to "
                f"{self.local_date(self.end_utc).isoformat()} ({self.day_basis_label()})")

    def day_basis_label(self) -> str:
        """How a column of dates should be described — the zone days are counted
        in, so a header never claims 'UTC' while bucketing locally."""
        return f"local dates, {self.tz}"
