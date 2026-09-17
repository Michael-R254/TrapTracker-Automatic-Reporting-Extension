"""Identify THIS event's TrapTracker rectangle: recover, then read the banner.

Ties Phase 1's overlay recovery to Phase 2b's banner matcher. The output is a
``TrapTrackerRead``, which the decision path uses ONLY to corroborate the
independent detector's choice — never as crop geometry.

Templates are learned from the corpus itself: an event with exactly one rendered
detection has banner text "<upstream_label> <upstream_confidence>", so
``TemplateFitter`` builds the bank with no manual annotation.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..logging import get_logger
from .banner_ocr import (BannerResolver, CONF_CANON, CONF_KEEP_RIGHT, LABEL_CANON,
                         TemplateBank, crop_right, glyph_mask, split_banner)
from .base import BannerRead, TrapTrackerRead
from .overlay import load_rgb, recover

logger = get_logger(__name__)


def extract_banners(boxed_frame) -> list[dict]:
    """Every recovered banner, with its label/confidence rasters where the split
    succeeded. Kept together with the traced box so the two stay aligned."""
    out = []
    for rec in recover(boxed_frame):
        mask = glyph_mask(boxed_frame, rec["banner"])
        parts = split_banner(mask)
        out.append({
            "rec": rec,
            "label_raster": parts[0] if parts else None,
            "conf_raster": parts[1] if parts else None,
        })
    return out


class TemplateFitter:
    """Builds the template banks from single-detection frames (self-labelling)."""

    def __init__(self) -> None:
        self.labels = TemplateBank(*LABEL_CANON)
        self.confs = TemplateBank(*CONF_CANON)
        self.n_label = 0
        self.n_conf = 0

    def add_event(self, boxed_frame, upstream_label: str,
                  upstream_confidence: Optional[float]) -> bool:
        """Add one event if it is usable as a template source. Returns whether it was.

        Only frames with exactly ONE recovered banner qualify: with more than one,
        which banner carries ``upstream_label`` is precisely the question the
        resolver exists to answer, so using them would be circular.
        """
        banners = extract_banners(boxed_frame)
        if len(banners) != 1 or banners[0]["label_raster"] is None:
            return False
        b = banners[0]
        self.labels.add(upstream_label, b["label_raster"])
        self.n_label += 1
        if upstream_confidence is not None:
            self.confs.add(f"{upstream_confidence:.2f}",
                           crop_right(b["conf_raster"], CONF_KEEP_RIGHT))
            self.n_conf += 1
        return True

    def build(self) -> BannerResolver:
        return BannerResolver(self.labels.fit(), self.confs.fit())


def identify(boxed_image_path: str, resolver: BannerResolver,
             upstream_label: str, upstream_confidence: Optional[float]) -> TrapTrackerRead:
    """Recover the frame's rectangles and say which one is this event's.

    A label MISMATCH is a first-class outcome, not an error. Phase 2b found it is
    how a Phase 1 recovery gap surfaces: on ``id1160`` the database says
    ``CorvusCorone 0.80`` while the single recovered banner plainly reads
    ``ColumbaPalumbus 0.18`` — the frame carries two detections and only the
    pigeon's banner was recovered. Recording the mismatch keeps Phase 1's
    "second detection missed" mode visible instead of silently resolved.
    """
    try:
        frame = load_rgb(boxed_image_path)
    except Exception as exc:
        return TrapTrackerRead(error=f"{type(exc).__name__}: {exc}")

    banners = extract_banners(frame)
    n = len(banners)
    if n == 0:
        return TrapTrackerRead(frame_detection_count=0,
                               resolution_basis="no_banner_recovered")

    usable = [b for b in banners if b["label_raster"] is not None]
    if not usable:
        return TrapTrackerRead(frame_detection_count=n,
                               resolution_basis="banner_split_failed")

    if len(usable) == 1:
        b = usable[0]
        read = resolver.read_label(b["label_raster"])
        rec = b["rec"]
        mismatch = read.label is not None and read.label != upstream_label
        basis = ("label_mismatch" if mismatch
                 else "single_banner_verified" if read.label == upstream_label
                 else "single_banner_unread")
        if mismatch:
            logger.warning("banner_label_mismatch",
                           extra={"db_label": upstream_label, "banner_label": read.label,
                                  "path": boxed_image_path})
        return _read_from(rec, read, n, mismatch, basis,
                          identified=(read.label == upstream_label))

    pairs = [(b["label_raster"], b["conf_raster"]) for b in usable]
    idx, basis = resolver.resolve(pairs, upstream_label, upstream_confidence)
    if idx is None:
        return TrapTrackerRead(frame_detection_count=n, resolution_basis=basis)
    b = usable[idx]
    read = resolver.read_label(b["label_raster"])
    if basis == "conf_broke_tie":
        # The digits were genuinely checked here: this banner's confidence raster
        # was ranked against the template for the expected value AND beat the
        # other same-species banner by a margin, with abstention the alternative.
        # Recording it says the confidence was verified, not merely assumed.
        # Phase 2b showed OPEN confidence reading is unsafe (88.1% with three
        # confident errors), so it is never read in the open — only ranked.
        read.confidence = upstream_confidence
    return _read_from(b["rec"], read, n, False, basis, identified=True)


def _read_from(rec: dict, read: BannerRead, n: int, mismatch: bool,
               basis: str, *, identified: bool) -> TrapTrackerRead:
    ok = rec.get("status") == "ok"
    return TrapTrackerRead(
        box=rec.get("box") if (ok and identified) else None,
        recovered=bool(ok and identified),
        self_check=("pass" if rec.get("closed") else "fail") if ok else None,
        clipped=bool(rec.get("clipped", False)),
        banner=read,
        frame_detection_count=n,
        label_mismatch=mismatch,
        resolution_basis=basis,
    )
