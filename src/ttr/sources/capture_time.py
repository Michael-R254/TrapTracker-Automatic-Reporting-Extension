"""Decode camera capture time from the attachment filename (PURE, no I/O).

The alert email carries no capture/EXIF time — the camera strips EXIF — but the
Reolink-native portion of the attachment filename encodes it. Filenames look like::

    20260705_070618887_20260705_070605490_01_20260705061950000.jpg
    └── A: send ──┘└── B: upstream ──┘ C  └──── D: capture ────┘

* **A** — alert-email SEND time (UTC). Reproduces the stored ``event_time_utc``
  to sub-second (measured median delta -0.3s over 475 rows).
* **B** — upstream processing stamp, a median 2.4s before A (UTC).
* **C** — camera channel (``01`` throughout the evaluated dataset).
* **D** — the camera's own capture stamp, in **Europe/London LOCAL time**, and the
  only block that matches the timestamp burned into the image pixels.

**The filename mixes timezones**: A and B are UTC, D is local. D is therefore
converted via :mod:`zoneinfo` (real DST rules), never a hardcoded +1h offset —
a fixed offset silently shifts every winter capture by an hour.

Validation scope (a known limitation, recorded per row): the format and the
timezone reading were verified against 475 images spanning 2026-06-28..2026-07-16,
which lie **entirely within BST**. Captures in GMT (after the October DST change),
and local times that are ambiguous or non-existent across a DST transition, are
**unvalidated** — they are still decoded, but flagged so a report can say so
rather than present them as settled.

Degradation (CLAUDE.md constraint 4): a filename that does not carry a usable D
block never drops the detection and never fabricates a time — the caller's
send-time is used instead and the substitution is recorded in the provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..logging import get_logger

logger = get_logger(__name__)

#: The camera's wall-clock timezone. Config-overridable at the call site.
CAMERA_TIMEZONE = "Europe/London"

#: Reolink-native trailing portion: ``_<channel>_<YYYYMMDDHHMMSSmmm>`` before the
#: extension, tolerating our own ``_boxed`` suffix. Anchored to the end so it can
#: only ever match the D block, never one of the leading UTC stamps.
_D_BLOCK_RE = re.compile(r"_(?P<channel>\d{2})_(?P<d>\d{17})(?:_boxed)?\.[A-Za-z0-9]+$")

# Provenance values stored in `capture_time_source`.
SOURCE_FILENAME = "filename_block_d"
SOURCE_SEND_FALLBACK = "send_time_fallback"
SOURCE_NONE = "none"

_BST = timedelta(hours=1)   # the offset the dataset was validated under


@dataclass(frozen=True)
class CaptureTimeRead:
    """Outcome of decoding capture time for one detection.

    ``capture_time_utc`` is the value to store. ``source`` says where it came
    from, and ``tz_validated`` is False whenever the conversion falls outside the
    validated BST window — both are persisted, so the honesty layer can scope its
    claims per row instead of asserting a blanket guarantee.
    """

    capture_time_utc: Optional[datetime]
    source: str                        # SOURCE_FILENAME | SOURCE_SEND_FALLBACK | SOURCE_NONE
    tz_validated: bool = False
    channel: Optional[str] = None
    note: Optional[str] = None         # why it degraded / why tz is unvalidated

    @property
    def from_filename(self) -> bool:
        return self.source == SOURCE_FILENAME


def parse_capture_time(
    filename: Optional[str],
    *,
    send_time_utc: Optional[datetime] = None,
    tz_name: str = CAMERA_TIMEZONE,
) -> CaptureTimeRead:
    """Decode block D of ``filename`` to a UTC capture time.

    Falls back to ``send_time_utc`` (flagged) when the filename carries no usable
    D block, so a detection is never dropped and a time is never invented.
    """
    if not filename:
        return _fallback(send_time_utc, "no filename recorded for this detection")

    m = _D_BLOCK_RE.search(filename)
    if m is None:
        logger.warning("capture_time_unparseable_filename", extra={"image_filename": filename})
        return _fallback(send_time_utc, f"filename does not carry a capture block: {filename!r}")

    raw, channel = m.group("d"), m.group("channel")
    try:
        naive_local = datetime.strptime(raw[:14], "%Y%m%d%H%M%S").replace(
            microsecond=int(raw[14:]) * 1000
        )
    except ValueError as exc:   # 17 digits that are not a real calendar time
        logger.warning("capture_time_invalid_digits",
                       extra={"image_filename": filename, "block": raw, "error": str(exc)})
        return _fallback(send_time_utc, f"capture block {raw!r} is not a valid timestamp")

    tz = ZoneInfo(tz_name)
    local = naive_local.replace(tzinfo=tz)
    capture_utc = local.astimezone(timezone.utc)

    # Scope the claim: only a genuine BST (+1h) reading is inside the validated
    # window, and only if that local time is neither ambiguous (clocks-back hour,
    # occurs twice) nor non-existent (clocks-forward gap).
    offset = local.utcoffset()
    ambiguous = local.replace(fold=1).utcoffset() != offset
    imaginary = tz.utcoffset(naive_local) != offset  # gap times normalise away
    notes = []
    if ambiguous:
        notes.append("local time is ambiguous across the DST change (occurs twice); "
                     "first occurrence assumed")
    if imaginary:
        notes.append("local time falls in the DST spring-forward gap and does not exist")
    if offset != _BST:
        notes.append(f"UTC offset {offset} is outside BST (+1:00), the only offset the "
                     "filename format was validated under")

    # Integrity check: a capture cannot post-date its own alert email. If it does,
    # the timezone reading is wrong — surface it rather than store a confident lie.
    if send_time_utc is not None and capture_utc > send_time_utc:
        drift = (capture_utc - send_time_utc).total_seconds()
        notes.append(f"decoded capture is {drift:.0f}s AFTER the alert send time, which is "
                     "impossible — the timezone reading for this row is suspect")
        logger.warning("capture_time_after_send_time",
                       extra={"image_filename": filename, "drift_seconds": drift})

    return CaptureTimeRead(
        capture_time_utc=capture_utc,
        source=SOURCE_FILENAME,
        tz_validated=not notes,
        channel=channel,
        note="; ".join(notes) or None,
    )


def _fallback(send_time_utc: Optional[datetime], why: str) -> CaptureTimeRead:
    """Degrade to send time (flagged), or to nothing at all — never to a guess."""
    if send_time_utc is None:
        return CaptureTimeRead(None, SOURCE_NONE, tz_validated=False,
                               note=f"{why}; no send time available either")
    return CaptureTimeRead(send_time_utc, SOURCE_SEND_FALLBACK, tz_validated=False,
                           note=f"{why}; using alert send time instead")
