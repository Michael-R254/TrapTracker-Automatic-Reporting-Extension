"""Config-driven detector selection, mirroring ``enrichment/factory.py``.

Only the shipped provider is wired. MegaDetector is deliberately NOT offered:
every shipped inference path for it installs an AGPL-3.0 dependency, and this
project serves a web UI. Adding it later is a config-and-factory change, and a
LICENSING_REVIEW.md §4 entry, not a redesign.
"""

from __future__ import annotations

from ..config import Settings
from .detector import ObjectDetector, RtDetrDetector


def build_detector(settings: Settings) -> ObjectDetector:
    return RtDetrDetector(
        model_name=settings.detector_model,
        device=settings.detector_device,
        score_threshold=settings.detector_score_threshold,
    )
