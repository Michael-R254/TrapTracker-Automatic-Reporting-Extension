"""The crop decision path (Phase 2c, §3 of the Phase 2 design).

One invariant governs everything here:

    THE CROP BOX IS ALWAYS THE INDEPENDENT DETECTOR'S BOX, OR THERE IS NO CROP.

TrapTracker's recovered rectangle is never crop geometry — not even when it is
the only box available. Feeding BioCLIP a region that TrapTracker localised would
let it inherit TrapTracker's localisation errors, and a mis-localised crop would
then be recorded as a species disagreement rather than a localisation one
(Decision 1). The recovered box corroborates the detector's choice and nothing
more.

Every branch ends in exactly one of the six ``CropStatus`` values, and every
branch still hands BioCLIP pixels — a crop or the full clean frame — so no event
loses its taxonomic read (Constraint 4).

Pure Python: no numpy, no Pillow, no torch. The decision is testable without any
model or image on disk.
"""

from __future__ import annotations

from typing import Optional

from .base import (CropDecision, DetectorRead, TrapTrackerRead, iou, pad_box)

#: Derived in Phase 2a from the valley between the noise floor and the
#: genuine-match mode of the IoU distribution, not chosen by convention. Any
#: value in [0.20, 0.45] changes at most four of 819 verdicts, so the parameter
#: is stable rather than finely tuned.
DEFAULT_TAU = 0.30

#: Phase 1 measured the median box at 0.91% of frame area; a little context around
#: the animal helps the classifier without pulling in the neighbouring clutter.
DEFAULT_PAD_FRAC = 0.15


def _basis_for_unusable(tt: TrapTrackerRead) -> str:
    """Name WHY the TrapTracker box could not corroborate, rather than lumping
    every cause into one bucket. These are different failures with different fixes."""
    if tt.label_mismatch:
        return "tt_banner_label_mismatch"
    if not tt.recovered or tt.box is None:
        return "tt_box_unrecoverable"
    if tt.self_check != "pass":
        return "tt_box_failed_self_check"
    return "tt_banner_unresolved"


def decide_crop(
    detector: DetectorRead,
    tt: TrapTrackerRead,
    frame_width: int,
    frame_height: int,
    *,
    tau: float = DEFAULT_TAU,
    pad_frac: float = DEFAULT_PAD_FRAC,
) -> CropDecision:
    """Choose the crop for one event, or decline to crop, with a recorded reason."""
    n = len(detector.candidates)

    if not detector.ok:
        return CropDecision(status="uncropped_error", basis="detector_failed",
                            detector_candidate_count=n, error=detector.error)

    if n == 0:
        # No independent box means no crop. We do NOT fall back to the TrapTracker
        # rectangle even when one is sitting there verified — that is exactly the
        # independence leak this phase exists to prevent.
        return CropDecision(status="uncropped_no_detection",
                            basis="no_candidate_above_threshold",
                            detector_candidate_count=0)

    if tt.usable_for_corroboration:
        assert tt.box is not None                      # implied by usable_for_corroboration
        best = max(detector.candidates, key=lambda c: iou(tt.box, c.box))
        best_iou = iou(tt.box, best.box)
        crop, achieved = pad_box(best.box, pad_frac, frame_width, frame_height)
        agreed = best_iou >= tau
        return CropDecision(
            status="cropped_verified" if agreed else "cropped_disputed",
            basis="iou_at_or_above_tau" if agreed else "iou_below_tau",
            crop_box=crop, detector_box=best.box, detector_score=best.score,
            detector_candidate_count=n, pad_frac=achieved,
            localisation_iou=round(best_iou, 4),
            localisation_agreement="agree" if agreed else "disagree")

    # No usable corroboration. A single candidate is safe enough to crop; several
    # are not. Phase 2a measured the top-scoring candidate as the right animal only
    # 72.1% of the time, so picking one on score alone would assert which animal
    # the alert names without evidence (Constraint 3).
    if n == 1:
        only = detector.candidates[0]
        crop, achieved = pad_box(only.box, pad_frac, frame_width, frame_height)
        return CropDecision(status="cropped_unverified",
                            basis="single_candidate_no_tt_box",
                            crop_box=crop, detector_box=only.box,
                            detector_score=only.score, detector_candidate_count=1,
                            pad_frac=achieved,
                            localisation_agreement="not_comparable")

    return CropDecision(status="uncropped_ambiguous",
                        basis=_basis_for_unusable(tt) if tt.frame_detection_count
                        else "multiple_candidates_no_tt_box",
                        detector_candidate_count=n,
                        localisation_agreement="not_comparable")
