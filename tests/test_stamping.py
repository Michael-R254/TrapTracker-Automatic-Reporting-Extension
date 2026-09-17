"""Stage 8: which project owns this database, and which table resolved its rows.

Two additive schema changes, both answering a question that could not be asked
before: `project_meta` says who owns a database, `alias_table_sha256` says what
resolved a row's species.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from ttr.projects import paths as ppaths
from ttr.projects.registry import Registry
from ttr.projects.service import context_for, create_project, removal_plan
from ttr.species.aliases import SpeciesAliasMap, content_hash
from ttr.storage.migrations import (APPLICATION_ID, DatabaseOwnershipError,
                                    PROJECT_META_VERSION, connect, read_owner,
                                    verify_ownership)
from ttr.storage.repository import DetectionRepository

from conftest import make_persisted_event

UTC = dt.timezone.utc


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(r))
    return r


def _project(root, name):
    manifest, _ = create_project(name, f"{name[0].lower()}@gmail.com", root=root,
                                 skip_connection_test=True)
    return context_for(Registry.load(root).get(manifest.id), root)


def _seed(repo, n=3, mid="e"):
    for i in range(n):
        repo.upsert_event(make_persisted_event(
            f"<{mid}{i}@x>", canonical_binomial="Vulpes vulpes",
            event_time=dt.datetime(2026, 7, 2 + i, 9, 0, tzinfo=UTC)))


# =========================================================================== #
# application_id
# =========================================================================== #
def test_application_id_is_stamped_on_creation_and_survives_a_reopen(tmp_path):
    """A different question from ownership: this says the FILE is one of ours,
    which any tool can check without knowing anything about projects."""
    db = tmp_path / "a.db"
    connect(db).close()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    finally:
        conn.close()

    connect(db).close()                                  # reopen
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    finally:
        conn.close()


def test_reopening_does_not_take_a_write_lock_for_the_application_id(tmp_path):
    """`PRAGMA application_id=` WRITES the header. Setting it unconditionally on
    every open turned an ordinary concurrent read into 'database is locked', so
    it is set only when it differs."""
    db = tmp_path / "b.db"
    first = connect(db)
    try:
        second = connect(db)                             # must not block or raise
        try:
            assert second.execute("SELECT 1").fetchone()[0] == 1
        finally:
            second.close()
    finally:
        first.close()


# =========================================================================== #
# project_meta: the three cases
# =========================================================================== #
def test_a_fresh_database_is_adopted_silently(tmp_path, capsys):
    db = tmp_path / "fresh.db"
    conn = connect(db, expected_project_id="proj-a")
    try:
        assert read_owner(conn) == "proj-a"
        row = conn.execute("SELECT * FROM project_meta").fetchone()
        assert row["schema_version"] == PROJECT_META_VERSION
        assert row["created_utc"]
    finally:
        conn.close()
    # Nothing said: an empty database has no history worth announcing.
    assert capsys.readouterr().err == ""


def test_a_legacy_database_with_rows_is_adopted_WITH_a_notice(tmp_path):
    """Silently claiming somebody's existing corpus is the kind of thing they
    should get to see happen."""
    db = tmp_path / "legacy.db"
    repo = DetectionRepository(db)                       # no project id: unstamped
    try:
        _seed(repo, 5)
    finally:
        repo.close()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        assert read_owner(conn) is None                  # genuinely unstamped
    finally:
        conn.close()

    said = []
    conn = connect(db)
    try:
        owner = verify_ownership(conn, "proj-a", notify=said.append)
    finally:
        conn.close()

    assert owner == "proj-a"
    assert len(said) == 1
    assert "5 detection event(s)" in said[0]
    assert "proj-a" in said[0]
    assert "predates database ownership stamping" in said[0]


def test_an_empty_project_meta_table_is_unstamped_not_owned(tmp_path):
    """The fourth case, and the one that actually happened.

    `apply_migrations` CREATEs `project_meta` whether or not anything then stamps
    a row into it, so any process that opens a legacy database — a stale server
    lazily importing new code, an interrupted import — leaves the table behind
    empty. A table is not an owner: this must read as unstamped and adopt with the
    same notice, not as "owned by nobody" and certainly not as a mismatch.
    """
    db = tmp_path / "table-but-no-row.db"
    repo = DetectionRepository(db)
    try:
        _seed(repo, 3)
    finally:
        repo.close()

    # Exactly what the stale process did: migrate, stamp nothing.
    conn = connect(db)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'project_meta'").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM project_meta").fetchone()[0] == 0
        assert read_owner(conn) is None                  # a table is not a claim
    finally:
        conn.close()

    said = []
    conn = connect(db)
    try:
        owner = verify_ownership(conn, "proj-a", notify=said.append)
    finally:
        conn.close()

    assert owner == "proj-a"
    assert len(said) == 1                                # adopted, and SAID SO
    assert "3 detection event(s)" in said[0]

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        assert read_owner(conn) == "proj-a"              # now genuinely stamped
    finally:
        conn.close()


def test_the_import_adopts_a_database_left_with_an_empty_project_meta(tmp_path):
    """And the import path reaches the same conclusion — this is the state the
    real corpus was in when it was adopted."""
    from ttr.projects.importer import build_plan

    db = tmp_path / "corpus.db"
    repo = DetectionRepository(db)
    try:
        _seed(repo, 4)
    finally:
        repo.close()
    connect(db).close()                                  # leaves project_meta empty

    images = tmp_path / "images"
    images.mkdir()
    plan = build_plan(name="Adopted", db_source=db, images_source=images,
                      root=tmp_path / "root")

    assert plan.row_count == 4                           # planned as adoptable


def test_a_mismatched_database_is_refused_and_nothing_is_written(tmp_path):
    """The refusal is the point. A manifest edited by hand, a directory copied
    and half renamed, a symlink — each writes plausible rows into another
    deployment's record, with nothing afterwards able to tell them apart."""
    db = tmp_path / "owned.db"
    conn = connect(db, expected_project_id="proj-a")
    try:
        _seed(DetectionRepository(db), 0)                # just establish the stamp
    finally:
        conn.close()

    before = db.read_bytes()

    with pytest.raises(DatabaseOwnershipError) as exc:
        connect(db, expected_project_id="proj-b")

    message = str(exc.value)
    assert "belongs to a different project" in message
    assert "proj-b" in message and "proj-a" in message
    assert "Refusing to open it" in message

    # Nothing written, and the stamp is untouched.
    assert db.read_bytes() == before
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        assert read_owner(conn) == "proj-a"
    finally:
        conn.close()


