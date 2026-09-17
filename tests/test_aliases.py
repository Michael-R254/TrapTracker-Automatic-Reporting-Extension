"""SpeciesAliasMap: reserved non-binomial keys, many-to-one tokens, comparability,
and did-you-mean ambiguity — plus validation of the real 31-class example table."""

from __future__ import annotations

from pathlib import Path

import pytest

from ttr.species.aliases import AmbiguousSpecies, SpeciesAliasMap, is_binomial_key

from conftest import EXAMPLE_ALIAS_PATH as EXAMPLE_YAML
CLASS_LIST = Path(__file__).resolve().parents[1] / "config" / "traptracker_uk_mammals_yolo26x_v1.0.0.txt"


# --------------------------------------------------------------------------- #
# Core behaviours on the small fixture map.
# --------------------------------------------------------------------------- #
def test_token_and_common_resolution(alias_map):
    assert alias_map.canonical_key_for_token("VulpesVulpes") == "Vulpes vulpes"
    assert alias_map.binomial_for_token("VulpesVulpes") == "Vulpes vulpes"
    assert alias_map.resolve("fox") == "Vulpes vulpes"
    assert alias_map.resolve("VULPES VULPES") == "Vulpes vulpes"     # binomial, case-insensitive
    assert alias_map.display_common_name("Vulpes vulpes") == "red fox"


def test_many_to_one_tokens_share_a_binomial(alias_map):
    assert alias_map.canonical_key_for_token("NumeniusArquata") == "Numenius arquata"
    assert alias_map.canonical_key_for_token("NumeniusArquataChick") == "Numenius arquata"
    assert sorted(alias_map.tokens_for_binomial("Numenius arquata")) == [
        "NumeniusArquata", "NumeniusArquataChick",
    ]


def test_reserved_nonbinomial_key(alias_map):
    key = alias_map.canonical_key_for_token("Person")
    assert key == "nonbio:Person"
    assert is_binomial_key(key) is False
    assert alias_map.binomial_for_token("Person") is None          # no binomial
    assert alias_map.is_bioclip_comparable("Person") is False
    assert alias_map.resolve("person") == "nonbio:Person"          # still queryable


def test_domestic_is_comparable(alias_map):
    assert alias_map.is_bioclip_comparable("BosTaurus") is True


def test_ambiguous_common_name_raises_did_you_mean(alias_map):
    with pytest.raises(AmbiguousSpecies) as exc:
        alias_map.resolve("deer")
    keys = {k for k, _ in exc.value.candidates}
    assert keys == {"Capreolus capreolus", "Dama dama"}
    assert "did you mean" in str(exc.value).lower()


def test_specific_common_name_is_unambiguous(alias_map):
    assert alias_map.resolve("roe deer") == "Capreolus capreolus"


def test_unknown_term_returns_none(alias_map):
    assert alias_map.resolve("velociraptor") is None
    assert alias_map.canonical_key_for_token("NoSuchToken") is None


# --------------------------------------------------------------------------- #
# entries() — enumeration for the UI dropdown (Prereq B).
# --------------------------------------------------------------------------- #
def test_entries_enumerates_all_with_resolvable_query_terms(alias_map):
    entries = alias_map.entries()
    keys = {e.canonical_key for e in entries}
    assert "Vulpes vulpes" in keys and "nonbio:Person" in keys
    # Each query_term round-trips through resolve() to its canonical key, so the
    # dropdown value the UI submits always resolves — species and nonbio alike.
    for e in entries:
        assert alias_map.resolve(e.query_term) == e.canonical_key
    # A nonbio class has no binomial, so it queries by its common name.
    person = next(e for e in entries if e.canonical_key == "nonbio:Person")
    assert person.is_binomial is False and person.query_term == "person"
    # Sorted by display name for the dropdown.
    names = [e.display_name for e in entries]
    assert names == sorted(names, key=str.casefold)


def test_entries_on_bundled_table_covers_all_classes_incl_nonbio():
    amap = SpeciesAliasMap.from_yaml(EXAMPLE_YAML)
    entries = amap.entries()
    nonbio = {e.canonical_key for e in entries if not e.is_binomial}
    assert nonbio == {"nonbio:Person", "nonbio:Car", "nonbio:CalibrationPole"}
    for e in entries:
        assert amap.resolve(e.query_term) == e.canonical_key


# --------------------------------------------------------------------------- #
# The BUNDLED example table — shipped as package data, illustrative by design.
#
# It exercises every structural case the code branches on. It is deliberately not
# a copy of any deployment's class list: that list's redistribution terms are
# unconfirmed, so it stays local and is covered by the
# deployment-conformance tests further down, which skip when it is absent.
# --------------------------------------------------------------------------- #
def test_bundled_table_nonbinomial_handling():
    amap = SpeciesAliasMap.from_yaml(EXAMPLE_YAML)

    # The three non-binomial classes are reserved keys, non-comparable, queryable.
    for token, term in (("Person", "person"), ("Car", "car"), ("CalibrationPole", "calibration pole")):
        key = amap.canonical_key_for_token(token)
        assert key and not is_binomial_key(key)
        assert amap.is_bioclip_comparable(token) is False
        assert amap.resolve(term) == key


