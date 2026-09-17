"""Crop path: an independent detector proposes the crop, TrapTracker's recovered
box only corroborates it (Phase 2c).

The invariant, stated once here because everything else depends on it: the crop
box is always the independent detector's box, or there is no crop. TrapTracker's
rectangle is never crop geometry.
"""

from .base import (Box, BannerRead, CropBasis, CropDecision, CropStatus,
                   DetectorBox, DetectorRead, LocalisationAgreement,
                   TrapTrackerRead, iou, pad_box)
from .decide import DEFAULT_PAD_FRAC, DEFAULT_TAU, decide_crop

__all__ = [
    "Box", "BannerRead", "CropBasis", "CropDecision", "CropStatus",
    "DetectorBox", "DetectorRead", "LocalisationAgreement", "TrapTrackerRead",
    "iou", "pad_box", "decide_crop", "DEFAULT_TAU", "DEFAULT_PAD_FRAC",
]