def test_adopt_overrides_a_mismatch_explicitly(tmp_path):
    """The migration importer legitimately opens a database that is not yet
    this project's. An explicit flag, never a silent special case."""
    db = tmp_path / "adopt.db"
    connect(db, expected_project_id="proj-a").close()

    with pytest.raises(DatabaseOwnershipError):
        connect(db, expected_project_id="proj-b")

    conn = connect(db, expected_project_id="proj-b", adopt=True)
    try:
        assert read_owner(conn) == "proj-b"
    finally:
        conn.close()


def test_a_bare_repository_still_works_with_no_project_in_sight(tmp_path):
    """What keeps `ttr enrich` and the existing suite alive. Verification is
    opt-in FROM the project layer; a caller with no manifest supplies nothing
    and skips it."""
    db = tmp_path / "bare.db"
    repo = DetectionRepository(db)                       # no expected_project_id
    try:
        _seed(repo, 2)
        assert sum(repo.species_counts().values()) == 2
    finally:
        repo.close()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        assert read_owner(conn) is None                  # unstamped, and fine
    finally:
        conn.close()


# =========================================================================== #
# project_meta, through the project layer
# =========================================================================== #
def test_open_repo_refuses_another_projects_database(root):
    a, b = _project(root, "Alpha"), _project(root, "Beta")

    repo = b.open_repo()                                 # stamps B's database
    try:
        _seed(repo, 4, mid="b")
    finally:
        repo.close()

    from ttr.projects.manifest import ProjectManifest

    manifest = ProjectManifest.read(a.dir)
    manifest.storage.database = str(b.db_path)
    manifest.storage.database_external = True
    manifest.write(a.dir)

    from ttr.projects.context import ProjectContext

    mispointed = ProjectContext.load(a.dir)
    with pytest.raises(DatabaseOwnershipError):
        mispointed.open_repo()

    # B's rows are untouched.
    repo = b.open_repo()
    try:
        assert sum(repo.species_counts().values()) == 4
    finally:
        repo.close()


