"""Stage 3 verification: the cross-check is TWO-STATE and joins on the binomial.

``agree`` means the same species and nothing else — a genus- or family-level near
miss is still a ``disagree`` (that is the definitional commitment; loosening it
would make "corroborated" indefensible). What a near miss gets instead is a
measured ``taxonomic_distance``, asserted here so the audit breakdown that replaced
the retired ``indeterminate`` bucket cannot silently drift.

The only third value is ``not_evaluable``, and it is a DENOMINATOR, not a verdict:
it marks the rows where no comparison was possible at all (Decision 2).
"""

from __future__ import annotations

import pytest

from ttr.enrichment.agreement import compute_agreement, compute_crosscheck
from ttr.enrichment.base import TaxonomicRead

# Lineages for the taxa the fixtures predict, so a distance can be measured.
_LINEAGES = {
    "Vulpes vulpes": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                      "order": "Carnivora", "family": "Canidae",
                      "genus": "Vulpes", "species_epithet": "vulpes"},
    "Vulpes lagopus": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                       "order": "Carnivora", "family": "Canidae",
                       "genus": "Vulpes", "species_epithet": "lagopus"},
    "Canis lupus": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                    "order": "Carnivora", "family": "Canidae",
                    "genus": "Canis", "species_epithet": "lupus"},
    "Meles meles": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                    "order": "Carnivora", "family": "Mustelidae",
                    "genus": "Meles", "species_epithet": "meles"},
    "Bos taurus": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                   "order": "Artiodactyla", "family": "Bovidae",
                   "genus": "Bos", "species_epithet": "taurus"},
    "Panthera leo": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                     "order": "Carnivora", "family": "Felidae",
                     "genus": "Panthera", "species_epithet": "leo"},
    "Capreolus capreolus": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                            "order": "Artiodactyla", "family": "Cervidae",
                            "genus": "Capreolus", "species_epithet": "capreolus"},
    "Corvus corone": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Aves",
                      "order": "Passeriformes", "family": "Corvidae",
                      "genus": "Corvus", "species_epithet": "corone"},
    "Salmo salar": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Actinopterygii",
                    "order": "Salmoniformes", "family": "Salmonidae",
                    "genus": "Salmo", "species_epithet": "salar"},
    "Poa pratensis": {"kingdom": "Plantae", "phylum": "Tracheophyta", "class": "Liliopsida",
                      "order": "Poales", "family": "Poaceae",
                      "genus": "Poa", "species_epithet": "pratensis"},
    "Ursus arctos syriacus": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                              "order": "Carnivora", "family": "Ursidae",
                              "genus": "Ursus", "species_epithet": "arctos syriacus"},
}


def _taxo(top1: str, *, ok: bool = True, error=None) -> TaxonomicRead:
    """A read carrying the top-1 AND its lineage, as the live enricher now does."""
    lineage = _LINEAGES.get(top1)
    return TaxonomicRead(
        provider="bioclip", model_name="test",
        topk=[(top1, 0.9)] if top1 else [],
        topk_lineages=[lineage] if lineage else [],
        ok=ok, error=error,
    )


# --------------------------------------------------------------------------- #
# The binary verdict
# --------------------------------------------------------------------------- #
def test_agree_when_binomials_match(alias_map, target_taxonomy):
    check = compute_crosscheck("VulpesVulpes", _taxo("Vulpes vulpes"), alias_map, target_taxonomy)
    assert check.flag == "agree"
    assert check.status == "evaluable"
    assert check.resolution_basis == "exact_species_match"
    assert check.matched_rank == "species"
    assert check.taxonomic_distance == "same_species"
    assert "Vulpes vulpes" in check.rationale


def test_disagree_when_binomials_differ(alias_map, target_taxonomy):
    check = compute_crosscheck("VulpesVulpes", _taxo("Meles meles"), alias_map, target_taxonomy)
    assert check.flag == "disagree"
    assert check.resolution_basis == "no_taxonomic_match"
    assert check.matched_rank == "none"
    assert "Vulpes vulpes" in check.rationale and "Meles meles" in check.rationale


def test_case_insensitive_join(alias_map, target_taxonomy):
    check = compute_crosscheck("vulpesvulpes", _taxo("VULPES VULPES"), alias_map, target_taxonomy)
    assert check.flag == "agree"


def test_subspecies_trinomial_reduces_to_species(alias_map, target_taxonomy):
    check = compute_crosscheck("UrsusArctos", _taxo("Ursus arctos syriacus"),
                               alias_map, target_taxonomy)
    assert check.flag == "agree"
    # Recorded under its own basis so a collapsed subspecies stays distinguishable
    # from an exact hit in the audit.
    assert check.resolution_basis == "subspecies_collapsed_match"
    assert check.taxonomic_distance == "same_species"


def test_comparable_domestic_still_agrees(alias_map, target_taxonomy):
    check = compute_crosscheck("BosTaurus", _taxo("Bos taurus"), alias_map, target_taxonomy)
    assert check.flag == "agree"


