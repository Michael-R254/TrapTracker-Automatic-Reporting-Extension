"""Capture-time decoding (Decision 3, amended).

Covers the three cases that matter: the BST->UTC conversion done with real DST
rules, the honest fallback when a filename carries no capture block, and the
PHANTOM ZERO — the day that reported no activity because a delivery outage filed
20 captures under the following day's send timestamp.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from ttr.agents.report import ReportGeneratorAgent
from ttr.agents.retrieval import RetrievalAgent
from ttr.agents.window import TimeWindow
from ttr.sources.capture_time import (
    SOURCE_FILENAME,
    SOURCE_NONE,
    SOURCE_SEND_FALLBACK,
    parse_capture_time,
)

from conftest import make_persisted_event

UTC = timezone.utc

# The real filename from the evaluated dataset whose burned-in pixel overlay
# reads "05/07/2026 06:19:50 am SUN" — block D is the only block that matches it.
REAL_NAME = "20260705_070618887_20260705_070605490_01_20260705061950000.jpg"


# --------------------------------------------------------------------------- #
# BST -> UTC conversion (real zoneinfo rules, never a hardcoded +1h)
# --------------------------------------------------------------------------- #
def test_bst_local_time_converts_to_utc():
    read = parse_capture_time(REAL_NAME)
    # Overlay/local 06:19:50 BST is 05:19:50 UTC — the -1h the offset requires.
    assert read.capture_time_utc == datetime(2026, 7, 5, 5, 19, 50, tzinfo=UTC)
    assert read.source == SOURCE_FILENAME
    assert read.tz_validated is True
    assert read.channel == "01"
    assert read.note is None


def test_gmt_winter_capture_uses_zero_offset_and_is_flagged_unvalidated():
    # A January capture is GMT (+0), NOT BST. A hardcoded +1h would shift it an
    # hour; zoneinfo gets it right, and it is flagged as outside the validated
    # window because the dataset predates the October DST change.
    read = parse_capture_time("cam_01_20260115093000000.jpg")
    assert read.capture_time_utc == datetime(2026, 1, 15, 9, 30, 0, tzinfo=UTC)  # +0, not +1
    assert read.source == SOURCE_FILENAME
    assert read.tz_validated is False                    # honestly scoped
    assert "outside BST" in read.note


def test_boxed_suffix_and_summer_offset_are_both_handled():
    read = parse_capture_time(REAL_NAME.replace(".jpg", "_boxed.jpg"))
    assert read.capture_time_utc == datetime(2026, 7, 5, 5, 19, 50, tzinfo=UTC)
    assert read.tz_validated is True


def test_block_d_is_read_not_the_leading_utc_blocks():
    # The filename holds three timestamps; only the LAST is capture time. A parser
    # that grabbed block A would return 07:06:18, an hour and 46 minutes wrong.
    read = parse_capture_time(REAL_NAME)
    assert read.capture_time_utc.hour == 5                # from block D, not A/B
    assert read.capture_time_utc != datetime(2026, 7, 5, 7, 6, 18, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Fallback — never drop, never invent
# --------------------------------------------------------------------------- #
def test_unparseable_filename_falls_back_to_send_time_flagged():
    send = datetime(2026, 7, 5, 7, 6, 18, tzinfo=UTC)
    read = parse_capture_time("not-a-reolink-name.jpg", send_time_utc=send)

    assert read.capture_time_utc == send                 # degraded, not dropped
    assert read.source == SOURCE_SEND_FALLBACK           # and flagged as such
    assert read.tz_validated is False
    assert "does not carry a capture block" in read.note
    assert "using alert send time instead" in read.note


def test_missing_filename_falls_back_to_send_time():
    send = datetime(2026, 7, 5, 7, 6, 18, tzinfo=UTC)
    assert parse_capture_time(None, send_time_utc=send).source == SOURCE_SEND_FALLBACK
    assert parse_capture_time("", send_time_utc=send).capture_time_utc == send


def test_invalid_calendar_digits_fall_back_rather_than_raise():
    # 17 digits that are not a real date (month 99) must degrade, not explode.
    send = datetime(2026, 7, 5, 7, 6, 18, tzinfo=UTC)
    read = parse_capture_time("cam_01_20269915093000000.jpg", send_time_utc=send)
    assert read.source == SOURCE_SEND_FALLBACK
    assert "not a valid timestamp" in read.note


def test_no_filename_and_no_send_time_yields_nothing_not_a_guess():
    read = parse_capture_time(None, send_time_utc=None)
    assert read.capture_time_utc is None
    assert read.source == SOURCE_NONE                    # never a fabricated time


def test_capture_after_send_is_flagged_as_suspect():
    # Physically impossible ordering => the tz reading is wrong. Store it, but say so.
    send = datetime(2026, 7, 5, 5, 0, 0, tzinfo=UTC)     # before the 05:19:50 capture
    read = parse_capture_time(REAL_NAME, send_time_utc=send)
    assert read.tz_validated is False
    assert "AFTER the alert send time" in read.note


# --------------------------------------------------------------------------- #
# THE PHANTOM ZERO — 2026-07-06 must show 20, not 0
# --------------------------------------------------------------------------- #
def _outage_events():
    """The measured outage shape: 20 events CAPTURED on 2026-07-06 but all
    delivered on 2026-07-07 after a ~53h delivery gap, so send-time dating filed
    them under the 7th and left the 6th reading as a zero-activity day."""
    out = []
    for i in range(20):
        cap = datetime(2026, 7, 6, 6 + (i % 12), (i * 3) % 60, tzinfo=UTC)
        out.append(make_persisted_event(
            f"<outage{i}@x>", canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, 7, 12, 20, i % 60, tzinfo=UTC),   # send: the 7th
            capture_time_utc=cap,                                          # capture: the 6th
            capture_time_source=SOURCE_FILENAME, capture_time_tz_validated=True,
            display_common_name="red fox",
            bioclip_ok=True, vlm_ok=True, agreement_flag="agree"))
    return out


def _report(repo, alias_map, llm):
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), llm)
    return agent.generate("fox",
                          TimeWindow.from_dates(datetime(2026, 7, 5).date(),
                                                datetime(2026, 7, 8).date()))


def test_phantom_zero_day_shows_twenty_after_correction(repo, alias_map, fake_llm):
    for ev in _outage_events():
        repo.upsert_event(ev)
    md = _report(repo, alias_map, fake_llm)

    # THE regression this whole change exists to prevent: the 6th is not empty.
    assert "| 2026-07-06 | 20 |" in md
    assert "| 2026-07-06 | 0 |" not in md
    # ...and the 7th is no longer inflated by the 6th's backlog.
    assert "| 2026-07-07 | 0 |" in md


def test_report_discloses_the_reclassified_days_and_keeps_send_time(repo, alias_map, fake_llm):
    for ev in _outage_events():
        repo.upsert_event(ev)
    md = _report(repo, alias_map, fake_llm)

    assert "## How these events were dated" in md
    assert "20 of 20 event(s) are placed by camera capture time" in md
    assert "would have been dated to a DIFFERENT day" in md
    assert "2026-07-06" in md
    # The send time is retained, not overwritten — the divergence is the evidence.
    assert "not overwritten" in md
    assert "retained as evidence" in md


def test_send_time_caveat_names_delivery_latency_not_midnight(repo, alias_map, fake_llm):
    for ev in _outage_events():
        repo.upsert_event(ev)
    md = _report(repo, alias_map, fake_llm).lower()

    # The measured mechanism (0 of 24 mis-datings were midnight cases).
    assert "delivery" in md and "backlog" in md
    # The within-60s figure is now recomputed per-report, not a hardcoded 88.2%.
    assert "within 60s of capture" in md                  # the claim wording is kept
    assert "delivered within" in md and "% were delivered" in md
    assert "midnight-boundary rounding" in md             # named only to rule it out
    # The DST scope limitation is stated, not assumed away.
    assert "validated only within bst" in md
    assert "zoneinfo" in md


def test_midnight_local_capture_counts_under_its_local_day(repo, alias_map, fake_llm):
    """A capture at 00:30 BST is 23:30 UTC the PREVIOUS day. It must count under
    the local day it was actually taken — bucketing by UTC date would re-file it a
    day early, a timezone-flavoured repeat of the send-time off-by-one."""
    repo.upsert_event(make_persisted_event(
        "<mid@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 5, 23, 35, tzinfo=UTC),
        capture_time_utc=datetime(2026, 7, 5, 23, 30, tzinfo=UTC),   # 00:30 BST on the 6th
        capture_time_source=SOURCE_FILENAME, capture_time_tz_validated=True,
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate("fox", TimeWindow.from_dates(date(2026, 7, 6), date(2026, 7, 6)))

    assert "| 2026-07-06 | 1 |" in md              # counted on its LOCAL day
    assert "| 2026-07-05 |" not in md              # not re-filed to the UTC day
    assert "did not reconcile" not in md           # selection and bucketing agree
    assert "Europe/London" in md                   # header names the basis honestly
    assert "Date (UTC)" not in md


def test_fallback_rows_are_disclosed_in_the_report(repo, alias_map, fake_llm):
    repo.upsert_event(make_persisted_event(
        "<fb@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
        capture_time_utc=datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
        capture_time_source=SOURCE_SEND_FALLBACK, capture_time_tz_validated=False,
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    md = _report(repo, alias_map, fake_llm)
    assert "1 event(s) had no decodable capture time" in md
    assert "flagged in the data rather than dropped" in md