def test_the_removal_plan_withholds_a_foreign_databases_row_count(root):
    """A deletion plan is the worst possible place to be reassured by a number
    that is not yours."""
    a, b = _project(root, "Alpha"), _project(root, "Beta")
    repo = b.open_repo()
    try:
        _seed(repo, 7, mid="b")
    finally:
        repo.close()

    from ttr.projects.manifest import ProjectManifest

    manifest = ProjectManifest.read(a.dir)
    manifest.storage.database = str(b.db_path)
    manifest.storage.database_external = True
    manifest.write(a.dir)

    plan = removal_plan("Alpha", root=root)
    assert plan.foreign_database_owner == b.id
    assert plan.row_count is None                        # never B's 7


# =========================================================================== #
# alias_table_sha256
# =========================================================================== #
def test_the_hash_is_stable_for_identical_content_and_differs_otherwise():
    text = "Vulpes vulpes:\n  raw_tokens: [VulpesVulpes]\n"
    assert content_hash(text) == content_hash(text)
    assert content_hash(text) != content_hash(text + "\n# a comment\n")
    assert len(content_hash(text)) == 16


def test_a_map_built_from_text_carries_its_hash_and_one_from_a_dict_does_not():
    """None means "we do not know", which is different from any real hash and
    should not be confused with one."""
    text = "Vulpes vulpes: {}\n"
    assert SpeciesAliasMap.from_text(text).content_sha256 == content_hash(text)
    assert SpeciesAliasMap({"Vulpes vulpes": {}}).content_sha256 is None


def test_a_project_on_the_bundled_table_records_the_bundled_tables_hash(root):
    """More honest than NULL: it keeps "resolved against the shipped example"
    distinguishable from "we have no record"."""
    from ttr.species.aliases import bundled_example_alias_text

    ctx = _project(root, "Bundled")
    assert ctx.alias_table_source() == "bundled"

    alias_map = ctx.load_alias_map()
    assert alias_map.content_sha256 == content_hash(bundled_example_alias_text())


def test_the_snapshot_is_written_once_per_distinct_hash(root):
    ctx = _project(root, "Snapped")

    first = ctx.load_alias_map()
    path = ctx.alias_snapshot_path(first.content_sha256)
    assert path.is_file()
    stamp = path.stat().st_mtime_ns
    body = path.read_text(encoding="utf-8")

    ctx.load_alias_map()                                 # again
    assert path.stat().st_mtime_ns == stamp              # not rewritten
    assert path.read_text(encoding="utf-8") == body

    # A hash IDENTIFIES a table; the snapshot PRESERVES it. Both are needed.
    assert content_hash(body) == first.content_sha256


def test_a_project_local_table_gets_its_own_snapshot(root):
    ctx = _project(root, "Own")
    (ctx.dir / "species_aliases.yaml").write_text(
        'Vulpes vulpes:\n  raw_tokens: ["HouseholdFoxUnit"]\n', encoding="utf-8")

    from ttr.projects.context import ProjectContext

    ctx = ProjectContext.load(ctx.dir)
    alias_map = ctx.load_alias_map()

    snapshot = ctx.alias_snapshot_path(alias_map.content_sha256)
    assert snapshot.is_file()
    assert "HouseholdFoxUnit" in snapshot.read_text(encoding="utf-8")


