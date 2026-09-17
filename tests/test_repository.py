"""Stage 2 verification: DetectionRepository round-trips events, enforces
Message-ID uniqueness, queries by binomial+window via the indexes, isolates
embedding storage, and supports a concurrent read during a write (WAL)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from ttr.sources.email_parser import parse_alert_email
from ttr.storage.mapping import persisted_from_detection_event
from ttr.storage.repository import DetectionRepository, PersistedEvent

from conftest import load_eml

UTC = timezone.utc


def _event(message_id: str, *, binomial: str, when: datetime, label: str = "VulpesVulpes",
           confidence: float = 0.87) -> PersistedEvent:
    return PersistedEvent(
        source_type="email",
        source_message_id=message_id,
        ingested_at_utc=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        upstream_label=label,
        canonical_binomial=binomial,
        display_common_name="red fox" if binomial == "Vulpes vulpes" else None,
        upstream_confidence=confidence,
        upstream_image_id=4231,
        upstream_image_id_resolvable=False,
        event_time_utc=when,
        images_present="both",
        field_provenance={"upstream_label": {"present": True, "source": "email_body"}},
        parse_warnings=["role inferred"],
    )


# (the `repo` fixture is shared from conftest.py)


# --------------------------------------------------------------------------- #
# Round-trip.
# --------------------------------------------------------------------------- #
def test_upsert_and_query_round_trip(repo):
    when = datetime(2026, 7, 14, 2, 13, 47, tzinfo=UTC)
    event_id = repo.upsert_event(_event("<a@x>", binomial="Vulpes vulpes", when=when))
    assert event_id > 0

    results = repo.query(binomial="Vulpes vulpes",
                         start_utc=when - timedelta(days=1),
                         end_utc=when + timedelta(days=1))
    assert len(results) == 1
    got = results[0]
    assert got.source_message_id == "<a@x>"
    assert got.canonical_binomial == "Vulpes vulpes"
    assert got.display_common_name == "red fox"
    assert got.upstream_confidence == 0.87
    assert got.event_time_utc == when                      # tz-aware round-trip
    assert got.upstream_image_id_resolvable is False       # int->bool restored
    assert got.field_provenance["upstream_label"]["present"] is True
    assert got.parse_warnings == ["role inferred"]


# --------------------------------------------------------------------------- #
# Idempotency: UNIQUE(source_message_id).
# --------------------------------------------------------------------------- #
def test_upsert_is_idempotent_on_message_id(repo):
    when = datetime(2026, 7, 14, 2, 13, 47, tzinfo=UTC)
    id1 = repo.upsert_event(_event("<dup@x>", binomial="Vulpes vulpes", when=when))
    id2 = repo.upsert_event(_event("<dup@x>", binomial="Vulpes vulpes", when=when, confidence=0.99))
    assert id1 == id2
    assert repo.exists_message_id("<dup@x>") is True

    results = repo.query(binomial="Vulpes vulpes",
                         start_utc=when - timedelta(days=1),
                         end_utc=when + timedelta(days=1))
    assert len(results) == 1                                # second upsert was a no-op
    assert results[0].upstream_confidence == 0.87          # original row unchanged


# --------------------------------------------------------------------------- #
# Windowed binomial query, index-backed.
# --------------------------------------------------------------------------- #
def test_query_filters_by_binomial_and_window(repo):
    base = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
    repo.upsert_event(_event("<in1@x>", binomial="Vulpes vulpes", when=base))
    repo.upsert_event(_event("<in2@x>", binomial="Vulpes vulpes", when=base + timedelta(days=2)))
    repo.upsert_event(_event("<out-late@x>", binomial="Vulpes vulpes", when=base + timedelta(days=30)))
    repo.upsert_event(_event("<other-species@x>", binomial="Meles meles", when=base, label="MelesMeles"))

    results = repo.query(binomial="Vulpes vulpes",
                         start_utc=base - timedelta(hours=1),
                         end_utc=base + timedelta(days=7))
    ids = {r.source_message_id for r in results}
    assert ids == {"<in1@x>", "<in2@x>"}                    # window + species both applied
    assert [r.event_time_utc for r in results] == sorted(r.event_time_utc for r in results)


def test_binomial_query_uses_an_index(repo):
    plan = repo._conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM detection_events "
        "WHERE canonical_binomial = ? AND event_time_utc >= ? AND event_time_utc <= ?",
        ("Vulpes vulpes", "2026-01-01T00:00:00+00:00", "2026-12-31T00:00:00+00:00"),
    ).fetchall()
    detail = " ".join(row["detail"] for row in plan)
    assert "USING INDEX" in detail.upper()
    assert "idx_de_binomial" in detail                     # not a full table scan


# --------------------------------------------------------------------------- #
# Embedding isolation (Fix b): caller only ever sees list[float].
# --------------------------------------------------------------------------- #
def test_embedding_round_trip_without_exposing_storage_shape(repo):
    when = datetime(2026, 7, 14, 2, 13, 47, tzinfo=UTC)
    event_id = repo.upsert_event(_event("<emb@x>", binomial="Vulpes vulpes", when=when))

    assert repo.get_embedding(event_id) is None            # none stored yet
    embedding = [0.1, -0.2, 0.3, 0.4]
    repo.store_embedding(event_id, embedding)
    assert repo.get_embedding(event_id) == embedding       # list in, list out


# --------------------------------------------------------------------------- #
# Seeded from Stage 1 fixtures (the parse->persist path).
# --------------------------------------------------------------------------- #
def test_persist_from_fixture_events(repo):
    # Stable reconstructed fixture (MelesMeles), not the swappable golden.
    ev = parse_alert_email(load_eml("both_attachments.eml"))
    # Alias resolution is Stage 4; inject the binomial here as the wiring will.
    pe = persisted_from_detection_event(
        ev, canonical_binomial="Meles meles", display_common_name="european badger"
    )
    assert pe.images_present == "both"
    assert pe.field_provenance["upstream_image_id"]["resolvable"] is False
    assert pe.upstream_image_id_resolvable is False           # never resolvable via email

    event_id = repo.upsert_event(pe)
    got = repo.query(binomial="Meles meles",
                     start_utc=datetime(2026, 7, 1, tzinfo=UTC),
                     end_utc=datetime(2026, 7, 31, tzinfo=UTC))
    assert len(got) == 1
    assert got[0].id == event_id
    assert got[0].upstream_label == "MelesMeles"            # authoritative token intact
    # The Decision-7 provenance note survived persistence.
    assert "ingestion_validation" in got[0].field_provenance


def test_absent_image_id_is_not_marked_resolvable(repo):
    ev = parse_alert_email(load_eml("malformed_body.eml"))    # no ImageId, no Time
    pe = persisted_from_detection_event(ev, canonical_binomial="Vulpes vulpes")
    assert pe.upstream_image_id is None
    assert pe.upstream_image_id_resolvable is False           # not vacuously True

    # It still persists (never dropped), but with a NULL event_time it is
    # correctly excluded from windowed queries rather than misdated.
    repo.upsert_event(pe)
    assert repo.exists_message_id(ev.source_message_id) is True
    windowed = repo.query(binomial="Vulpes vulpes",
                          start_utc=datetime(2026, 7, 1, tzinfo=UTC),
                          end_utc=datetime(2026, 7, 31, tzinfo=UTC))
    assert windowed == []                                     # NULL time -> not window-matched


# --------------------------------------------------------------------------- #
# WAL: concurrent read during an open write transaction.
# --------------------------------------------------------------------------- #
def test_species_counts(repo):
    when = datetime(2026, 7, 10, tzinfo=UTC)
    repo.upsert_event(_event("<a@x>", binomial="Vulpes vulpes", when=when))
    repo.upsert_event(_event("<b@x>", binomial="Vulpes vulpes", when=when))
    repo.upsert_event(_event("<c@x>", binomial="Meles meles", when=when))
    assert repo.species_counts() == {"Vulpes vulpes": 2, "Meles meles": 1}


def test_wal_enabled(repo):
    mode = repo._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_concurrent_read_during_write(tmp_path):
    db = tmp_path / "wal.db"
    writer = DetectionRepository(db)
    when = datetime(2026, 7, 14, 2, 13, 47, tzinfo=UTC)
    writer.upsert_event(_event("<committed@x>", binomial="Vulpes vulpes", when=when))

    # A second connection begins a write and holds it open (uncommitted).
    other = sqlite3.connect(str(db))
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute(
            "INSERT INTO detection_events "
            "(source_type, source_message_id, ingested_at_utc, images_present, "
            " provenance_json, created_at_utc) "
            "VALUES ('email', '<pending@x>', ?, 'none', '{}', ?)",
            (when.isoformat(), when.isoformat()),
        )
        # While that write is open, a reader still sees committed data (no block).
        reader = DetectionRepository(db)
        try:
            results = reader.query(binomial="Vulpes vulpes",
                                   start_utc=when - timedelta(days=1),
                                   end_utc=when + timedelta(days=1))
            assert {r.source_message_id for r in results} == {"<committed@x>"}
        finally:
            reader.close()
    finally:
        other.rollback()
        other.close()
        writer.close()


def test_earliest_event_time_ignores_undated_and_absent(repo):
    # None when the key has no dated rows (Change 2: 'no records that far back'
    # vs 'empty period' are distinct honest reasons).
    assert repo.earliest_event_time("Vulpes vulpes") is None

    repo.upsert_event(_event("<a@x>", binomial="Vulpes vulpes",
                             when=datetime(2026, 7, 10, 5, 0, tzinfo=UTC)))
    repo.upsert_event(_event("<b@x>", binomial="Vulpes vulpes",
                             when=datetime(2026, 7, 8, 9, 0, tzinfo=UTC)))
    # An undated row must not become the 'earliest'.
    undated = _event("<u@x>", binomial="Vulpes vulpes", when=datetime(2026, 7, 1, tzinfo=UTC))
    undated.event_time_utc = None
    repo.upsert_event(undated)

    assert repo.earliest_event_time("Vulpes vulpes") == datetime(2026, 7, 8, 9, 0, tzinfo=UTC)
    assert repo.earliest_event_time("Meles meles") is None      # other key unaffected
