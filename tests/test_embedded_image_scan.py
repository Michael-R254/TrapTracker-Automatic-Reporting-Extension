"""The photograph scan's cleared-file list, and the one thing it must not do.

`REVIEWED` exists so six reviewed screenshots stop sitting in the uncertain
column forever, where they would teach a reader to skip the column. The risk it
introduces is obvious and is the thing pinned hardest here: a list that could
silence a PHOTOGRAPH would be worse than no list, because the scan's whole job is
to catch one.

The scan needs Pillow, which CI does not install (it lives in the `enrich`
extra), so these skip there rather than fail.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "docs" / "evaluation" / "embedded_image_scan.py"


@pytest.fixture(scope="module")
def scan():
    pytest.importorskip("PIL", reason="the scan needs Pillow (the enrich extra)")
    spec = importlib.util.spec_from_file_location("embedded_image_scan", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["embedded_image_scan"] = module
    spec.loader.exec_module(module)
    return module


def _png(colours: int) -> bytes:
    """A PNG sampling roughly `colours` distinct values."""
    import io

    from PIL import Image

    im = Image.new("RGB", (64, 64))
    im.putdata([(i % colours, (i * 7) % colours, (i * 13) % colours)
                for i in range(64 * 64)])
    buffer = io.BytesIO()
    im.save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# The list is honest about what it covers.
# --------------------------------------------------------------------------- #
def test_every_cleared_path_exists(scan):
    """A list naming files that are gone is a list nobody can check."""
    missing = [p for p in scan.REVIEWED if not (REPO / p).is_file()]

    assert missing == [], f"REVIEWED names files that are not here: {missing}"


def test_every_cleared_entry_says_when_and_what(scan):
    for path, reason in scan.REVIEWED.items():
        assert "reviewed 20" in reason, f"{path}: no review date"
        assert len(reason) > 30, f"{path}: reason too thin to be a review"


def test_the_screenshots_are_the_files_it_covers(scan):
    assert all(p.startswith("docs/screenshots/") for p in scan.REVIEWED)
    assert len(scan.REVIEWED) == len(list((REPO / "docs" / "screenshots").glob("*.png")))


# --------------------------------------------------------------------------- #
# What it must never do.
# --------------------------------------------------------------------------- #
def test_a_cleared_file_that_becomes_a_photograph_is_still_a_photograph(scan):
    """The whole risk of the list, in one test.

    Clearing applies to the uncertain band. A photograph at a cleared path means
    the file changed since someone looked at it, so it must still be fatal.
    """
    photo = scan.sampled_colours(_png(2000))
    assert photo is not None
    assert photo[2] >= scan.PHOTO_COLOURS
    assert scan.classify(photo[2]) == "PHOTOGRAPH"

    # The classifier is path-blind on purpose: clearing happens afterwards, in
    # `main`, and only for "uncertain".
    for colours in (photo[2], scan.PHOTO_COLOURS, scan.PHOTO_COLOURS + 1):
        assert scan.classify(colours) == "PHOTOGRAPH"


def test_the_bands_are_unchanged_by_the_list(scan):
    assert scan.classify(scan.UNCERTAIN_COLOURS - 1) == "synthetic"
    assert scan.classify(scan.UNCERTAIN_COLOURS) == "uncertain"
    assert scan.classify(scan.PHOTO_COLOURS - 1) == "uncertain"
    assert scan.classify(scan.PHOTO_COLOURS) == "PHOTOGRAPH"


def test_the_reviewed_screenshots_are_in_the_uncertain_band_not_the_photo_one(scan):
    """Why the list was needed at all: a UI screenshot lands between a chart and
    a camera frame, in the band the scan deliberately refuses to judge."""
    for path in scan.REVIEWED:
        images = list(scan.images_in(REPO / path))
        assert len(images) == 1, f"{path}: expected one raster"
        _, data = images[0]
        width, height, colours = scan.sampled_colours(data)
        assert scan.UNCERTAIN_COLOURS <= colours < scan.PHOTO_COLOURS, (
            f"{path} samples {colours} colours: it is no longer an uncertain-band "
            f"file, so the clearing note no longer describes it")