# --------------------------------------------------------------------------- #
# The old 'indeterminate' rows are now DISAGREEMENTS — this is the behaviour
# change, so it is asserted head-on rather than inferred from a count.
# --------------------------------------------------------------------------- #
def test_unmappable_bioclip_label_is_disagree_not_a_third_state(alias_map, target_taxonomy):
    # Panthera leo is a real binomial but absent from this small alias table.
    # It used to be 'indeterminate'. BioCLIP named a species and it was not the
    # upstream one, so it is a disagreement.
    check = compute_crosscheck("VulpesVulpes", _taxo("Panthera leo"), alias_map, target_taxonomy)
    assert check.flag == "disagree"
    assert check.status == "evaluable"
    assert check.resolution_basis == "unresolved_prediction"
    assert "Panthera leo" in check.rationale


def test_no_verdict_is_ever_indeterminate(alias_map, target_taxonomy):
    for label, taxo in (
        ("VulpesVulpes", _taxo("Vulpes vulpes")),
        ("VulpesVulpes", _taxo("Panthera leo")),
        ("VulpesVulpes", _taxo("Poa pratensis")),
        ("Person", _taxo("Vulpes vulpes")),
        ("VulpesVulpes", _taxo("", ok=False, error="boom")),
    ):
        flag = compute_crosscheck(label, taxo, alias_map, target_taxonomy).flag
        assert flag in {"agree", "disagree", "not_evaluable"}


# --------------------------------------------------------------------------- #
# Taxonomic distance — the audit breakdown that replaced 'indeterminate'
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("top1,expected", [
    ("Vulpes vulpes", "same_species"),
    ("Vulpes lagopus", "same_genus"),      # same genus, different species
    ("Canis lupus", "same_family"),        # Canidae, different genus
    ("Meles meles", "same_order"),         # Carnivora, different family
    ("Capreolus capreolus", "same_class"), # Mammalia, different order
    ("Corvus corone", "other_animal"),     # Animalia, different class
    ("Salmo salar", "other_animal"),
    ("Poa pratensis", "non_animal"),       # scene content, not the animal
])
def test_distance_is_measured_at_the_nearest_shared_rank(
        alias_map, target_taxonomy, top1, expected):
    check = compute_crosscheck("VulpesVulpes", _taxo(top1), alias_map, target_taxonomy)
    assert check.taxonomic_distance == expected
    # Only an exact species match may be an agreement — a near miss must not be.
    assert check.is_agree == (expected == "same_species")


def test_near_miss_in_the_same_genus_is_still_a_disagreement(alias_map, target_taxonomy):
    """The definitional commitment, asserted on its own so it cannot regress:
    'corroborated' means the same species, never merely a close relative."""
    check = compute_crosscheck("VulpesVulpes", _taxo("Vulpes lagopus"), alias_map, target_taxonomy)
    assert check.flag == "disagree"
    assert check.taxonomic_distance == "same_genus"
    assert check.matched_rank == "none"


def test_distance_is_undetermined_without_a_lineage(alias_map, target_taxonomy):
    # A row stored before hierarchy capture: verdict still decided, distance not
    # invented from the binomial.
    taxo = TaxonomicRead(provider="bioclip", model_name="test", ok=True,
                         topk=[("Panthera leo", 0.9)])
    check = compute_crosscheck("VulpesVulpes", taxo, alias_map, target_taxonomy)
    assert check.flag == "disagree"
    assert check.taxonomic_distance == "undetermined"


def test_distance_is_undetermined_without_a_target_taxonomy(alias_map):
    # No taxonomy supplied: the verdict is unaffected (it is species-level and needs
    # only the alias table), the distance simply cannot be measured.
    check = compute_crosscheck("VulpesVulpes", _taxo("Poa pratensis"), alias_map, None)
    assert check.flag == "disagree"
    assert check.taxonomic_distance == "undetermined"


# --------------------------------------------------------------------------- #
# not_evaluable — an excluded denominator, never a disagreement
# --------------------------------------------------------------------------- #
def test_missing_upstream_label_is_not_evaluable(alias_map, target_taxonomy):
    check = compute_crosscheck(None, _taxo("Vulpes vulpes"), alias_map, target_taxonomy)
    assert check.flag == "not_evaluable" and check.status == "not_evaluable"
    assert check.resolution_basis == "upstream_label_unusable"
    assert "no upstream label" in check.rationale.lower()


def test_bioclip_failure_is_not_evaluable_not_disagree(alias_map, target_taxonomy):
    check = compute_crosscheck("VulpesVulpes", _taxo("", ok=False, error="timeout"),
                               alias_map, target_taxonomy)
    assert check.flag == "not_evaluable"     # a failed run is not a taxonomic conflict
    assert check.resolution_basis == "bioclip_unavailable"
    assert "timeout" in check.rationale


