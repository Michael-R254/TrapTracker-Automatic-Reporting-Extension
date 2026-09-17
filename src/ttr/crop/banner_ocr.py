"""Read TrapTracker's banner text, to say which box belongs to this event.

Validated in Phase 2b on a held-out split: the label is read correctly 98.1% of
the time, abstained on 1.9%, and was never wrong — 100% precision on everything
accepted. End to end, 93.1% of the corpus gets its banner confidently identified.

Per-character templates are NOT achievable on this renderer. At ~20px the font
sets adjacent letters in contact, so column-projection segmentation splits
"ColumbaPalumbus" into six blobs rather than fifteen characters, and no
luminance threshold fixes it — raising the cut far enough to separate letters
starts fragmenting them instead. What the renderer does give is pixel-stability:
the same string produces the same raster every time, to the pixel. So the label
and the confidence are each matched as a WHOLE raster against templates learned
from the corpus itself.

The corpus is self-labelling: an event with exactly one rendered detection has
banner text "<upstream_label> <upstream_confidence>", so templates need no manual
annotation and grow on their own as the deployment sees new species.

Heavy imports are deferred so this module imports without the ``[enrich]`` extra.
"""

from __future__ import annotations

from typing import Optional

from .base import BannerRead

# --- tuned in Phase 2b on a validation split carved out of TRAINING ----------
LABEL_FLOOR = 0.90
LABEL_MARGIN = 0.05
CONF_TIEBREAK_MARGIN = 0.02
#: Every confidence renders "0.NN", so the leading "0." is shared by all of them
#: and swamps the correlation. Matching the right-hand fraction lets the two
#: digits that actually vary dominate — this moved validation accuracy from
#: 43.6% to 87.2% at the same threshold.
CONF_KEEP_RIGHT = 0.62
#: A real "0.NN" occupies 35-39px. Outside this band the label/confidence split
#: landed in the wrong gap: a malformed extraction, not a hard-to-read banner.
CONF_W_MIN, CONF_W_MAX = 33, 45
WIDTH_TOL = 4
#: A gap this wide separates the label from the confidence. Inter-letter gaps are
#: <=1px; the word gap measured 9px in 100 of 102 sampled banners.
SPLIT_GAP = 5

LABEL_CANON = (16, 120)
CONF_CANON = (28, 72)
GLYPH_CUT = 170


def glyph_mask(boxed_frame, banner: tuple[int, int, int, int]):
    """Binary glyph mask cropped to the banner rectangle."""
    x0, y0, x1, y1 = banner
    return boxed_frame[y0:y1 + 1, x0:x1 + 1].min(axis=2) >= GLYPH_CUT


def _col_runs(mask, min_gap: int = 1) -> list[tuple[int, int]]:
    cols = mask.any(axis=0)
    runs, start, gap = [], None, 0
    for i, v in enumerate(cols):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > min_gap:
                runs.append((start, i - gap))
                start = None
    if start is not None:
        runs.append((start, len(cols) - 1))
    return runs


def split_banner(mask):
    """(label_raster, confidence_raster), or None if the wide gap is not found."""
    rs = _col_runs(mask)
    if len(rs) < 2:
        return None
    gaps = [(rs[i + 1][0] - rs[i][1] - 1, i) for i in range(len(rs) - 1)]
    width, i = max(gaps)
    if width < SPLIT_GAP:
        return None
    return mask[:, rs[0][0]:rs[i][1] + 1], mask[:, rs[i + 1][0]:rs[-1][1] + 1]


def _norm(mask, h: int, w: int):
    import numpy as np
    from PIL import Image
    im = Image.fromarray((mask * 255).astype("uint8")).resize((w, h), Image.BILINEAR)
    a = np.asarray(im, dtype="float32") / 255.0
    a = a - a.mean()
    n = float(np.linalg.norm(a))
    return (a / n).ravel() if n > 0 else a.ravel()


def crop_right(mask, frac: float):
    w = mask.shape[1]
    return mask[:, int(w * (1.0 - frac)):]


