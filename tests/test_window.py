"""TimeWindow: the absolute-date constructor the UI needs (Prereq A)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from ttr.agents.window import TimeWindow

UTC = timezone.utc


def test_from_dates_spans_LOCAL_days_not_utc_days():
    # Calendar days are local (Europe/London). In BST that means the window opens
    # at 23:00 UTC the PREVIOUS day and closes at 22:59:59.999999 UTC on the end
    # day — anything else would slice an hour off each local day.
    w = TimeWindow.from_dates(date(2026, 6, 28), date(2026, 7, 16))
    assert w.start_utc == datetime(2026, 6, 27, 23, 0, 0, tzinfo=UTC)   # 00:00 BST on the 28th
    assert w.end_utc == datetime(2026, 7, 16, 22, 59, 59, 999999, tzinfo=UTC)
    # The local days are still exactly the ones asked for.
    days = w.days()
    assert days[0] == date(2026, 6, 28) and days[-1] == date(2026, 7, 16)
    # A late-evening LOCAL event on the end date is inside the window.
    assert w.start_utc <= datetime(2026, 7, 16, 21, 58, tzinfo=UTC) <= w.end_utc


def test_from_dates_single_day_covers_the_whole_local_day():
    w = TimeWindow.from_dates(date(2026, 7, 1), date(2026, 7, 1))
    assert w.days() == [date(2026, 7, 1)]
    # 00:30 LOCAL on 1 July is 23:30 UTC on 30 June — still inside this window,
    # and still counted under 1 July.
    just_after_local_midnight = datetime(2026, 6, 30, 23, 30, tzinfo=UTC)
    assert w.start_utc <= just_after_local_midnight <= w.end_utc
    assert w.local_date(just_after_local_midnight) == date(2026, 7, 1)


def test_local_date_bucketing_survives_the_midnight_boundary():
    # THE off-by-one this guards against: a capture at 00:30 BST is 23:30 UTC the
    # previous day. Bucketing by UTC date would file it a day early.
    w = TimeWindow.from_dates(date(2026, 7, 6), date(2026, 7, 6))
    midnight_thirty_bst = datetime(2026, 7, 5, 23, 30, tzinfo=UTC)
    assert midnight_thirty_bst.date() == date(2026, 7, 5)              # naive UTC reading
    assert w.local_date(midnight_thirty_bst) == date(2026, 7, 6)       # correct local day

    # ...and the reverse: 23:30 BST on the 6th is 22:30 UTC the same day.
    late_evening_bst = datetime(2026, 7, 6, 22, 30, tzinfo=UTC)
    assert w.local_date(late_evening_bst) == date(2026, 7, 6)
    assert w.start_utc <= midnight_thirty_bst <= w.end_utc
    assert w.start_utc <= late_evening_bst <= w.end_utc


def test_winter_window_uses_gmt_not_a_fixed_offset():
    # In GMT the local day and the UTC day coincide — a hardcoded +1h would break
    # this, zoneinfo does not.
    w = TimeWindow.from_dates(date(2026, 1, 15), date(2026, 1, 15))
    assert w.start_utc == datetime(2026, 1, 15, 0, 0, 0, tzinfo=UTC)
    assert w.local_date(datetime(2026, 1, 15, 23, 30, tzinfo=UTC)) == date(2026, 1, 15)


def test_day_basis_label_does_not_claim_utc():
    w = TimeWindow.from_dates(date(2026, 7, 6), date(2026, 7, 6))
    assert "Europe/London" in w.day_basis_label()
    assert "UTC" not in w.day_basis_label()      # the header must not mislabel the basis


def test_from_dates_rejects_reversed_range():
    with pytest.raises(ValueError):
        TimeWindow.from_dates(date(2026, 7, 16), date(2026, 6, 28))


def test_parse_relative_still_works():
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    w = TimeWindow.parse("7d", now=now)
    assert w.end_utc == now
    assert w.start_utc == datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


def test_previous_period_is_equal_length_and_immediately_preceding():
    # A 7-day window (Change 2 baseline): the prior period is the 7 days directly
    # before it, day-aligned and NON-overlapping.
    w = TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 14))
    prev = w.previous_period()
    assert prev.days()[0] == date(2026, 7, 1)          # equal length (7 days)
    assert prev.days()[-1] == date(2026, 7, 7)         # ends the day before the current window
    assert len(prev.days()) == len(w.days())
    assert prev.end_utc < w.start_utc                  # no overlap