def test_bioclip_empty_topk_is_not_evaluable(alias_map, target_taxonomy):
    check = compute_crosscheck("VulpesVulpes", _taxo(""), alias_map, target_taxonomy)
    assert check.flag == "not_evaluable"
    assert check.resolution_basis == "bioclip_unavailable"


def test_unmappable_upstream_token_is_not_evaluable(alias_map, target_taxonomy):
    check = compute_crosscheck("GallusGallus", _taxo("Vulpes vulpes"), alias_map, target_taxonomy)
    assert check.flag == "not_evaluable"     # NOT disagree — we never asked the question
    assert check.resolution_basis == "upstream_label_unusable"
    assert "not mappable" in check.rationale


def test_nonbinomial_class_is_not_evaluable_outside_taxonomy(alias_map, target_taxonomy):
    # Person is outside BioCLIP's Tree of Life; BioCLIP still returns a top-1, which
    # must NOT become a permanent false disagree (Decision 2).
    check = compute_crosscheck("Person", _taxo("Vulpes vulpes"), alias_map, target_taxonomy)
    assert check.flag == "not_evaluable"
    assert check.resolution_basis == "outside_bioclip_taxonomy"
    assert "outside BioCLIP" in check.rationale


# --------------------------------------------------------------------------- #
# Top-k join (opt-in, laxer) vs the default top-1 join. The fixture mirrors the
# real full-frame pigeon result: correct species present but buried at rank 4.
# It loosens WHERE a match may be found, never the rank at which one counts.
# --------------------------------------------------------------------------- #
def _pigeon_full_frame():
    return TaxonomicRead(
        provider="bioclip", model_name="test", ok=True,
        topk=[("Panthera leo", 0.39), ("Ursus arctos", 0.09),
              ("Meles meles", 0.05), ("Vulpes vulpes", 0.019), ("Canis lupus", 0.017)],
        topk_lineages=[_LINEAGES["Panthera leo"], None, _LINEAGES["Meles meles"],
                       _LINEAGES["Vulpes vulpes"], _LINEAGES["Canis lupus"]],
    )


def test_topk_off_by_default_is_disagree_when_top1_unmappable(alias_map, target_taxonomy):
    check = compute_crosscheck("VulpesVulpes", _pigeon_full_frame(), alias_map, target_taxonomy)
    assert check.flag == "disagree"
    assert check.resolution_basis == "unresolved_prediction"
    assert "Panthera leo" in check.rationale


def test_topk_join_rescues_deep_match_but_names_the_rank_and_score(alias_map, target_taxonomy):
    # With use_topk the rank-4 Vulpes vulpes (0.019) flips to 'agree' — but the
    # rationale states plainly it was rank 4 at 0.0190, and the basis records that
    # it was a top-k match, so it can never pass for a confident top-1 cross-check.
    check = compute_crosscheck("VulpesVulpes", _pigeon_full_frame(), alias_map,
                               target_taxonomy, use_topk=True)
    assert check.flag == "agree"
    assert check.resolution_basis == "topk_species_match"
    assert "rank 4" in check.rationale and "0.0190" in check.rationale
    assert "top-k join" in check.rationale


def test_topk_join_disagrees_when_top1_mappable_and_upstream_absent(alias_map, target_taxonomy):
    taxo = TaxonomicRead(provider="bioclip", model_name="test", ok=True,
                         topk=[("Meles meles", 0.8), ("Ursus arctos", 0.1)],
                         topk_lineages=[_LINEAGES["Meles meles"], None])
    check = compute_crosscheck("VulpesVulpes", taxo, alias_map, target_taxonomy, use_topk=True)
    assert check.flag == "disagree"
    assert "Meles meles" in check.rationale


def test_topk_join_disagrees_when_absent_and_top1_unmappable(alias_map, target_taxonomy):
    taxo = TaxonomicRead(provider="bioclip", model_name="test", ok=True,
                         topk=[("Panthera leo", 0.8), ("Gallus gallus", 0.1)],
                         topk_lineages=[_LINEAGES["Panthera leo"], None])
    check = compute_crosscheck("VulpesVulpes", taxo, alias_map, target_taxonomy, use_topk=True)
    assert check.flag == "disagree"


# --------------------------------------------------------------------------- #
# The narrow (flag, rationale) wrapper stays in step with the full result.
# --------------------------------------------------------------------------- #
def test_compute_agreement_wrapper_matches_compute_crosscheck(alias_map, target_taxonomy):
    for label, top1 in (("VulpesVulpes", "Vulpes vulpes"),
                        ("VulpesVulpes", "Poa pratensis"),
                        ("Person", "Vulpes vulpes")):
        check = compute_crosscheck(label, _taxo(top1), alias_map, target_taxonomy)
        flag, why = compute_agreement(label, _taxo(top1), alias_map, target_taxonomy)
        assert (flag, why) == (check.flag, check.rationale)
