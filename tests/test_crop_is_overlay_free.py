"""The crop is taken from the CLEAN frame, so it carries no overlay pixels.

Phase 1 established the geometric fact that makes this cheap: the label banner
sits strictly ABOVE the box top in 909 of 909 recovered rectangles, so a crop to
the box excludes its own label. But the stronger guarantee is structural rather
than geometric — the crop is cut from the stored clean original, which has no
overlay on it anywhere. Not a low contamination rate: zero, by construction.

That matters because burned-in text is a known VLM/CLIP hazard in this project.
This test pins the property to the code path rather than to an argument.
"""

from __future__ import annotations

import io

import pytest

pytest.importorskip("PIL", reason="needs the [enrich] extra")
np = pytest.importorskip("numpy", reason="needs the [enrich] extra")

from PIL import Image, ImageDraw            # noqa: E402

from ttr.crop.base import DetectorBox, DetectorRead, TrapTrackerRead  # noqa: E402
from ttr.crop.overlay import GREEN, GTOL    # noqa: E402
from ttr.crop.runner import process_event   # noqa: E402


def _frames(tmp_path):
    """A synthetic clean/boxed pair in the renderer's own idiom."""
    clean = Image.new("RGB", (400, 300), (120, 140, 95))
    ImageDraw.Draw(clean).ellipse([180, 150, 240, 210], fill=(90, 85, 80))
    clean_path = tmp_path / "clean.jpg"
    clean.save(clean_path, quality=95)

    boxed = clean.copy()
    d = ImageDraw.Draw(boxed)
    d.rectangle([175, 145, 245, 215], outline=GREEN, width=3)     # the box
    d.rectangle([175, 122, 330, 145], fill=GREEN)                 # banner above it
    d.text((180, 128), "ColumbaPalumbus 0.97", fill=(250, 255, 240))
    boxed_path = tmp_path / "boxed.jpg"
    boxed.save(boxed_path, quality=95)
    return str(clean_path), str(boxed_path)


class _Detector:
    """Proposes one box over the animal, as RT-DETR would."""

    def detect(self, clean_image):
        return DetectorRead(provider="rtdetr", model_name="stub", ok=True,
                            candidates=[DetectorBox(box=(178, 148, 242, 212),
                                                    score=0.93, label="bird")])


class _Capture:
    """Stands in for BioCLIP: records the bytes it was handed, and nothing else."""

    def __init__(self):
        self.payload = None
        self.call_kwargs = None

    def classify(self, image_bytes):
        self.payload = image_bytes
        return None


def test_crop_handed_to_bioclip_contains_no_overlay_green(tmp_path):
    clean_path, boxed_path = _frames(tmp_path)
    spy = _Capture()
    out = process_event(1, "ColumbaPalumbus", 0.97, clean_path, boxed_path,
                        resolver=None, detector=_Detector(), taxonomic=spy,
                        tau=0.30, pad_frac=0.15)

    # No resolver, so there is no corroboration -- but a single candidate is
    # still safe to crop, and the crop must come from the clean frame.
    assert out.decision.status == "cropped_unverified"
    assert spy.payload, "BioCLIP was handed nothing"

    crop = np.asarray(Image.open(io.BytesIO(spy.payload)).convert("RGB"), dtype=np.int16)
    overlay_px = int((np.abs(crop - np.array(GREEN)).max(axis=2) < GTOL).sum())
    assert overlay_px == 0, f"{overlay_px} overlay-green pixels leaked into the crop"


def test_the_same_region_of_the_BOXED_frame_would_have_leaked(tmp_path):
    """Control: the guarantee comes from cropping the clean frame, not from luck.

    Cropping the identical rectangle out of the annotated frame does pick up
    overlay pixels — which is what the design avoids by construction.
    """
    _clean_path, boxed_path = _frames(tmp_path)
    boxed = np.asarray(Image.open(boxed_path).convert("RGB"), dtype=np.int16)
    x0, y0, x1, y1 = 178 - 9, 148 - 9, 242 + 9, 212 + 9        # the padded crop
    region = boxed[y0:y1 + 1, x0:x1 + 1]
    leaked = int((np.abs(region - np.array(GREEN)).max(axis=2) < GTOL).sum())
    assert leaked > 0, "control failed: the synthetic overlay is not where expected"


def test_full_frame_fallback_is_also_the_clean_frame(tmp_path):
    """When no crop is taken, BioCLIP still sees the clean original — never the
    annotated one. An uncropped event must not be a contaminated event."""
    clean_path, boxed_path = _frames(tmp_path)

    class _NoBoxes:
        def detect(self, clean_image):
            return DetectorRead(provider="rtdetr", model_name="stub", ok=True,
                                candidates=[])

    spy = _Capture()
    out = process_event(1, "ColumbaPalumbus", 0.97, clean_path, boxed_path,
                        resolver=None, detector=_NoBoxes(), taxonomic=spy,
                        tau=0.30, pad_frac=0.15)
    assert out.decision.status == "uncropped_no_detection"
    assert out.decision.crop_box is None
    frame = np.asarray(Image.open(io.BytesIO(spy.payload)).convert("RGB"), dtype=np.int16)
    assert int((np.abs(frame - np.array(GREEN)).max(axis=2) < GTOL).sum()) == 0
