"""Stage 5 verification: the real parse -> resolve -> persist -> retrieve path.

Exercises the pipeline against EmailSource backed by a fixture fetcher (no live
IMAP) and a FakeEnricher (no models), then retrieves by common name — proving
canonical_binomial is actually populated in production, not just in seeded data.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ttr.agents.retrieval import RetrievalAgent
from ttr.agents.window import TimeWindow
from ttr.enrichment.base import DescriptionRead, TaxonomicRead
from ttr.pipeline import Pipeline
from ttr.sources.email_source import EmailSource
from ttr.sources.seen_store import SeenStore

from conftest import FakeDescriptionEnricher, FakeTaxonomicEnricher, load_eml

UTC = timezone.utc


class FakeFetcher:
    def __init__(self, names):
        self._names = names

    def fetch_unseen(self):
        for name in self._names:
            msg = load_eml(name)
            yield (msg["Message-ID"] or "").strip(), msg


def _wide_window():
    return TimeWindow(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC))


def _pipeline(repo, alias_map, tmp_path, names, *, taxo=None, desc=None, taxonomy=None):
    source = EmailSource(FakeFetcher(names), SeenStore(tmp_path / "seen.txt"))
    taxo = taxo or FakeTaxonomicEnricher(TaxonomicRead(
        provider="bioclip", model_name="fake-bioclip",
        topk=[("Vulpes vulpes", 0.91)], embedding=[0.1, 0.2, 0.3], ok=True))
    desc = desc or FakeDescriptionEnricher(DescriptionRead(
        provider="ollama", model_name="fake-vlm", text="A red fox in a field.", ok=True))
    return Pipeline(source=source, taxonomic=taxo, description=desc, repo=repo,
                    alias_map=alias_map, target_taxonomy=taxonomy,
                    image_store_dir=tmp_path / "images")


def test_end_to_end_ingest_resolve_persist_retrieve(repo, alias_map, tmp_path):
    # Stable reconstructed fixture (MelesMeles -> badger), not the swappable golden.
    taxo = FakeTaxonomicEnricher(TaxonomicRead(
        provider="bioclip", model_name="fake-bioclip",
        topk=[("Meles meles", 0.91)], embedding=[0.1, 0.2, 0.3], ok=True))
    desc = FakeDescriptionEnricher(DescriptionRead(
        provider="ollama", model_name="fake-vlm", text="A badger at the sett.", ok=True))
    pipe = _pipeline(repo, alias_map, tmp_path, ["both_attachments.eml"], taxo=taxo, desc=desc)
    assert pipe.run_once() == 1

    # The whole point: retrieve by COMMON NAME against pipeline-produced data.
    results = RetrievalAgent(repo, alias_map).find("badger", _wide_window())
    assert len(results) == 1
    r = results[0]
    assert r.canonical_binomial == "Meles meles"       # RESOLVED in production, not NULL
    assert r.upstream_label == "MelesMeles"             # authoritative token intact
    assert r.bioclip_ok and r.agreement_flag == "agree"
    # The audit trail is persisted alongside the verdict, not derived at read time.
    assert r.cross_check_status == "evaluable"
    assert r.resolution_basis == "exact_species_match"
    assert r.matched_rank == "species"
    assert r.taxonomic_distance == "same_species"
    assert r.vlm_description == "A badger at the sett."
    assert r.original_image_path and r.boxed_image_path # blobs written to our store
    assert repo.get_embedding(r.id) == [0.1, 0.2, 0.3]  # embedding round-tripped


def test_pipeline_is_idempotent(repo, alias_map, tmp_path):
    names = ["both_attachments.eml"]
    _pipeline(repo, alias_map, tmp_path, names).run_once()
    # Second pass over the same seen-store yields nothing new.
    again = _pipeline(repo, alias_map, tmp_path, names).run_once()
    assert again == 0


def test_pipeline_persists_the_prediction_lineage_and_measures_the_distance(
        repo, alias_map, tmp_path, target_taxonomy):
    """A disagreement is stored WITH the ranks it was measured from, so the audit
    breakdown in the report can be checked against the evidence rather than taken
    on trust."""
    taxo = FakeTaxonomicEnricher(TaxonomicRead(
        provider="bioclip", model_name="fake-bioclip", ok=True,
        topk=[("Poa pratensis", 0.44)],
        topk_lineages=[{"kingdom": "Plantae", "phylum": "Tracheophyta",
                        "class": "Liliopsida", "order": "Poales", "family": "Poaceae",
                        "genus": "Poa", "species_epithet": "pratensis"}]))
    pipe = _pipeline(repo, alias_map, tmp_path, ["both_attachments.eml"],
                     taxo=taxo, taxonomy=target_taxonomy)
    assert pipe.run_once() == 1

    r = RetrievalAgent(repo, alias_map).find("badger", _wide_window())[0]
    assert r.agreement_flag == "disagree"          # a plant is not the upstream badger
    assert r.cross_check_status == "evaluable"
    assert r.resolution_basis == "unresolved_prediction"
    assert r.taxonomic_distance == "non_animal"
    assert r.bioclip_top1_kingdom == "Plantae"
    assert r.bioclip_top1_family == "Poaceae"
    assert r.bioclip_top1_genus == "Poa"


def test_pipeline_unmapped_token_stays_queryable(repo, alias_map, tmp_path):
    # zero_attachments.eml => ErinaceusEuropaeus, absent from the FIXTURE alias map.
    pipe = _pipeline(repo, alias_map, tmp_path, ["zero_attachments.eml"])
    assert pipe.run_once() == 1

    agent = RetrievalAgent(repo, alias_map)
    assert agent.find("hedgehog", _wide_window()) == []          # unresolved term
    unmapped = agent.find_unmapped(_wide_window())
    assert len(unmapped) == 1
    assert unmapped[0].canonical_binomial == "unmapped:ErinaceusEuropaeus"
    assert unmapped[0].canonical_is_binomial is False


def test_one_event_failure_does_not_halt_loop(repo, alias_map, tmp_path):
    class FlakyTaxo:
        model_name = "flaky"
        def __init__(self):
            self.n = 0
        def classify(self, original_image):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("unexpected enricher crash")   # first event explodes
            return TaxonomicRead(provider="bioclip", model_name="flaky",
                                 topk=[("Meles meles", 0.9)], ok=True)

    # First email crashes enrichment; the loop must still store the second.
    # Stable reconstructed fixtures (not the swappable golden).
    pipe = _pipeline(repo, alias_map, tmp_path,
                     ["missing_boxed.eml", "both_attachments.eml"],
                     taxo=FlakyTaxo())
    stored = pipe.run_once()
    assert stored == 1                                            # one survived, loop didn't halt
    assert RetrievalAgent(repo, alias_map).find("badger", _wide_window())  # MelesMeles stored


# ------------------------------------------------------ progress observer (§4)
def test_progress_callback_fires_once_per_stored_event(repo, alias_map, tmp_path):
    """The ingest page's live strip is fed from here, so the payload must carry
    what the strip shows: the upstream token, the BioCLIP top-1 and the flag."""
    seen = []
    pipe = _pipeline(repo, alias_map, tmp_path, ["both_attachments.eml"])
    pipe._on_event = seen.append
    assert pipe.run_once() == 1

    assert len(seen) == 1
    payload = seen[0]
    assert payload["ok"] is True
    assert payload["upstream_label"] == "MelesMeles"
    assert payload["bioclip_top1"] == "Vulpes vulpes"
    assert payload["agreement"] in ("agree", "disagree", "not_evaluable")
    assert payload["event_id"] is not None
    # Plain scalars/lists only — no domain object escapes to the observer.
    assert all(not hasattr(v, "__dict__") for v in payload.values())


def test_failing_progress_callback_never_breaks_the_run(repo, alias_map, tmp_path):
    """Degrade gracefully, never drop (§8): a display sink is not allowed to cost
    a stored detection."""
    def hostile(_payload):
        raise ValueError("observer exploded")

    pipe = _pipeline(repo, alias_map, tmp_path, ["both_attachments.eml"])
    pipe._on_event = hostile
    assert pipe.run_once() == 1                                   # stored anyway
    assert RetrievalAgent(repo, alias_map).find("badger", _wide_window())


def test_progress_callback_reports_a_failed_event(repo, alias_map, tmp_path):
    """A skipped event must be VISIBLE to the observer, not a silent gap in the
    strip."""
    class FlakyTaxo:
        model_name = "flaky"
        def classify(self, original_image):
            raise RuntimeError("unexpected enricher crash")

    seen = []
    pipe = _pipeline(repo, alias_map, tmp_path, ["both_attachments.eml"], taxo=FlakyTaxo())
    pipe._on_event = seen.append
    assert pipe.run_once() == 0

    assert len(seen) == 1
    assert seen[0]["ok"] is False
    assert "unexpected enricher crash" in seen[0]["error"]


# --------------------------------------------------------------------------- #
# Durability: an event is retired only once it is stored.
#
# The source used to mark a Message-ID seen the moment it yielded the event, so a
# failure anywhere downstream — a full image store, a locked database — left the
# message marked and it never came round again. That is a silent drop, which
# "degrade gracefully, never drop" forbids. These pin the contract.
# --------------------------------------------------------------------------- #
class ExplodingRepo:
    """A repository whose writes fail, standing in for a full disk / locked DB."""

    def __init__(self, real, *, fail_times=1):
        self._real = real
        self._remaining = fail_times
        self.attempts = 0

    def upsert_event(self, ev):
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise OSError("disk full")
        return self._real.upsert_event(ev)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_storage_failure_does_not_retire_the_message(repo, alias_map, tmp_path):
    """The failure case: the run survives, and the event stays eligible."""
    seen_path = tmp_path / "seen.txt"
    source = EmailSource(FakeFetcher(["both_attachments.eml"]), SeenStore(seen_path))
    failing = ExplodingRepo(repo, fail_times=99)
    pipe = Pipeline(
        source=source,
        taxonomic=FakeTaxonomicEnricher(TaxonomicRead(
            provider="bioclip", model_name="fake", topk=[("Meles meles", 0.9)], ok=True)),
        description=FakeDescriptionEnricher(DescriptionRead(
            provider="ollama", model_name="fake", text="A badger.", ok=True)),
        repo=failing, alias_map=alias_map, image_store_dir=tmp_path / "images")

    assert pipe.run_once() == 0                      # nothing stored
    assert failing.attempts == 1                     # but it was attempted
    # Nothing retired: the seen-store must not have gained the id.
    assert not seen_path.exists() or seen_path.read_text(encoding="utf-8").strip() == ""


def test_event_is_redelivered_and_stored_after_the_failure_clears(
        repo, alias_map, tmp_path):
    """The recovery the fix exists for: next poll picks the same message up and
    stores it. Previously this returned 0 forever — the record was gone."""
    seen_path = tmp_path / "seen.txt"
    fetcher = FakeFetcher(["both_attachments.eml"])
    source = EmailSource(fetcher, SeenStore(seen_path))
    failing = ExplodingRepo(repo, fail_times=1)      # fails once, then recovers
    pipe = Pipeline(
        source=source,
        taxonomic=FakeTaxonomicEnricher(TaxonomicRead(
            provider="bioclip", model_name="fake", topk=[("Meles meles", 0.9)], ok=True)),
        description=FakeDescriptionEnricher(DescriptionRead(
            provider="ollama", model_name="fake", text="A badger.", ok=True)),
        repo=failing, alias_map=alias_map, image_store_dir=tmp_path / "images")

    assert pipe.run_once() == 0                      # first poll: the disk is full
    assert pipe.run_once() == 1                      # second poll: it comes back
    assert failing.attempts == 2

    results = RetrievalAgent(repo, alias_map).find("badger", _wide_window())
    assert len(results) == 1


def test_stored_event_is_retired_and_not_reprocessed(repo, alias_map, tmp_path):
    """The success case still holds: a stored event is never enriched twice."""
    seen_path = tmp_path / "seen.txt"
    taxo = FakeTaxonomicEnricher(TaxonomicRead(
        provider="bioclip", model_name="fake", topk=[("Meles meles", 0.9)], ok=True))
    source = EmailSource(FakeFetcher(["both_attachments.eml"]), SeenStore(seen_path))
    pipe = Pipeline(
        source=source, taxonomic=taxo,
        description=FakeDescriptionEnricher(DescriptionRead(
            provider="ollama", model_name="fake", text="A badger.", ok=True)),
        repo=repo, alias_map=alias_map, image_store_dir=tmp_path / "images")

    assert pipe.run_once() == 1
    assert taxo.calls == 1
    assert pipe.run_once() == 0                      # retired, so not yielded again
    assert taxo.calls == 1                           # and not re-enriched
    assert seen_path.read_text(encoding="utf-8").strip() != ""
