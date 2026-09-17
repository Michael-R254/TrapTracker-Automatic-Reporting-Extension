"""Stage 4 verification: RetrievalAgent resolves common name -> binomial before
querying, filters by window, and never returns a silent empty."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ttr.agents.retrieval import RetrievalAgent
from ttr.agents.window import TimeWindow
from ttr.species.aliases import AmbiguousSpecies

from conftest import make_persisted_event

UTC = timezone.utc


def _window():
    return TimeWindow(start_utc=datetime(2026, 7, 1, tzinfo=UTC),
                      end_utc=datetime(2026, 7, 31, tzinfo=UTC))


def _seed(repo):
    repo.upsert_event(make_persisted_event(
        "<fox1@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 10, 2, 0, tzinfo=UTC)))
    repo.upsert_event(make_persisted_event(
        "<fox2@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 20, 2, 0, tzinfo=UTC)))
    repo.upsert_event(make_persisted_event(
        "<badger@x>", canonical_binomial="Meles meles",
        event_time=datetime(2026, 7, 15, 2, 0, tzinfo=UTC)))
    repo.upsert_event(make_persisted_event(
        "<person@x>", canonical_binomial="nonbio:Person",
        event_time=datetime(2026, 7, 12, 2, 0, tzinfo=UTC)))


def test_common_name_resolves_to_binomial_and_queries(repo, alias_map):
    _seed(repo)
    agent = RetrievalAgent(repo, alias_map)
    results = agent.find("fox", _window())      # common name -> Vulpes vulpes
    assert {r.source_message_id for r in results} == {"<fox1@x>", "<fox2@x>"}


def test_binomial_query_works_too(repo, alias_map):
    _seed(repo)
    agent = RetrievalAgent(repo, alias_map)
    assert len(agent.find("Vulpes vulpes", _window())) == 2


def test_window_filters_out_of_range(repo, alias_map):
    _seed(repo)
    agent = RetrievalAgent(repo, alias_map)
    narrow = TimeWindow(start_utc=datetime(2026, 7, 18, tzinfo=UTC),
                        end_utc=datetime(2026, 7, 25, tzinfo=UTC))
    results = agent.find("fox", narrow)
    assert {r.source_message_id for r in results} == {"<fox2@x>"}


def test_nonbinomial_class_is_queryable(repo, alias_map):
    _seed(repo)
    agent = RetrievalAgent(repo, alias_map)
    results = agent.find("person", _window())   # nonbio:Person, not NULL, not lost
    assert {r.source_message_id for r in results} == {"<person@x>"}


def test_unresolved_term_returns_empty_not_error(repo, alias_map):
    _seed(repo)
    agent = RetrievalAgent(repo, alias_map)
    assert agent.find("velociraptor", _window()) == []


def test_ambiguous_term_raises_did_you_mean(repo, alias_map):
    _seed(repo)
    agent = RetrievalAgent(repo, alias_map)
    with pytest.raises(AmbiguousSpecies):
        agent.find("deer", _window())


def test_resolve_identity_returns_key_and_display(repo, alias_map):
    agent = RetrievalAgent(repo, alias_map)
    key, display = agent.resolve_identity("fox")
    assert key == "Vulpes vulpes"
    assert display == "red fox"


def test_find_unmapped_includes_undated_rows(repo, alias_map):
    repo.upsert_event(make_persisted_event(
        "<u-dated@x>", canonical_binomial="unmapped:GallusGallus", canonical_is_binomial=False,
        upstream_label="GallusGallus", event_time=datetime(2026, 7, 10, tzinfo=UTC)))
    repo.upsert_event(make_persisted_event(
        "<u-undated@x>", canonical_binomial="unmapped:(no upstream label)",
        canonical_is_binomial=False, upstream_label=None, event_time=None))
    got = RetrievalAgent(repo, alias_map).find_unmapped(_window())
    # The undated unmapped row is NOT lost to the time filter.
    assert {r.source_message_id for r in got} == {"<u-dated@x>", "<u-undated@x>"}


def test_count_undated_for_species(repo, alias_map):
    repo.upsert_event(make_persisted_event(
        "<dated@x>", canonical_binomial="Vulpes vulpes", event_time=datetime(2026, 7, 10, tzinfo=UTC)))
    repo.upsert_event(make_persisted_event(
        "<undated@x>", canonical_binomial="Vulpes vulpes", event_time=None))
    assert RetrievalAgent(repo, alias_map).count_undated("Vulpes vulpes") == 1
