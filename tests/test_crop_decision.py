"""Phase 2c: the crop decision path, its invariant, and the Phase 2b guards.

These run without numpy, Pillow, torch, transformers or any model weights: the
decision logic is pure Python and the resolver guards are exercised through a
stub template bank. That is deliberate — the rules that keep a wrong crop out of
the pipeline should be testable in CI, not only on a machine with the extras.
"""

from __future__ import annotations

import pytest

from ttr.crop.base import (BannerRead, CropDecision, DetectorBox, DetectorRead,
                           TrapTrackerRead, iou, pad_box)
from ttr.crop.decide import decide_crop

FRAME_W, FRAME_H = 896, 512

ALL_STATUSES = {
    "cropped_verified", "cropped_disputed", "cropped_unverified",
    "uncropped_ambiguous", "uncropped_no_detection", "uncropped_error",
}


def _det(*boxes, ok=True, error=None):
    return DetectorRead(provider="rtdetr", model_name="test", ok=ok, error=error,
                        candidates=[DetectorBox(box=b, score=s, label="bird")
                                    for b, s in boxes])


def _tt(box=(100, 100, 200, 200), *, self_check="pass", mismatch=False,
        recovered=True, n=1):
    return TrapTrackerRead(box=box, recovered=recovered, self_check=self_check,
                           banner=BannerRead(label="ColumbaPalumbus", confidence=0.97),
                           frame_detection_count=n, label_mismatch=mismatch)


# --------------------------------------------------------------------------
# The invariant
# --------------------------------------------------------------------------

def test_crop_box_is_never_the_traptracker_box():
    """The whole point of Phase 2c: TrapTracker's rectangle is comparison-only.

    Even when it is the ONLY box available, it must not become crop geometry —
    cropping to it would let BioCLIP inherit TrapTracker's localisation, and a
    mis-localised crop would then be recorded as a species disagreement.
    """
    d = decide_crop(_det(), _tt(box=(10, 10, 90, 90)), FRAME_W, FRAME_H)
    assert d.status == "uncropped_no_detection"
    assert d.crop_box is None
    assert d.detector_box is None


def test_crop_box_always_derives_from_a_detector_candidate():
    tt_box = (100, 100, 200, 200)
    det = _det(((104, 104, 196, 196), 0.9))
    d = decide_crop(det, _tt(box=tt_box), FRAME_W, FRAME_H)
    assert d.is_cropped
    assert d.detector_box == (104, 104, 196, 196)
    assert d.detector_box != tt_box


# --------------------------------------------------------------------------
# The six states
# --------------------------------------------------------------------------

def test_verified_when_iou_at_or_above_tau():
    d = decide_crop(_det(((104, 104, 196, 196), 0.9)), _tt(), FRAME_W, FRAME_H, tau=0.30)
    assert d.status == "cropped_verified"
    assert d.basis == "iou_at_or_above_tau"
    assert d.localisation_agreement == "agree"
    assert d.localisation_iou >= 0.30


def test_disputed_when_iou_below_tau():
    d = decide_crop(_det(((600, 400, 700, 480), 0.9)), _tt(), FRAME_W, FRAME_H, tau=0.30)
    assert d.status == "cropped_disputed"
    assert d.basis == "iou_below_tau"
    assert d.localisation_agreement == "disagree"
    # Independence: the detector still supplies the geometry on a disagreement.
    assert d.detector_box == (600, 400, 700, 480)


def test_unverified_when_single_candidate_and_no_usable_tt_box():
    d = decide_crop(_det(((104, 104, 196, 196), 0.9)),
                    TrapTrackerRead(), FRAME_W, FRAME_H)
    assert d.status == "cropped_unverified"
    assert d.localisation_agreement == "not_comparable"


def test_ambiguous_when_several_candidates_and_no_usable_tt_box():
    """Phase 2a measured the top-scoring candidate as the right animal only 72.1%
    of the time, so picking on score alone would assert which animal the alert
    names without evidence."""
    d = decide_crop(_det(((10, 10, 90, 90), 0.9), ((300, 300, 380, 380), 0.8)),
                    TrapTrackerRead(), FRAME_W, FRAME_H)
    assert d.status == "uncropped_ambiguous"
    assert d.crop_box is None


def test_no_detection_and_error_states():
    assert decide_crop(_det(), TrapTrackerRead(), FRAME_W, FRAME_H).status \
        == "uncropped_no_detection"
    bad = decide_crop(_det(ok=False, error="boom"), _tt(), FRAME_W, FRAME_H)
    assert bad.status == "uncropped_error"
    assert bad.error == "boom"


# --------------------------------------------------------------------------
# A failed self-check or a label mismatch removes corroboration, not the crop
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tt,expect_basis", [
    (_tt(self_check="fail"), "tt_box_failed_self_check"),
    (_tt(mismatch=True), "tt_banner_label_mismatch"),
    (_tt(recovered=False, box=None), "tt_box_unrecoverable"),
])
def test_unusable_tt_box_falls_back_without_using_its_geometry(tt, expect_basis):
    """Phase 1 showed only self-check-PASSING boxes are exact; the rest are not
    trustworthy enough to adjudicate anything."""
    tt.frame_detection_count = 2
    d = decide_crop(_det(((10, 10, 90, 90), 0.9), ((300, 300, 380, 380), 0.8)),
                    tt, FRAME_W, FRAME_H)
    assert d.status == "uncropped_ambiguous"
    assert d.basis == expect_basis
    assert d.crop_box is None


def test_label_mismatch_is_recorded_not_swallowed():
    """Phase 2b found a mismatch is how a Phase 1 recovery gap surfaces (id1160:
    the database says CorvusCorone, the only recovered banner reads
    ColumbaPalumbus). It must stay visible."""
    tt = _tt(mismatch=True)
    assert tt.label_mismatch is True
    assert tt.usable_for_corroboration is False


# --------------------------------------------------------------------------
# Conservation and geometry
# --------------------------------------------------------------------------

def test_every_input_shape_lands_in_exactly_one_known_status():
    """Corpus-level conservation: nothing is dropped and nothing invents a state."""
    tts = [TrapTrackerRead(), _tt(), _tt(self_check="fail"), _tt(mismatch=True),
           _tt(box=(600, 400, 700, 480))]
    dets = [_det(), _det(((104, 104, 196, 196), 0.9)),
            _det(((10, 10, 90, 90), 0.9), ((300, 300, 380, 380), 0.8)),
            _det(ok=False, error="x")]
    seen = set()
    for tt in tts:
        for det in dets:
            d = decide_crop(det, tt, FRAME_W, FRAME_H)
            assert d.status in ALL_STATUSES
            assert d.is_cropped == (d.crop_box is not None)
            seen.add(d.status)
    assert seen == ALL_STATUSES, f"unreached states: {ALL_STATUSES - seen}"


def test_padding_is_clamped_and_the_achieved_fraction_is_recorded():
    box, achieved = pad_box((100, 100, 200, 200), 0.15, FRAME_W, FRAME_H)
    assert box == (85, 85, 215, 215)
    assert achieved == pytest.approx(0.15, abs=1e-3)
    # Against a frame edge the requested padding cannot be applied in full, and
    # the record says so rather than claiming 15%.
    edge, edge_achieved = pad_box((0, 0, 100, 100), 0.15, FRAME_W, FRAME_H)
    assert edge[0] == 0 and edge[1] == 0
    assert edge_achieved < 0.15


def test_iou_basics():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)
    assert iou((0, 0, 10, 10), (100, 100, 110, 110)) == 0.0
