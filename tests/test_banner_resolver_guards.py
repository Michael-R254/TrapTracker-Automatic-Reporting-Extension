"""Regression tests for the three resolver guards found during Phase 2b.

Each guard exists because a plausible resolver produced a CONFIDENTLY WRONG
answer — picking the wrong banner, and so eventually the wrong crop — and each
was caught by synthetic multi-banner frames where the right answer was known by
construction. Together they took the wrong-pick rate on same-species pairs from
4.2% to 0.12%.

A wrong pick here is worse than an abstention: it mislabels which box belongs to
the alert, silently. These tests exist so that property cannot regress.

The template bank is stubbed, so no Pillow and no corpus is needed. numpy IS
needed: the resolver under test imports it inside `banner_ocr` to score the
rasters, and an earlier version of this docstring claimed otherwise, which is
how a fresh clone came to see six ModuleNotFoundError instead of six skips.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

# Same pattern as `test_thumbnails.py` (PIL) and `test_crop_is_overlay_free.py`:
# an absent optional extra is a SKIP, not a failure. numpy is declared by the
# [detect] extra and also arrives with torch in [enrich].
pytest.importorskip("numpy", reason="needs the [detect] or [enrich] extra")

from ttr.crop.banner_ocr import BannerResolver

PIGEON = "ColumbaPalumbus"
CROW = "CorvusCorone"


@dataclass
class _Raster:
    """Stands in for a glyph mask: only its width is consulted by the guards."""
    width: int

    @property
    def shape(self):
        return (22, self.width)


class _StubBank:
    """A template bank with scripted reads, so the guards can be exercised alone."""

    def __init__(self, widths, reads, width_tol=4):
        self.tpl = {k: object() for k in widths}
        self.width = dict(widths)
        self.width_tol = width_tol
        self._reads = reads          # {raster width: label or None}

    def classify(self, mask, floor, margin):
        return self._reads.get(mask.width, None), 0.99, 0.5, "ok"

    def well_formed(self, label_raster, conf_raster, *, require_label=True):
        # Mirrors the real rule: a genuine banner renders "0.NN" at 35-39px.
        if not (33 <= conf_raster.width <= 45):
            return False, "conf_width_out_of_range"
        if require_label and min(abs(w - label_raster.width)
                                 for w in self.width.values()) > self.width_tol:
            return False, "label_width_matches_no_template"
        return True, "ok"


def _resolver(reads, widths=None, conf_values=("0.97",)):
    widths = widths or {PIGEON: 157, CROW: 117}
    # The confidence bank carries real templates, so a tie-break reaches the
    # sanity gate rather than short-circuiting on a missing template.
    confs = _StubBank({v: 39 for v in conf_values}, {})
    return BannerResolver(_StubBank(widths, reads), confs)


# --------------------------------------------------------------------------

def test_guard_1_an_unreadable_banner_makes_the_frame_unresolvable():
    """An abstention on one banner must not become a confident pick on another.

    Before this guard, a banner that failed to read left the other as the
    "unique" match — turning an honest abstention into a wrong answer 4.2% of
    the time.
    """
    banners = [(_Raster(157), _Raster(39)),      # width-compatible, unreadable
               (_Raster(157), _Raster(39))]      # reads as the expected label
    r = _resolver({157: None})
    idx, basis = r.resolve(banners, PIGEON, 0.97)
    assert idx is None
    assert basis == "unreadable_banner_in_frame"


def test_guard_2_a_malformed_extraction_never_reaches_the_tiebreak():
    """A banner whose label/confidence split landed in the wrong gap still
    correlates highly against some template. It must be gated out before any
    correlation, not scored."""
    banners = [(_Raster(157), _Raster(39)),
               (_Raster(157), _Raster(201))]     # 201px "confidence": malformed
    r = _resolver({157: PIGEON})
    idx, basis = r.resolve(banners, PIGEON, 0.97)
    assert idx is None
    assert basis == "conf_malformed_in_tiebreak"


def test_guard_3_an_out_of_vocabulary_neighbour_is_excluded_not_fatal():
    """Fixing guard 2 over-corrected once: real out-of-vocabulary tokens
    (OvisAries, PasserDomesticus) look identical to malformed extractions under a
    width test, and abstention rose to 55%. A sane confidence raster tells them
    apart — the split worked, so the banner is safely excluded."""
    banners = [(_Raster(157), _Raster(39)),      # the expected token
               (_Raster(96), _Raster(39))]       # unknown token, well-formed
    r = _resolver({157: PIGEON})
    idx, basis = r.resolve(banners, PIGEON, 0.97)
    assert idx == 0
    assert basis == "label_unique"


def test_a_genuinely_different_known_label_is_excluded():
    banners = [(_Raster(157), _Raster(39)), (_Raster(117), _Raster(39))]
    r = _resolver({157: PIGEON, 117: CROW})
    idx, basis = r.resolve(banners, PIGEON, 0.97)
    assert idx == 0
    assert basis == "label_unique"


def test_no_template_for_the_expected_label_abstains():
    r = _resolver({157: PIGEON}, widths={PIGEON: 157})
    idx, basis = r.resolve([(_Raster(157), _Raster(39))], "SciurusCarolinensis", 0.74)
    assert idx is None
    assert basis == "label_template_missing"


def test_no_matching_banner_abstains_rather_than_picking_the_nearest():
    banners = [(_Raster(117), _Raster(39))]
    r = _resolver({117: CROW}, widths={PIGEON: 157, CROW: 117})
    idx, basis = r.resolve(banners, PIGEON, 0.97)
    assert idx is None
    assert basis == "no_label_match"


# --------------------------------------------------------------------------
# A label mismatch is a first-class outcome, not an error
# --------------------------------------------------------------------------

def test_label_mismatch_is_recorded_and_withholds_the_box(monkeypatch):
    """Phase 2b found this is how a Phase 1 recovery gap surfaces.

    On id1160 the database says ``CorvusCorone 0.80`` while the only recovered
    banner plainly reads ``ColumbaPalumbus 0.18`` — the frame carries two
    detections and only the pigeon's banner was recovered. The read is right and
    the assumption "one recovered banner means it is this event's" is wrong.

    So the mismatch must be RECORDED, and the box must NOT be offered for
    corroboration: it belongs to a different detection.
    """
    from ttr.crop import identify as identify_mod
    from ttr.crop.base import BannerRead

    monkeypatch.setattr(identify_mod, "load_rgb", lambda path: object())
    monkeypatch.setattr(identify_mod, "extract_banners", lambda frame: [{
        "rec": {"status": "ok", "box": (10, 10, 90, 90), "closed": True,
                "clipped": False, "banner": (10, 0, 120, 9)},
        "label_raster": _Raster(157), "conf_raster": _Raster(39),
    }])

    class _Resolver:
        def read_label(self, raster):
            return BannerRead(label=PIGEON, score=0.99, basis="ok")

    tt = identify_mod.identify("boxed.jpg", _Resolver(), CROW, 0.80)

    assert tt.label_mismatch is True
    assert tt.resolution_basis == "label_mismatch"
    assert tt.banner.label == PIGEON            # what the pixels actually say
    assert tt.box is None                       # not this event's box
    assert tt.recovered is False
    assert tt.usable_for_corroboration is False


def test_a_matching_single_banner_is_verified_and_offers_its_box(monkeypatch):
    from ttr.crop import identify as identify_mod
    from ttr.crop.base import BannerRead

    monkeypatch.setattr(identify_mod, "load_rgb", lambda path: object())
    monkeypatch.setattr(identify_mod, "extract_banners", lambda frame: [{
        "rec": {"status": "ok", "box": (10, 10, 90, 90), "closed": True,
                "clipped": False, "banner": (10, 0, 120, 9)},
        "label_raster": _Raster(157), "conf_raster": _Raster(39),
    }])

    class _Resolver:
        def read_label(self, raster):
            return BannerRead(label=PIGEON, score=0.99, basis="ok")

    tt = identify_mod.identify("boxed.jpg", _Resolver(), PIGEON, 0.97)
    assert tt.label_mismatch is False
    assert tt.resolution_basis == "single_banner_verified"
    assert tt.box == (10, 10, 90, 90)
    assert tt.self_check == "pass"
    assert tt.usable_for_corroboration is True
