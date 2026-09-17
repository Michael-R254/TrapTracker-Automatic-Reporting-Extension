"""Phase 3: the cropped read's cross-check is a PARALLEL trail, never the verdict.

Option B reports the two reads side by side as a framing-sensitivity finding. It
does not restate the published number, because the change is not one-way — 17
events that agree on the full frame disagree on the crop. The property these tests
pin is that filling the ``*_crop`` columns cannot move ``agreement_flag``.

The two existing tripwires stay untouched by design and are asserted here to still
be in place: ``test_crosscheck_corpus.py``'s hard-coded 110/677, and
``recompute-crosscheck``'s reversal guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from ttr.storage.repository import DetectionRepository

PARALLEL = {"agreement_flag_crop", "agreement_rationale_crop", "cross_check_status_crop",
            "resolution_basis_crop", "matched_rank_crop", "taxonomic_distance_crop"}
PUBLISHED = {"agreement_flag", "agreement_rationale", "cross_check_status",
             "resolution_basis", "matched_rank", "taxonomic_distance"}


@dataclass
class _Check:
    flag: str = "agree"
    rationale: str = "exact species match"
    status: str = "evaluable"
    resolution_basis: str = "exact_species_match"
    matched_rank: Optional[str] = "species"
    taxonomic_distance: str = "same_species"


@dataclass
class _Lineage:
    kingdom: str = "Animalia"
    class_name: str = "Aves"
    order: str = "Columbiformes"
    family: str = "Columbidae"
    genus: str = "Columba"


@pytest.fixture()
def repo(tmp_path: Path):
    r = DetectionRepository(tmp_path / "t.db")
    yield r
    r.close()


def _insert(repo, flag="disagree", distance="non_animal") -> int:
    repo._conn.execute(
        "INSERT INTO detection_events (source_type, source_message_id, ingested_at_utc,"
        " images_present, provenance_json, created_at_utc, upstream_label,"
        " agreement_flag, agreement_rationale, cross_check_status, resolution_basis,"
        " matched_rank, taxonomic_distance, bioclip_topk_json)"
        " VALUES ('email', 'm1', '2026-01-01T00:00:00+00:00', 'both', '{}',"
        " '2026-01-01T00:00:00+00:00', 'ColumbaPalumbus', ?, 'no match', 'evaluable',"
        " 'unresolved_prediction', NULL, ?, '[[\"Poa pratensis\", 0.4]]')",
        (flag, distance))
    repo._conn.commit()
    return int(repo._conn.execute("SELECT id FROM detection_events").fetchone()["id"])


def test_parallel_columns_exist_and_start_null(repo):
    cols = {r[1] for r in repo._conn.execute("PRAGMA table_info(detection_events)")}
    assert PARALLEL <= cols
    eid = _insert(repo)
    row = repo._conn.execute("SELECT * FROM detection_events WHERE id=?", (eid,)).fetchone()
    for c in PARALLEL:
        assert row[c] is None, f"{c} should be NULL until the comparison runs"


def test_writing_the_crop_trail_leaves_the_published_verdict_alone(repo):
    """The whole point of Option B: the comparison is additive."""
    eid = _insert(repo, flag="disagree", distance="non_animal")
    before = repo._conn.execute("SELECT * FROM detection_events WHERE id=?",
                                (eid,)).fetchone()

    # The cropped read reaches the OPPOSITE verdict — the hardest case.
    repo.set_crop_crosscheck(eid, _Check(flag="agree"), _Lineage())

    after = repo._conn.execute("SELECT * FROM detection_events WHERE id=?",
                               (eid,)).fetchone()
    for c in PUBLISHED:
        assert after[c] == before[c], f"published column {c} was modified"
    assert after["bioclip_topk_json"] == before["bioclip_topk_json"]
    # ...and the parallel trail now carries the cropped decision.
    assert after["agreement_flag_crop"] == "agree"
    assert after["taxonomic_distance_crop"] == "same_species"
    assert after["bioclip_crop_top1_genus"] == "Columba"


def test_a_reversal_is_storable_rather_than_rejected(repo):
    """agree -> disagree is a regression for the PUBLISHED verdict, which is why
    recompute-crosscheck reports it. On the parallel trail it is ordinary data:
    17 such events exist and the finding is precisely that they exist."""
    eid = _insert(repo, flag="agree", distance="same_species")
    repo.set_crop_crosscheck(eid, _Check(flag="disagree",
                                         taxonomic_distance="same_genus",
                                         resolution_basis="unresolved_prediction"))
    row = repo._conn.execute("SELECT * FROM detection_events WHERE id=?", (eid,)).fetchone()
    assert row["agreement_flag"] == "agree"              # published, unmoved
    assert row["agreement_flag_crop"] == "disagree"      # cropped, recorded
    assert row["taxonomic_distance_crop"] == "same_genus"


def test_the_two_existing_tripwires_are_still_in_place():
    """Option B leaves both untouched. If a later phase moves the published number,
    these are what should stop it happening quietly — so their continued existence
    is itself worth asserting."""
    corpus = Path("tests/test_crosscheck_corpus.py").read_text(encoding="utf-8")
    assert "_EXPECTED_AGREE = 110" in corpus
    assert "_EXPECTED_DISAGREE = 677" in corpus

    cli = Path("src/ttr/cli.py").read_text(encoding="utf-8")
    assert "must not swap sides" in cli, "the reversal guard's rationale went missing"
    assert 'if old_flag in ("agree", "disagree") and check.flag != old_flag:' in cli


def test_the_crop_command_never_writes_the_published_columns():
    """Static guard: no UPDATE in the crop cross-check path may name them."""
    src = Path("src/ttr/storage/repository.py").read_text(encoding="utf-8")
    start = src.index("def set_crop_crosscheck")
    body = src[start:src.index("def iter_for_crop_crosscheck")]
    for column in PUBLISHED:
        assert f"{column} = ?" not in body, \
            f"set_crop_crosscheck writes the published column {column}"
    for column in PARALLEL:
        assert f"{column} = ?" in body, f"set_crop_crosscheck does not write {column}"