def test_new_rows_carry_the_hash_and_existing_rows_stay_null(root):
    """No back-fill. NULL means "resolved before provenance was recorded", which
    is true; inventing an attribution for those rows would not be."""
    ctx = _project(root, "Stamped")

    repo = ctx.open_repo()
    try:
        _seed(repo, 2, mid="old")                        # written with no hash
    finally:
        repo.close()

    alias_map = ctx.load_alias_map()
    repo = ctx.open_repo()
    try:
        repo.upsert_event(make_persisted_event(
            "<new@x>", canonical_binomial="Vulpes vulpes",
            event_time=dt.datetime(2026, 7, 9, 9, 0, tzinfo=UTC),
            alias_table_sha256=alias_map.content_sha256))
    finally:
        repo.close()

    conn = sqlite3.connect(ctx.db_path)
    try:
        rows = dict(conn.execute(
            "SELECT source_message_id, alias_table_sha256 FROM detection_events"))
    finally:
        conn.close()

    assert rows["<old0@x>"] is None
    assert rows["<old1@x>"] is None
    assert rows["<new@x>"] == alias_map.content_sha256


def test_the_pipeline_stamps_the_hash_of_the_map_that_resolved_the_row(root):
    """Read off the map that did the resolving, so the two cannot disagree."""
    import ttr.enrichment.factory as factory
    from ttr.enrichment.base import DescriptionRead, TaxonomicRead
    from ttr.pipeline import build_pipeline_for
    from ttr.sources.base import DetectionEvent

    from conftest import FakeDescriptionEnricher, FakeTaxonomicEnricher

    ctx = _project(root, "Piped")
    alias_map = ctx.load_alias_map()

    class _App:
        vlm_fallback_to_original_on_missing_boxed = False
        agreement_topk_join = False

    original_taxo = factory.build_taxonomic_enricher
    original_desc = factory.build_description_enricher
    factory.build_taxonomic_enricher = lambda s: FakeTaxonomicEnricher(
        TaxonomicRead(provider="bioclip", model_name="fake", ok=False))
    factory.build_description_enricher = lambda s: FakeDescriptionEnricher(
        DescriptionRead(provider="ollama", model_name="fake", ok=False))
    try:
        bundle = build_pipeline_for(ctx, alias_map=alias_map, app_settings=_App())
        try:
            bundle.pipeline.process_event(DetectionEvent(
                source_type="email", source_message_id="<p1@x>",
                ingested_at_utc=dt.datetime(2026, 7, 1, tzinfo=UTC),
                upstream_project="demo", upstream_label="VulpesVulpes",
                upstream_best_confidence=0.9, upstream_image_id=None,
                upstream_event_time_utc=dt.datetime(2026, 7, 1, tzinfo=UTC),
                images=[], field_provenance={}))
        finally:
            bundle.close()
    finally:
        factory.build_taxonomic_enricher = original_taxo
        factory.build_description_enricher = original_desc

    repo = ctx.open_repo()
    try:
        stored = repo.query_all(start_utc=dt.datetime(2026, 6, 1, tzinfo=UTC),
                                end_utc=dt.datetime(2026, 8, 1, tzinfo=UTC))
    finally:
        repo.close()
    assert stored and stored[0].alias_table_sha256 == alias_map.content_sha256


def test_the_hash_takes_no_part_in_resolution(root):
    """It is recorded ALONGSIDE the result, never consulted to reach one. Two
    maps over identical content, one with its hash blanked: same answers."""
    text = ("Vulpes vulpes:\n"
            "  raw_tokens: [VulpesVulpes]\n"
            "  common_names: [red fox]\n"
            "Meles meles:\n"
            "  raw_tokens: [MelesMeles]\n"
            "  common_names: [badger]\n")
    with_hash = SpeciesAliasMap.from_text(text)
    without = SpeciesAliasMap.from_text(text)
    without.content_sha256 = None                        # blank it entirely

    assert with_hash.content_sha256 and without.content_sha256 is None
    for token in ("VulpesVulpes", "MelesMeles", "NotAThing"):
        assert (with_hash.canonical_key_for_token(token)
                == without.canonical_key_for_token(token))
    for term in ("red fox", "badger", "Vulpes vulpes"):
        assert with_hash.resolve(term) == without.resolve(term)
    assert (with_hash.binomial_for_bioclip("Vulpes vulpes")
            == without.binomial_for_bioclip("Vulpes vulpes"))
