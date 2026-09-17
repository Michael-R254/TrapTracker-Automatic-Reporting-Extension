"""Run the crop path over stored events (Phase 2c backfill).

Two passes, because the banner templates are learned from the corpus itself:

  1. FIT   — build label/confidence templates from single-detection frames, whose
             banner text is known from ``upstream_label``/``upstream_confidence``
             with no manual annotation.
  2. DECIDE — for each event: recover and identify TrapTracker's box, run the
             independent detector on the clean frame, decide, crop, classify.

Every event ends in exactly one of the six ``CropStatus`` values and every event
still hands BioCLIP pixels — a crop or the full clean frame. One event's failure
never halts the loop (Constraint 4).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from ..logging import get_logger
from .base import CropDecision, DetectorRead, TrapTrackerRead
from .decide import decide_crop
from .identify import TemplateFitter, identify
from .overlay import load_rgb

logger = get_logger(__name__)


@dataclass
class CropOutcome:
    event_id: int
    decision: CropDecision
    tt: TrapTrackerRead
    detector: DetectorRead
    taxo: object = None                     # TaxonomicRead on the crop, when run


def fit_templates(rows, *, progress: Optional[Callable[[int, int], None]] = None):
    """Build the banner template banks from every usable single-detection frame."""
    fitter = TemplateFitter()
    total = len(rows)
    for n, (_id, label, conf, _orig, boxed) in enumerate(rows):
        if not (label and boxed):
            continue
        try:
            fitter.add_event(load_rgb(boxed), label, conf)
        except Exception as exc:            # a bad frame must not stop the fit
            logger.warning("template_fit_skipped",
                           extra={"path": boxed, "error": repr(exc)})
        if progress and n % 50 == 0:
            progress(n, total)
    logger.info("banner_templates_fitted",
                extra={"label_examples": fitter.n_label, "conf_examples": fitter.n_conf,
                       "label_tokens": fitter.labels.n_classes,
                       "conf_values": fitter.confs.n_classes})
    return fitter.build()


def _crop_bytes(clean_path: str, box) -> bytes:
    from PIL import Image
    im = Image.open(clean_path).convert("RGB")
    x0, y0, x1, y1 = box
    buf = io.BytesIO()
    im.crop((x0, y0, x1 + 1, y1 + 1)).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _full_bytes(clean_path: str) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.open(clean_path).convert("RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def process_event(event_id: int, upstream_label: str, upstream_confidence,
                  clean_path: str, boxed_path: str, *, resolver, detector,
                  taxonomic=None, tau: float, pad_frac: float) -> CropOutcome:
    """Decide, crop and (optionally) classify one event. Never raises."""
    if not boxed_path:
        tt = TrapTrackerRead(resolution_basis="no_boxed_image")
    elif resolver is None:
        # Corroboration is optional: without a resolver the crop path still runs,
        # it just cannot identify which recovered box is this event's.
        tt = TrapTrackerRead(resolution_basis="no_resolver")
    else:
        tt = identify(boxed_path, resolver, upstream_label, upstream_confidence)

    try:
        clean = _full_bytes(clean_path) if clean_path else b""
    except Exception as exc:
        clean = b""
        logger.warning("clean_frame_unreadable",
                       extra={"path": clean_path, "error": repr(exc)})

    det = detector.detect(clean)

    width = height = 0
    if clean:
        try:
            from PIL import Image
            with Image.open(clean_path) as im:
                width, height = im.size
        except Exception:
            width = height = 0

    if not clean or not width:
        decision = CropDecision(status="uncropped_error", basis="image_unreadable",
                                detector_candidate_count=len(det.candidates),
                                error="clean frame unreadable")
    else:
        decision = decide_crop(det, tt, width, height, tau=tau, pad_frac=pad_frac)

    taxo = None
    if taxonomic is not None and clean:
        try:
            # BioCLIP sees PIXELS ONLY -- the crop, or the full clean frame when
            # no crop was taken. It is never told the label, the OCR'd banner
            # text, or which detector proposed the box.
            payload = (_crop_bytes(clean_path, decision.crop_box)
                       if decision.is_cropped and decision.crop_box else clean)
            taxo = taxonomic.classify(payload)
        except Exception as exc:            # enrichers do not raise, but belt-and-braces
            logger.warning("crop_classify_failed",
                           extra={"event_id": event_id, "error": repr(exc)})

    return CropOutcome(event_id=event_id, decision=decision, tt=tt,
                       detector=det, taxo=taxo)