class TemplateBank:
    """One averaged raster per class, plus the native widths that produced it."""

    def __init__(self, h: int, w: int, width_tol: int = WIDTH_TOL) -> None:
        self.h, self.w, self.width_tol = h, w, width_tol
        self._acc: dict[str, list] = {}
        self._widths: dict[str, list[int]] = {}
        self.tpl: dict[str, object] = {}
        self.width: dict[str, float] = {}

    def add(self, key: str, mask) -> None:
        self._acc.setdefault(key, []).append(_norm(mask, self.h, self.w))
        self._widths.setdefault(key, []).append(int(mask.shape[1]))

    @property
    def n_classes(self) -> int:
        return len(self._acc)

    def fit(self) -> "TemplateBank":
        import numpy as np
        for k, vs in self._acc.items():
            m = np.mean(vs, axis=0)
            n = float(np.linalg.norm(m))
            self.tpl[k] = m / n if n > 0 else m
            self.width[k] = float(np.median(self._widths[k]))
        return self

    def score(self, mask) -> list[tuple[str, float]]:
        """[(key, ncc)] best first. Native pixel width is a hard prefilter — the
        renderer is deterministic, so a width mismatch rules a candidate out."""
        import numpy as np
        v = _norm(mask, self.h, self.w)
        obs = mask.shape[1]
        out = [(k, float(np.dot(v, t))) for k, t in self.tpl.items()
               if abs(self.width[k] - obs) <= self.width_tol]
        out.sort(key=lambda x: -x[1])
        return out

    def classify(self, mask, floor: float, margin: float):
        """(key, score, margin, basis); key is None when it declines to guess."""
        s = self.score(mask)
        if not s:
            return None, 0.0, 0.0, "no_width_match"
        top = s[0][1]
        gap = top - (s[1][1] if len(s) > 1 else -1.0)
        if top < floor:
            return None, top, gap, "below_floor"
        if len(s) > 1 and gap < margin:
            return None, top, gap, "ambiguous"
        return s[0][0], top, gap, "ok"

    def well_formed(self, label_raster, conf_raster, *, require_label: bool = True):
        """Is this extraction structurally sane enough to match at all?

        Matching a malformed raster is how a confident wrong answer gets produced
        — Phase 2b measured a 205px label and a 201px confidence still correlating
        highly against a template. Gated out before any correlation is computed.
        """
        if not (CONF_W_MIN <= conf_raster.shape[1] <= CONF_W_MAX):
            return False, "conf_width_out_of_range"
        if require_label and self.width and min(
                abs(w - label_raster.shape[1]) for w in self.width.values()) > self.width_tol:
            return False, "label_width_matches_no_template"
        return True, "ok"


class BannerResolver:
    """Decides which banner in a frame carries this event's alert.

    Carries the three guards Phase 2b found by testing rather than by design.
    Each is a way a plausible resolver produces a confidently wrong answer, and
    each was caught by synthetic multi-banner frames where the right answer was
    known by construction.
    """

    def __init__(self, labels: TemplateBank, confs: TemplateBank) -> None:
        self.labels, self.confs = labels, confs

    def read_label(self, label_raster) -> BannerRead:
        k, s, _g, basis = self.labels.classify(label_raster, LABEL_FLOOR, LABEL_MARGIN)
        return BannerRead(label=k, score=round(s, 4), basis=basis)

    def resolve(self, banners: list[tuple], expected_label: str,
                expected_conf: Optional[float]) -> tuple[Optional[int], str]:
        """``(index of this event's banner, basis)``; index None means abstain.

        ``banners`` is a list of ``(label_raster, confidence_raster)``.
        """
        import numpy as np
        if expected_label not in self.labels.tpl:
            return None, "label_template_missing"
        want_w = self.labels.width[expected_label]
        hits: list[int] = []
        unsafe = False

        for i, (lm, cm) in enumerate(banners):
            if abs(lm.shape[1] - want_w) <= self.labels.width_tol:
                # Could be the expected token on width alone, so it must be read.
                k, *_ = self.labels.classify(lm, LABEL_FLOOR, LABEL_MARGIN)
                if k == expected_label:
                    hits.append(i)
                elif k is None:
                    # GUARD 1. Width-compatible but unreadable: it might BE the
                    # match. Treating the other banner as "uniquely matching"
                    # turns an honest abstention on one into a confident wrong
                    # pick on the other — measured at 4.2% before this guard.
                    unsafe = True
                continue
            # Different width from the expected token, so it is a different
            # string. But "wrong width" has two causes that must not be
            # conflated: a legitimately different token (OvisAries,
            # PasserDomesticus — outside the template set and common in these
            # frames), or a malformed extraction. GUARD 3: the confidence raster
            # tells them apart, because a real banner renders "0.NN" at 35-39px
            # whatever its label says.
            if self.labels.well_formed(lm, cm, require_label=False)[0]:
                continue
            unsafe = True

        if unsafe:
            return None, "unreadable_banner_in_frame"
        if not hits:
            return None, "no_label_match"
        if len(hits) == 1:
            return hits[0], "label_unique"

        if expected_conf is None:
            return None, "conf_unknown"
        key = f"{expected_conf:.2f}"
        if key not in self.confs.tpl:
            return None, "conf_template_missing"
        # GUARD 2. The tie-break reads confidences, so now the rasters must be
        # sane: scoring a malformed one against a template invents a winner.
        for i in hits:
            if not self.labels.well_formed(banners[i][0], banners[i][1])[0]:
                return None, "conf_malformed_in_tiebreak"

        t = self.confs.tpl[key]
        ranked = sorted(
            ((float(np.dot(_norm(crop_right(banners[i][1], CONF_KEEP_RIGHT), *CONF_CANON), t)), i)
             for i in hits), reverse=True)
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < CONF_TIEBREAK_MARGIN:
            return None, "conf_ambiguous"
        return ranked[0][1], "conf_broke_tie"