def test_bundled_table_exercises_every_structural_case():
    """The point of shipping this file: each branch in SpeciesAliasMap has a
    worked example, so the fallback is a real demonstration and not a stub."""
    amap = SpeciesAliasMap.from_yaml(EXAMPLE_YAML)

    # many-to-one: a life-stage variant is the same species
    assert amap.canonical_key_for_token("CygnusOlorCygnet") == "Cygnus olor"
    assert len(amap.tokens_for_binomial("Cygnus olor")) == 2

    # a domestic animal is still inside BioCLIP's taxonomy, so still compared
    assert amap.is_bioclip_comparable("GallusGallus") is True

    # two independent ambiguities, so did-you-mean is covered for more than one shape
    for term in ("deer", "pipistrelle"):
        with pytest.raises(AmbiguousSpecies) as exc:
            amap.resolve(term)
        assert len(exc.value.candidates) == 2, term

    # and the specific names are unambiguous
    assert amap.resolve("roe deer") == "Capreolus capreolus"
    assert amap.resolve("common pipistrelle") == "Pipistrellus pipistrellus"


def test_bundled_table_is_not_a_copy_of_a_deployment_class_list():
    """Provenance guard. The shipped table may overlap a
    deployment's classes only where a bundled fixture or a reserved-key branch
    actually needs the token — never as a wholesale reproduction of one."""
    amap = SpeciesAliasMap.from_yaml(EXAMPLE_YAML)
    tokens = {t for e in amap.entries() for t in amap.tokens_for_binomial(e.canonical_key)}

    load_bearing = {
        # the five tokens the bundled .eml fixtures actually carry
        "VulpesVulpes", "MelesMeles", "CapreolusCapreolus", "ColumbaPalumbus",
        "ErinaceusEuropaeus",
        # the three reserved non-binomial classes the code branches on
        "Person", "Car", "CalibrationPole",
    }
    if not CLASS_LIST.exists():
        pytest.skip("deployment class list not present — nothing to compare against")

    upstream = {t.strip() for t in CLASS_LIST.read_text(encoding="utf-8").splitlines() if t.strip()}
    overlap = tokens & upstream
    assert overlap <= load_bearing, (
        f"shipped table reproduces deployment classes it does not need: "
        f"{sorted(overlap - load_bearing)}")
    # And it is a genuinely different table, not a thinned copy.
    assert len(overlap) < len(upstream) / 2
    assert tokens - upstream, "shipped table contains nothing of its own"


# --------------------------------------------------------------------------- #
# Deployment conformance: the LOCAL table matches the operator's class list.
#
# Skipped unless both files are present. Neither ships: the class list came from
# the TrapTracker RT deployment and its redistribution terms are unconfirmed, and
# the table derived from it is therefore local too. These still run for the
# maintainer, which is where the check has value.
# --------------------------------------------------------------------------- #
LOCAL_TABLE = Path(__file__).resolve().parents[1] / "config" / "species_aliases.yaml"

requires_deployment_table = pytest.mark.skipif(
    not (CLASS_LIST.exists() and LOCAL_TABLE.exists()),
    reason="deployment class list / local alias table not present (not redistributed)",
)


@requires_deployment_table
def test_local_table_covers_every_deployment_class_token():
    tokens = [t.strip() for t in CLASS_LIST.read_text(encoding="utf-8").splitlines() if t.strip()]
    assert len(tokens) == 31

    amap = SpeciesAliasMap.from_yaml(LOCAL_TABLE)
    for token in tokens:
        assert amap.canonical_key_for_token(token) is not None, f"{token} missing from alias table"


@requires_deployment_table
def test_local_table_keeps_the_upstream_misspelling_out_of_common_names():
    amap = SpeciesAliasMap.from_yaml(LOCAL_TABLE)

    # Upstream misspelling kept in tokens (the tokens ARE the class names);
    # correct spelling in the common name.
    assert amap.canonical_key_for_token("CappercaillieCock") == "Tetrao urogallus"
    assert amap.canonical_key_for_token("CappercaillieHen") == "Tetrao urogallus"
    assert amap.display_common_name("Tetrao urogallus") == "western capercaillie"


@requires_deployment_table
def test_local_table_deer_is_ambiguous_across_all_four_species():
    amap = SpeciesAliasMap.from_yaml(LOCAL_TABLE)
    with pytest.raises(AmbiguousSpecies) as exc:
        amap.resolve("deer")
    assert len(exc.value.candidates) == 4
