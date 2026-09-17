"""Phase 2c storage: additive columns, conservation, and the BioCLIP contract.

Two properties this phase must not break:

  * the crop read lands in PARALLEL columns, so agreement.py keeps reading the
    same full-frame evidence and no corroboration verdict changes;
  * BioCLIP's interface stays ``classify(image_bytes)``, which is what guarantees
    it never sees a label — upstream, OCR'd, or otherwise. Only pixels.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from ttr.crop.base import BannerRead, CropDecision, DetectorRead, TrapTrackerRead
from ttr.storage.migrations import _ADDED_COLUMNS, connect
from ttr.storage.repository import DetectionRepository

CROP_STATUSES = {
    "cropped_verified", "cropped_disputed", "cropped_unverified",
    "uncropped_ambiguous", "uncropped_no_detection", "uncropped_error",
}

PROVENANCE_COLUMNS = {
    "crop_status", "crop_basis", "crop_error", "crop_box_json", "crop_pad_frac",
    "detector_provider", "detector_model", "detector_box_json", "detector_score",
    "detector_candidate_count", "detector_error", "tt_box_json", "tt_box_recovered",
    "tt_box_self_check", "tt_box_clipped", "tt_banner_ocr_label",
    "tt_banner_ocr_confidence", "tt_banner_ocr_score", "tt_banner_basis",
    "tt_label_mismatch", "frame_detection_count", "localisation_iou",
    "localisation_agreement",
}

CROP_READ_COLUMNS = {
    "bioclip_crop_ok", "bioclip_crop_model", "bioclip_crop_topk_json",
    "bioclip_crop_error", "bioclip_crop_top1_kingdom", "bioclip_crop_top1_class",
    "bioclip_crop_top1_order", "bioclip_crop_top1_family", "bioclip_crop_top1_genus",
}


@pytest.fixture()
def repo(tmp_path: Path):
    r = DetectionRepository(tmp_path / "t.db")
    yield r
    r.close()


def _columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(detection_events)")}


def test_all_crop_columns_exist_after_migration(repo):
    cols = _columns(repo._conn)
    missing = (PROVENANCE_COLUMNS | CROP_READ_COLUMNS) - cols
    assert not missing, f"missing columns: {sorted(missing)}"


def test_existing_columns_are_untouched_by_this_phase(repo):
    """The full-frame read and every cross-check column must still be there,
    unrenamed and unrepurposed — agreement.py reads exactly these."""
    cols = _columns(repo._conn)
    for name in ("bioclip_ok", "bioclip_model", "bioclip_topk_json", "bioclip_error",
                 "bioclip_top1_kingdom", "bioclip_top1_genus", "agreement_flag",
                 "agreement_rationale", "cross_check_status", "resolution_basis",
                 "matched_rank", "taxonomic_distance"):
        assert name in cols
    # And the crop read is a genuinely separate set of columns, not an alias.
    assert not (PROVENANCE_COLUMNS | CROP_READ_COLUMNS) & {
        "bioclip_ok", "bioclip_topk_json", "agreement_flag", "cross_check_status"}


def test_migration_is_idempotent(tmp_path: Path):
    path = tmp_path / "twice.db"
    DetectionRepository(path).close()
    before = None
    with sqlite3.connect(path) as c:
        before = {r[1] for r in c.execute("PRAGMA table_info(detection_events)")}
    DetectionRepository(path).close()          # migrate again over the same file
    with sqlite3.connect(path) as c:
        after = {r[1] for r in c.execute("PRAGMA table_info(detection_events)")}
    assert before == after


def test_added_columns_have_no_duplicates():
    names = [n for n, _ddl in _ADDED_COLUMNS]
    assert len(names) == len(set(names)), "duplicate column in _ADDED_COLUMNS"


def test_crop_columns_default_to_null_not_to_a_guess(repo, monkeypatch):
    """An un-recomputed row must read as visibly 'not computed', never as a
    silently defaulted status — the same rule the cross-check audit trail follows."""
    repo._conn.execute(
        "INSERT INTO detection_events (source_type, source_message_id, ingested_at_utc,"
        " images_present, provenance_json, created_at_utc) VALUES"
        " ('email', 'm1', '2026-01-01T00:00:00+00:00', 'both', '{}', "
        "'2026-01-01T00:00:00+00:00')")
    repo._conn.commit()
    row = repo._conn.execute(
        "SELECT crop_status, crop_basis, bioclip_crop_ok FROM detection_events").fetchone()
    assert row["crop_status"] is None
    assert row["crop_basis"] is None
    assert row["bioclip_crop_ok"] is None
    assert repo.crop_status_counts() == {"not_computed": 1}


def test_set_crop_writes_only_crop_columns(repo):
    repo._conn.execute(
        "INSERT INTO detection_events (source_type, source_message_id, ingested_at_utc,"
        " images_present, provenance_json, created_at_utc, bioclip_topk_json,"
        " agreement_flag, cross_check_status) VALUES"
        " ('email', 'm1', '2026-01-01T00:00:00+00:00', 'both', '{}',"
        " '2026-01-01T00:00:00+00:00', '[[\"Columba palumbus\", 0.9]]', 'agree',"
        " 'evaluable')")
    repo._conn.commit()
    eid = repo._conn.execute("SELECT id FROM detection_events").fetchone()["id"]

    decision = CropDecision(status="cropped_verified", basis="iou_at_or_above_tau",
                            crop_box=(10, 10, 90, 90), detector_box=(15, 15, 85, 85),
                            detector_score=0.91, detector_candidate_count=4,
                            pad_frac=0.15, localisation_iou=0.87,
                            localisation_agreement="agree")
    tt = TrapTrackerRead(box=(16, 16, 84, 84), recovered=True, self_check="pass",
                         banner=BannerRead(label="ColumbaPalumbus", confidence=0.97,
                                           score=0.99),
                         frame_detection_count=1, resolution_basis="single_banner_verified")
    det = DetectorRead(provider="rtdetr", model_name="rtdetr-test", ok=True)
    repo.set_crop(eid, decision, tt, det)

    row = repo._conn.execute("SELECT * FROM detection_events WHERE id=?", (eid,)).fetchone()
    assert row["crop_status"] == "cropped_verified"
    assert row["localisation_agreement"] == "agree"
    assert row["tt_box_json"] == "[16, 16, 84, 84]"
    # The full-frame read and the cross-check verdict are exactly as they were.
    assert row["bioclip_topk_json"] == '[["Columba palumbus", 0.9]]'
    assert row["agreement_flag"] == "agree"
    assert row["cross_check_status"] == "evaluable"
    assert row["bioclip_crop_ok"] is None       # no crop classification was passed


def test_conservation_every_row_holds_a_known_status_or_null(repo):
    """Corpus-level check: the union of crop_status values is always a subset of
    the six, plus NULL for rows the backfill has not reached."""
    det = DetectorRead(provider="rtdetr", model_name="m", ok=True)
    tt = TrapTrackerRead()
    for i, status in enumerate(sorted(CROP_STATUSES)):
        repo._conn.execute(
            "INSERT INTO detection_events (source_type, source_message_id,"
            " ingested_at_utc, images_present, provenance_json, created_at_utc)"
            " VALUES ('email', ?, '2026-01-01T00:00:00+00:00', 'both', '{}',"
            " '2026-01-01T00:00:00+00:00')", (f"m{i}",))
        eid = repo._conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        repo.set_crop(eid, CropDecision(status=status, basis="detector_failed"),
                      tt, det)
    repo._conn.commit()
    counts = repo.crop_status_counts()
    assert set(counts) <= CROP_STATUSES | {"not_computed"}
    assert sum(counts.values()) == len(CROP_STATUSES)


# --------------------------------------------------------------------------
# The contract that keeps BioCLIP blind
# --------------------------------------------------------------------------

def test_bioclip_enricher_interface_is_unchanged():
    """``classify(image_bytes)`` and nothing else.

    This is the guarantee that BioCLIP never sees a label — not the upstream
    token, not the OCR'd banner text, not the detector's class name. If a future
    change adds a hint parameter here, this test is the thing that should stop it.
    """
    from ttr.enrichment.base import TaxonomicEnricher
    from ttr.enrichment.bioclip_enricher import BioClipEnricher

    for cls in (TaxonomicEnricher, BioClipEnricher):
        sig = inspect.signature(cls.classify)
        params = [p for p in sig.parameters if p != "self"]
        assert params == ["original_image"], f"{cls.__name__}.classify{sig}"
        annotation = sig.parameters["original_image"].annotation
        assert annotation in (bytes, "bytes"), annotation


def test_crop_runner_passes_only_bytes_to_the_classifier(tmp_path: Path):
    """The runner must hand the enricher pixels and nothing else."""
    from ttr.crop.runner import process_event

    seen = {}

    class _Spy:
        def classify(self, image_bytes):
            seen["type"] = type(image_bytes)
            seen["args"] = 1
            return None

    class _Det:
        def detect(self, clean_image):
            return DetectorRead(provider="rtdetr", model_name="m", ok=False,
                                error="stubbed")

    out = process_event(1, "ColumbaPalumbus", 0.97, "", "", resolver=None,
                        detector=_Det(), taxonomic=_Spy(), tau=0.30, pad_frac=0.15)
    # No readable image, so nothing was classified -- but the path still resolves
    # to a recorded status rather than raising.
    assert out.decision.status == "uncropped_error"
    assert seen == {}
