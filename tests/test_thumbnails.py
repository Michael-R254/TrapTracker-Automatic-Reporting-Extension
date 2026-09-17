"""The shared thumbnail helper — one implementation, two consumers.

It feeds both the report's Appendix-A grid and the ingest page's live strip, and
it must never raise: a missing file, an undecodable blob or a core-only install
without Pillow all degrade to None so the caller renders a caption-only card.
"""

from __future__ import annotations

import pytest

from ttr.thumbnails import thumb_data_uri


def test_absent_and_missing_paths_return_none():
    assert thumb_data_uri(None) is None
    assert thumb_data_uri("") is None
    assert thumb_data_uri("no/such/file.jpg") is None


def test_a_non_image_file_returns_none_rather_than_raising(tmp_path):
    junk = tmp_path / "not-an-image.jpg"
    junk.write_bytes(b"this is definitely not a JPEG")
    assert thumb_data_uri(junk) is None


def test_a_directory_returns_none(tmp_path):
    assert thumb_data_uri(tmp_path) is None


def test_report_generator_still_delegates_here():
    """The report's private helper must keep working after the extraction."""
    from ttr.agents.report import ReportGeneratorAgent

    assert ReportGeneratorAgent._thumb_data_uri(None) is None
    assert ReportGeneratorAgent._thumb_data_uri("no/such/file.jpg") is None


def test_a_real_image_becomes_a_bounded_jpeg_data_uri(tmp_path):
    Image = pytest.importorskip("PIL.Image")      # Pillow is in the [enrich] extra
    path = tmp_path / "big.png"
    Image.new("RGB", (900, 600), (30, 90, 60)).save(path)

    uri = thumb_data_uri(path, max_px=64)
    assert uri.startswith("data:image/jpeg;base64,")

    import base64
    import io
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as thumb:
        assert max(thumb.size) <= 64             # actually resized, not just re-encoded
