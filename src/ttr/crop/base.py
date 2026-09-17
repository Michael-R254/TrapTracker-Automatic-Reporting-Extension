"""Crop-path contracts: result dataclasses and the crop_status vocabulary (Phase 2c).

Deliberately free of numpy, Pillow and torch so the decision logic is importable
— and testable — without the ``[enrich]`` or ``[detect]`` extras. The heavy
pieces live in ``overlay``, ``banner_ocr`` and ``detector``, which defer their
imports to call time the way ``bioclip_enricher`` already does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

#: A rectangle on the frame, ``(x0, y0, x1, y1)`` inclusive, in pixels.
Box = tuple[int, int, int, int]

#: The headline outcome. Every event lands in exactly one of these six, and the
#: prefix says whether BioCLIP saw a crop or the whole frame — so a reader never
#: has to infer it. ``cropped_*`` means a crop was taken; ``uncropped_*`` means
#: the full clean frame was passed instead, which is always a valid fallback and
#: never a dropped event (Constraint 4).
CropStatus = Literal[
    "cropped_verified",        # detector box, corroborated by an OCR-identified TT box
    "cropped_disputed",        # detector box used, TT box identified, but IoU < tau
    "cropped_unverified",      # single detector box, no TT box available to corroborate
    "uncropped_ambiguous",     # several detector boxes and nothing to choose between them
    "uncropped_no_detection",  # the detector proposed nothing above threshold
    "uncropped_error",         # the detector or the crop step failed; error recorded
]

#: Finer reason, in the spirit of ``resolution_basis`` on the cross-check. Says
#: HOW the status was reached, so a cohort can be isolated without re-deriving it.
CropBasis = Literal[
    "iou_at_or_above_tau",
    "iou_below_tau",
    "single_candidate_no_tt_box",
    "multiple_candidates_no_tt_box",
    "tt_box_unrecoverable",
    "tt_box_failed_self_check",
    "tt_banner_unresolved",
    "tt_banner_label_mismatch",
    "no_candidate_above_threshold",
    "detector_failed",
    "image_unreadable",
]

#: Whether the independent detector and TrapTracker agree on WHERE the animal is.
#: Distinct from ``agreement_flag``, which is about WHAT species it is — the two
#: must never be conflated, hence the separate name and column.
LocalisationAgreement = Literal["agree", "disagree", "not_comparable"]


@dataclass
class DetectorBox:
    box: Box
    score: float
    label: str                      # the detector's own class name; recorded, never used


@dataclass
class DetectorRead:
    """What the independent detector proposed on the CLEAN frame."""
    provider: str
    model_name: str
    candidates: list[DetectorBox] = field(default_factory=list)
    ok: bool = False
    error: Optional[str] = None


@dataclass
class BannerRead:
    """One banner's text, as read by the Phase 2b matcher."""
    label: Optional[str] = None
    confidence: Optional[float] = None
    score: float = 0.0
    basis: str = "unread"


@dataclass
class TrapTrackerRead:
    """The recovered TrapTracker rectangle for THIS event, if it could be identified.

    Comparison-only by construction: nothing downstream may use ``box`` as crop
    geometry. It exists to corroborate the detector's choice and to record when
    the two disagree.
    """
    box: Optional[Box] = None
    recovered: bool = False
    self_check: Optional[str] = None            # "pass" | "fail" | None
    clipped: bool = False
    banner: BannerRead = field(default_factory=BannerRead)
    frame_detection_count: int = 0
    label_mismatch: bool = False
    #: Why identification succeeded or failed, from the Phase 2b resolver.
    resolution_basis: Optional[str] = None
    error: Optional[str] = None

    @property
    def usable_for_corroboration(self) -> bool:
        """A TT box corroborates only when it is present, geometrically trusted,
        and demonstrably this event's. Phase 1 showed self-check-passing boxes are
        exact; the ones that fail are not trustworthy enough to adjudicate."""
        return (self.box is not None
                and self.recovered
                and self.self_check == "pass"
                and not self.label_mismatch)


@dataclass
class CropDecision:
    """The full auditable outcome for one event."""
    status: CropStatus
    basis: CropBasis
    crop_box: Optional[Box] = None              # after padding, on the clean frame
    detector_box: Optional[Box] = None          # before padding
    detector_score: Optional[float] = None
    detector_candidate_count: int = 0
    pad_frac: Optional[float] = None
    localisation_iou: Optional[float] = None
    localisation_agreement: LocalisationAgreement = "not_comparable"
    error: Optional[str] = None

    @property
    def is_cropped(self) -> bool:
        return self.status.startswith("cropped_")


def iou(a: Box, b: Box) -> float:
    """Intersection over union of two inclusive pixel rectangles."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    iw = min(ax1, bx1) - max(ax0, bx0)
    ih = min(ay1, by1) - max(ay0, by0)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = float(iw * ih)
    union = ((ax1 - ax0) * (ay1 - ay0)) + ((bx1 - bx0) * (by1 - by0)) - inter
    return inter / union if union > 0 else 0.0


def pad_box(box: Box, frac: float, width: int, height: int) -> tuple[Box, float]:
    """Expand a box by ``frac`` of each side, clamped to the frame.

    Returns the padded box and the fraction ACTUALLY applied, which is smaller
    than requested when the box sits against a frame edge. Storing the achieved
    padding rather than the requested one keeps the record honest about what the
    crop really contains.
    """
    x0, y0, x1, y1 = box
    px, py = int(round((x1 - x0) * frac)), int(round((y1 - y0) * frac))
    nx0, ny0 = max(0, x0 - px), max(0, y0 - py)
    nx1, ny1 = min(width - 1, x1 + px), min(height - 1, y1 + py)
    want = (x1 - x0) * (y1 - y0) * (1 + 2 * frac) ** 2
    got = max(1, (nx1 - nx0) * (ny1 - ny0))
    achieved = frac if want <= 0 else frac * min(1.0, got / want)
    return (nx0, ny0, nx1, ny1), round(achieved, 4)
