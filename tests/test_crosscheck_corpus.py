"""Conservation of events across the two-state cross-check.

The failure this guards against is silent: a decision function with an unhandled
path returns nothing for some rows, the headline counts still look plausible, and
the corpus quietly shrinks. So the invariant is asserted directly — every event
lands in exactly one of agree / disagree / not_evaluable, and the three sum back to
the total.

Two layers:
  - a SYNTHETIC corpus covering every outcome shape, which runs everywhere;
  - the REAL 787-event corpus, which runs only where that database is present
    (it is gitignored, so CI skips it) and pins the published figures.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from ttr.enrichment.agreement import RESOLUTION_BASES, compute_crosscheck
from ttr.enrichment.base import TaxonomicRead
from ttr.species.taxonomy import DISTANCES

_LIVE_DB = Path(__file__).resolve().parents[1] / "data" / "live_ingest.db"

#: The figures this change produced, recomputed over the full corpus. Pinned so a
#: later edit to the matching layer cannot move them without the diff saying so.
_EXPECTED_TOTAL = 787
_EXPECTED_AGREE = 110
_EXPECTED_DISAGREE = 677
_EXPECTED_NOT_EVALUABLE = 0


def _read(top1, *, ok=True, error=None, lineage=None) -> TaxonomicRead:
    return TaxonomicRead(
        provider="bioclip", model_name="test",
        topk=[(top1, 0.9)] if top1 else [],
        topk_lineages=[lineage] if lineage else [],
        ok=ok, error=error,
    )


# --------------------------------------------------------------------------- #
# Synthetic corpus — runs everywhere, no data files, no weights
# --------------------------------------------------------------------------- #
def test_every_event_lands_in_exactly_one_bucket(alias_map, target_taxonomy):
    fox = {"kingdom": "Animalia", "class": "Mammalia", "order": "Carnivora",
           "family": "Canidae", "genus": "Vulpes", "species_epithet": "vulpes"}
    grass = {"kingdom": "Plantae", "class": "Liliopsida", "order": "Poales",
             "family": "Poaceae", "genus": "Poa", "species_epithet": "pratensis"}
    corpus = [
        ("VulpesVulpes", _read("Vulpes vulpes", lineage=fox)),          # agree
        ("VulpesVulpes", _read("Meles meles")),                          # disagree (known)
        ("VulpesVulpes", _read("Panthera leo")),                         # disagree (unresolved)
        ("VulpesVulpes", _read("Poa pratensis", lineage=grass)),         # disagree (non-animal)
        ("UrsusArctos", _read("Ursus arctos syriacus")),                 # agree (subspecies)
        ("Person", _read("Vulpes vulpes", lineage=fox)),                 # not_evaluable
        ("GallusGallus", _read("Vulpes vulpes", lineage=fox)),           # not_evaluable
        (None, _read("Vulpes vulpes", lineage=fox)),                     # not_evaluable
        ("VulpesVulpes", _read("", ok=False, error="boom")),             # not_evaluable
        ("VulpesVulpes", _read("")),                                     # not_evaluable
    ]
    flags = Counter()
    for label, taxo in corpus:
        check = compute_crosscheck(label, taxo, alias_map, target_taxonomy)
        flags[check.flag] += 1
        # Every outcome is fully described — no field left unset to be counted as
        # something else downstream.
        assert check.resolution_basis in RESOLUTION_BASES
        assert check.taxonomic_distance in DISTANCES
        assert check.matched_rank in {"species", "none"}
        assert check.rationale
        assert check.is_evaluable == (check.flag != "not_evaluable")
        assert check.is_agree == (check.flag == "agree")

    assert sum(flags.values()) == len(corpus)
    assert flags["agree"] + flags["disagree"] + flags["not_evaluable"] == len(corpus)
    assert flags["agree"] == 2
    assert flags["disagree"] == 3
    assert flags["not_evaluable"] == 5


def test_only_species_level_bases_produce_agree(alias_map, target_taxonomy):
    """No basis other than a species-level match may return ``agree`` — the whole
    point of keeping the verdict strict."""
    agreeing = {"exact_species_match", "subspecies_collapsed_match", "topk_species_match"}
    cases = [
        ("VulpesVulpes", _read("Vulpes vulpes")),
        ("VulpesVulpes", _read("Vulpes lagopus")),
        ("VulpesVulpes", _read("Meles meles")),
        ("VulpesVulpes", _read("Panthera leo")),
        ("UrsusArctos", _read("Ursus arctos syriacus")),
        ("Person", _read("Vulpes vulpes")),
    ]
    for label, taxo in cases:
        check = compute_crosscheck(label, taxo, alias_map, target_taxonomy)
        assert check.is_agree == (check.resolution_basis in agreeing)
        if check.is_agree:
            assert check.matched_rank == "species"
            assert check.taxonomic_distance == "same_species"


def test_decision_is_deterministic(alias_map, target_taxonomy):
    taxo = _read("Panthera leo")
    first = compute_crosscheck("VulpesVulpes", taxo, alias_map, target_taxonomy)
    for _ in range(5):
        assert compute_crosscheck("VulpesVulpes", taxo, alias_map, target_taxonomy) == first


# --------------------------------------------------------------------------- #
# Real corpus — skipped where the (gitignored) database is absent
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _LIVE_DB.exists(), reason="live ingest corpus not present")
def test_stored_corpus_is_two_state_and_conserved():
    conn = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT agreement_flag, cross_check_status, resolution_basis, "
            "taxonomic_distance FROM detection_events").fetchall()
    finally:
        conn.close()

    flags = Counter(r["agreement_flag"] for r in rows)
    assert len(rows) == _EXPECTED_TOTAL
    assert set(flags) <= {"agree", "disagree", "not_evaluable"}, \
        f"a retired verdict is still stored: {sorted(set(flags))}"
    assert flags["agree"] == _EXPECTED_AGREE
    assert flags["disagree"] == _EXPECTED_DISAGREE
    assert flags["not_evaluable"] == _EXPECTED_NOT_EVALUABLE
    # No row dropped, none double-counted.
    assert (flags["agree"] + flags["disagree"] + flags["not_evaluable"]) == len(rows)

    # Status and flag must not disagree about whether a row was evaluable.
    for r in rows:
        evaluable = r["cross_check_status"] == "evaluable"
        assert evaluable == (r["agreement_flag"] != "not_evaluable")

    # Every row carries its audit trail, or the breakdown the report prints is
    # computed over holes.
    assert all(r["resolution_basis"] in RESOLUTION_BASES for r in rows)
    assert all(r["taxonomic_distance"] in DISTANCES for r in rows)


@pytest.mark.skipif(not _LIVE_DB.exists(), reason="live ingest corpus not present")
def test_stored_corpus_agreements_are_all_exact_species_matches():
    """The regression guard for the change itself: better matching moved rows OUT
    of the retired indeterminate state, and did not reclassify anything as an
    agreement on weaker evidence than an identical binomial."""
    conn = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT upstream_label, canonical_binomial, bioclip_topk_json, "
            "resolution_basis, taxonomic_distance FROM detection_events "
            "WHERE agreement_flag = 'agree'").fetchall()
    finally:
        conn.close()

    assert len(rows) == _EXPECTED_AGREE
    for r in rows:
        assert r["resolution_basis"] in {"exact_species_match", "subspecies_collapsed_match"}
        assert r["taxonomic_distance"] == "same_species"
        top1 = json.loads(r["bioclip_topk_json"])[0][0]
        # The binomial itself agrees — asserted from the stored evidence rather
        # than trusting the flag that was written beside it.
        assert " ".join(top1.split()[:2]).casefold() == r["canonical_binomial"].casefold()
