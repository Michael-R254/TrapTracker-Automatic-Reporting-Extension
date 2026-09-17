"""The taxonomy backbone behind the cross-check's audit layer.

Two things are load-bearing and are pinned here:
  1. the shipped ``target_taxonomy.yaml`` covers exactly the alias table's species —
     a key that drifts out of step silently costs every distance for that species;
  2. ``taxonomic_distance`` reports the NEAREST shared rank, and treats a missing
     rank as unknown rather than as a match.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ttr.species.aliases import SpeciesAliasMap, is_binomial_key
from ttr.species.taxonomy import (DISTANCE_LABELS, DISTANCES, Lineage,
                                  TargetTaxonomy, load_target_taxonomy,
                                  taxonomic_distance)

# Resolved from THIS FILE's location, not the working directory. As a bare
# "config/species_aliases.yaml" this was CWD-relative, so the two tests below
# passed only when pytest happened to be invoked from the repository root.
#
# And the table is gitignored (it describes the operator's model, not this
# project), so on a fresh clone those tests did not
# skip: they raised FileNotFoundError. A missing non-redistributable file is an
# expected state, not a failure, so it is guarded the same way
# `tests/test_aliases.py` already guards it.
_ALIAS_PATH = Path(__file__).resolve().parents[1] / "config" / "species_aliases.yaml"

requires_deployment_table = pytest.mark.skipif(
    not _ALIAS_PATH.exists(),
    reason="local alias table not present (not redistributed)",
)


def _lin(**ranks) -> Lineage:
    return Lineage.from_mapping(ranks)


# --------------------------------------------------------------------------- #
# The shipped table
# --------------------------------------------------------------------------- #
def test_bundled_table_loads_as_package_data():
    taxonomy = TargetTaxonomy.bundled()
    assert len(taxonomy) > 0
    assert load_target_taxonomy(None).binomials() == taxonomy.binomials()


def test_every_shipped_entry_has_the_ranks_the_distance_function_uses():
    for binomial in (taxonomy := TargetTaxonomy.bundled()).binomials():
        lineage = taxonomy.lineage_for(binomial)
        assert lineage is not None
        for rank in ("kingdom", "class_name", "order", "family", "genus"):
            assert getattr(lineage, rank), f"{binomial} is missing {rank}"
        # The key must be reconstructible from the lineage, or the join to the
        # alias table's canonical key is not the join it claims to be.
        assert lineage.binomial and lineage.binomial.casefold() == binomial


def test_reserved_nonbio_keys_have_no_lineage():
    taxonomy = TargetTaxonomy({
        "Vulpes vulpes": {"kingdom": "Animalia", "class": "Mammalia",
                          "order": "Carnivora", "family": "Canidae"},
        "nonbio:Person": {"kingdom": "Animalia"},
    })
    assert taxonomy.lineage_for("Vulpes vulpes") is not None
    assert taxonomy.lineage_for("nonbio:Person") is None


@requires_deployment_table
def test_shipped_taxonomy_covers_exactly_the_alias_tables_species():
    """A key that drifts is invisible at runtime — the verdict still computes and
    only the distance goes quiet — so it is asserted rather than left to notice."""
    alias_entries = yaml.safe_load(_ALIAS_PATH.read_text(encoding="utf-8"))
    species = {k.casefold() for k in alias_entries if is_binomial_key(k)}
    shipped = set(TargetTaxonomy.bundled().binomials())
    assert shipped == species, (
        f"missing lineages: {sorted(species - shipped)}; "
        f"lineages with no alias entry: {sorted(shipped - species)}")


@requires_deployment_table
def test_alias_table_tokens_all_reach_a_lineage_or_are_declared_non_comparable():
    alias_entries = yaml.safe_load(_ALIAS_PATH.read_text(encoding="utf-8"))
    alias_map = SpeciesAliasMap(alias_entries)
    taxonomy = TargetTaxonomy.bundled()
    for key, body in alias_entries.items():
        for token in (body or {}).get("raw_tokens", []):
            if not alias_map.is_bioclip_comparable(token):
                continue        # not_evaluable by design; no lineage needed
            assert taxonomy.lineage_for(alias_map.binomial_for_token(token)) is not None, key


# --------------------------------------------------------------------------- #
# Distance
# --------------------------------------------------------------------------- #
_FOX = _lin(kingdom="Animalia", phylum="Chordata", **{"class": "Mammalia"},
            order="Carnivora", family="Canidae", genus="Vulpes", species_epithet="vulpes")


@pytest.mark.parametrize("predicted,expected", [
    (_FOX, "same_species"),
    (_lin(kingdom="Animalia", **{"class": "Mammalia"}, order="Carnivora",
          family="Canidae", genus="Vulpes", species_epithet="lagopus"), "same_genus"),
    (_lin(kingdom="Animalia", **{"class": "Mammalia"}, order="Carnivora",
          family="Canidae", genus="Canis", species_epithet="lupus"), "same_family"),
    (_lin(kingdom="Animalia", **{"class": "Mammalia"}, order="Carnivora",
          family="Felidae", genus="Felis", species_epithet="catus"), "same_order"),
    (_lin(kingdom="Animalia", **{"class": "Mammalia"}, order="Rodentia",
          family="Sciuridae", genus="Sciurus", species_epithet="vulgaris"), "same_class"),
    (_lin(kingdom="Animalia", **{"class": "Aves"}, order="Columbiformes",
          family="Columbidae", genus="Columba", species_epithet="palumbus"), "other_animal"),
    (_lin(kingdom="Plantae", **{"class": "Liliopsida"}, order="Poales",
          family="Poaceae", genus="Poa", species_epithet="pratensis"), "non_animal"),
    (_lin(kingdom="Fungi", **{"class": "Agaricomycetes"}, order="Agaricales",
          family="Amanitaceae", genus="Amanita", species_epithet="muscaria"), "non_animal"),
])
def test_distance_returns_the_nearest_shared_rank(predicted, expected):
    assert taxonomic_distance(_FOX, predicted) == expected


def test_distance_is_symmetric():
    other = _lin(kingdom="Animalia", **{"class": "Mammalia"}, order="Carnivora",
                 family="Canidae", genus="Canis", species_epithet="lupus")
    assert taxonomic_distance(_FOX, other) == taxonomic_distance(other, _FOX)


def test_missing_lineages_are_undetermined_not_a_match():
    assert taxonomic_distance(_FOX, None) == "undetermined"
    assert taxonomic_distance(None, _FOX) == "undetermined"
    assert taxonomic_distance(_FOX, Lineage()) == "undetermined"


def test_two_absent_ranks_do_not_count_as_agreement():
    """A blank rank is unknown, not 'the same unknown'. Two lineages that share
    only their gaps are a cross-kingdom homonym, and must not read as related."""
    bare_a = _lin(genus="Ficus", species_epithet="pumila")
    bare_b = _lin(genus="Zzz", species_epithet="qqq")
    assert taxonomic_distance(bare_a, bare_b) == "undetermined"


def test_partial_lineage_still_matches_at_the_ranks_it_knows():
    # A merged entry (BioCLIP's table disagrees about the order) keeps class, so a
    # class-level distance is still measurable from what IS known.
    partial = _lin(kingdom="Animalia", **{"class": "Mammalia"},
                   family="Ursidae", genus="Ursus", species_epithet="arctos")
    assert taxonomic_distance(_FOX, partial) == "same_class"


def test_every_distance_has_a_render_phrase():
    assert set(DISTANCE_LABELS) == set(DISTANCES)
    assert all(DISTANCE_LABELS[d] for d in DISTANCES)


def test_lineage_mapping_round_trips():
    assert Lineage.from_mapping(_FOX.as_mapping()) == _FOX
