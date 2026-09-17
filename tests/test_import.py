"""Stage 9: adopting an existing corpus as a project.

Tested against a SYNTHETIC fixture that reproduces the real corpus's awkward
shape — two image roots, backslash-separated CWD-anchored paths, orphan
directories, a WAL sidecar — rather than against `data/`. The real corpus is
read-only evidence; a test suite that can write to it is one mistake from
rewriting 787 events of dissertation data, which is why conftest's tripwire
refuses a writable open there at all.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3

import pytest

from ttr.projects import paths as ppaths
from ttr.projects.errors import ProjectError
from ttr.projects.importer import (ImportVerificationError, build_plan,
                                   run_import)
from ttr.projects.manifest import Mailbox, ProjectManifest
from ttr.projects.registry import Registry
from ttr.storage.digest import verdict_digest
from ttr.storage.migrations import connect, read_owner
from ttr.storage.repository import DetectionRepository

from conftest import make_persisted_event

UTC = dt.timezone.utc

#: Mirrors the real split: some rows under a nested root, some at the top level.
NESTED = 6
TOP = 4
ORPHANS = 5


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(r))
    return r


@pytest.fixture
def corpus(tmp_path):
    """A synthetic stand-in for data/: the same shape, none of the evidence.

    Reproduces every awkward property the real one has:
      * image paths stored with BACKSLASHES, relative to the CWD;
      * two roots — `image_store/live/<id>/` and `image_store/<id>/`;
      * orphan directories referenced by no row;
      * a -wal sidecar beside the database;
      * an unstamped database that already holds rows.
    """
    src = tmp_path / "data"
    src.mkdir()
    images = tmp_path / "image_store"
    (images / "live").mkdir(parents=True)

    db = src / "corpus.db"
    repo = DetectionRepository(db)
    try:
        for i in range(NESTED + TOP):
            mid = f"msg{i:03d}.example.com"
            nested = i < NESTED
            folder = (images / "live" / mid) if nested else (images / mid)
            folder.mkdir(parents=True)
            (folder / "orig.jpg").write_bytes(b"original-" + str(i).encode())
            (folder / "boxed.jpg").write_bytes(b"boxed-" + str(i).encode())
            stored = (f"image_store\\live\\{mid}" if nested
                      else f"image_store\\{mid}")
            repo.upsert_event(make_persisted_event(
                f"<{mid}>", canonical_binomial="Vulpes vulpes",
                event_time=dt.datetime(2026, 7, 2 + i, 9, 0, tzinfo=UTC),
                display_common_name="red fox", upstream_confidence=0.9,
                agreement_flag="agree", cross_check_status="evaluable",
                resolution_basis="exact_species_match", matched_rank="species",
                taxonomic_distance="same_species",
                original_image_path=stored + "\\orig.jpg",
                boxed_image_path=stored + "\\boxed.jpg"))
        # The crop columns are written by `set_crop`, not carried on
        # PersistedEvent. Set directly, so the fixture exercises the crop half of
        # the verdict digest as well as the cross-check half.
        with repo._conn:
            repo._conn.execute(
                "UPDATE detection_events SET crop_status = 'cropped_verified', "
                "crop_basis = 'iou_at_or_above_tau', "
                "localisation_iou = 0.87 + id / 1000.0, "
                "localisation_agreement = 'agree', tt_box_recovered = 1, "
                "tt_box_self_check = 'pass', tt_box_clipped = 0, "
                "tt_banner_basis = 'single_banner_verified', tt_label_mismatch = 0, "
                "agreement_flag_crop = 'agree', "
                "cross_check_status_crop = 'evaluable', "
                "resolution_basis_crop = 'exact_species_match', "
                "matched_rank_crop = 'species', "
                "taxonomic_distance_crop = 'same_species'")
    finally:
        repo.close()

    # Orphans: directories nothing references. In the real store these are
    # duplicates left by _persist_images running before an ON CONFLICT no-op.
    for i in range(ORPHANS):
        orphan = images / f"orphan{i:03d}.example.com"
        orphan.mkdir()
        (orphan / "orig.jpg").write_bytes(b"orphan" * 50)

    # Two categories the real store happens to have NONE of. Included anyway, so
    # the code paths are exercised and the reconciliation is tested against a
    # store that actually populates every bucket — "zero today" is not "zero
    # forever", and a category nobody counts is one that can hide a surprise.
    nested_orphan = images / "live" / "nested-orphan.example.com"
    nested_orphan.mkdir()
    (nested_orphan / "orig.jpg").write_bytes(b"nested-orphan" * 20)
    (images / "stray-note.txt").write_bytes(b"a file loose in the image store")

    # A WAL sidecar, so the copy-then-checkpoint path is exercised.
    db.with_name(db.name + "-wal").write_bytes(b"")

    seen = src / "seen_message_ids.txt"
    seen.write_text(
        "\n".join([f"<msg{i:03d}.example.com>" for i in range(NESTED + TOP)]
                  + ["<not-an-alert-1>", "<not-an-alert-2>"]) + "\n",
        encoding="utf-8")

    alias = tmp_path / "species_aliases.yaml"
    alias.write_text(
        'Vulpes vulpes:\n  raw_tokens: ["VulpesVulpes"]\n'
        '  common_names: ["red fox"]\n', encoding="utf-8")

    return {"db": db, "images": images, "seen": seen, "alias": alias,
            "rows": NESTED + TOP}


def _plan(corpus, root, **kw):
    kw.setdefault("name", "Imported Site")
    kw.setdefault("db_source", corpus["db"])
    kw.setdefault("images_source", corpus["images"])
    kw.setdefault("seen_source", corpus["seen"])
    return build_plan(root=root, **kw)


# =========================================================================== #
# Planning writes nothing
# =========================================================================== #
def test_the_plan_describes_the_move_without_making_it(corpus, root):
    plan = _plan(corpus, root)

    assert plan.row_count == corpus["rows"]
    assert len(plan.referenced) == corpus["rows"]
    assert len(plan.orphans) == ORPHANS
    assert plan.collisions == []
    assert plan.roots_seen == {"live": NESTED * 2, "(top level)": TOP * 2}
    assert plan.source_digest == verdict_digest(corpus["db"])

    # Nothing created.
    assert not plan.project_dir.exists()
    assert Registry.load(root).entries == []


def test_dry_run_through_the_cli_writes_nothing(corpus, root):
    from typer.testing import CliRunner

    from ttr.cli import app

    before = sorted(p.name for p in corpus["images"].iterdir())
    result = CliRunner().invoke(app, [
        "project", "import", "--name", "Imported Site",
        "--db", str(corpus["db"]), "--images", str(corpus["images"]),
        "--seen", str(corpus["seen"]), "--alias-table", str(corpus["alias"]),
        "--dry-run"], env={ppaths.ROOT_ENV_VAR: str(root)})

    assert result.exit_code == 0
    assert "dry run - nothing written" in result.output   # ASCII: CLI output
    # is a surface people screenshot, and a console that cannot encode an
    # em-dash renders it as a replacement box
    assert Registry.load(root).entries == []
    assert not (root / "projects").exists()
    assert sorted(p.name for p in corpus["images"].iterdir()) == before


# =========================================================================== #
# The collision refusal
# =========================================================================== #
def test_a_message_id_under_both_roots_is_refused(corpus, root):
    """Flattening two roots into one images/ would make one silently overwrite
    the other, and the losing row would then resolve to the wrong frames.

    Phase 1b's counts suggest the referenced sets are disjoint. Suggest is not
    enough when THIS is the failure mode.
    """
    shared = "msg000.example.com"                      # already under live/
    duplicate = corpus["images"] / shared              # now also at the top level
    duplicate.mkdir()
    (duplicate / "orig.jpg").write_bytes(b"a different original")

    repo = DetectionRepository(corpus["db"])
    try:
        repo.upsert_event(make_persisted_event(
            "<clash@x>", canonical_binomial="Vulpes vulpes",
            event_time=dt.datetime(2026, 8, 1, tzinfo=UTC),
            original_image_path=f"image_store\\{shared}\\orig.jpg"))
    finally:
        repo.close()

    plan = _plan(corpus, root)
    assert plan.collisions == [shared]

    with pytest.raises(ProjectError) as exc:
        run_import(plan, root=root)
    # Normalised: the message is hard-wrapped for the terminal, so the phrases
    # under test span line breaks.
    message = " ".join(str(exc.value).split())
    assert "BOTH image roots" in message
    assert shared in message
    assert "silently overwrite" in message
    assert not plan.project_dir.exists()


# =========================================================================== #
# A real import
# =========================================================================== #
@pytest.fixture
def imported(corpus, root):
    plan = _plan(corpus, root, alias_source=corpus["alias"])
    said = []
    manifest, plan = run_import(plan, root=root, notify=said.append,
                                mailbox=Mailbox(address="a@gmail.com",
                                                imap_user="a@gmail.com",
                                                imap_host="imap.gmail.com"))
    from ttr.projects.service import context_for

    ctx = context_for(Registry.load(root).get(manifest.id), root)
    return {"manifest": manifest, "plan": plan, "ctx": ctx, "said": said}


def test_the_corpus_arrives_intact(imported, corpus):
    ctx, plan = imported["ctx"], imported["plan"]

    repo = ctx.open_repo()
    try:
        assert sum(repo.species_counts().values()) == corpus["rows"]
    finally:
        repo.close()

    # THE acceptance test: a migration moves bytes, it does not re-decide.
    assert verdict_digest(ctx.db_path) == plan.source_digest
    assert any("verdict digest reproduces exactly" in m for m in imported["said"])


def test_the_originals_are_untouched(imported, corpus):
    """They are the backup, which is the whole reason copy is the default."""
    import hashlib

    assert corpus["db"].is_file()
    conn = sqlite3.connect(f"file:{corpus['db']}?mode=ro", uri=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM detection_events").fetchone()[0] == corpus["rows"]
        # Still CWD-anchored and backslashed: the rewrite happened on the COPY.
        stored = conn.execute(
            "SELECT original_image_path FROM detection_events "
            "ORDER BY source_message_id").fetchone()[0]
        assert "\\" in stored and stored.startswith("image_store")
    finally:
        conn.close()

    # Every referenced source directory is still there.
    for ref in imported["plan"].referenced:
        assert ref.source_dir.is_dir()


def test_the_database_is_adopted_with_a_notice(imported):
    """An unstamped database WITH rows is case 2 of the stamping stage."""
    ctx = imported["ctx"]
    conn = sqlite3.connect(f"file:{ctx.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        assert read_owner(conn) == ctx.id
    finally:
        conn.close()


def test_the_seen_store_comes_across(imported, corpus):
    """The single most likely migration mistake: without it the first `ttr run`
    re-downloads the whole mailbox and re-persists every image, which is how the
    orphans were created in the first place."""
    ctx = imported["ctx"]
    assert ctx.seen_store_path.is_file()

    lines = [l for l in ctx.seen_store_path.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    assert len(lines) == corpus["rows"] + 2          # + the two non-alerts
    assert "<not-an-alert-1>" in lines
    assert any("seen-store copied" in m for m in imported["said"])


def test_orphans_are_dropped_and_referenced_directories_copied(imported, corpus):
    ctx, plan = imported["ctx"], imported["plan"]

    copied = sorted(p.name for p in ctx.image_store_dir.iterdir() if p.is_dir())
    assert len(copied) == corpus["rows"]
    assert not any(name.startswith("orphan") for name in copied)
    # ...and both roots were flattened into one.
    assert "live" not in copied

    # The orphans are still in the SOURCE — dropped, not deleted.
    assert len(plan.orphans) == ORPHANS
    for orphan in plan.orphans:
        assert orphan.is_dir()


def test_paths_are_rewritten_and_resolve_from_a_foreign_cwd(imported, tmp_path):
    """The property the rewrite exists to create.

    Resolving from the repository root would prove nothing — the ORIGINAL paths
    resolve there too. So the check is made with the process standing somewhere
    entirely unrelated.
    """
    ctx = imported["ctx"]

    conn = sqlite3.connect(f"file:{ctx.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT original_image_path, boxed_image_path "
                            "FROM detection_events").fetchall()
    finally:
        conn.close()

    elsewhere = tmp_path / "somewhere-unrelated"
    elsewhere.mkdir()
    previous = os.getcwd()
    os.chdir(elsewhere)
    try:
        for row in rows:
            for column in ("original_image_path", "boxed_image_path"):
                stored = row[column]
                assert stored, "a row lost its image path"
                assert "\\" not in stored, "backslashes survived the rewrite"
                assert not stored.startswith("image_store"), \
                    "path is still image-store-prefixed"
                assert (ctx.image_store_dir / stored).is_file(), \
                    f"{stored} does not resolve from {os.getcwd()}"
    finally:
        os.chdir(previous)


def test_the_alias_table_is_carried_and_snapshotted(imported):
    ctx = imported["ctx"]
    assert (ctx.dir / "species_aliases.yaml").is_file()
    assert ctx.alias_table_source() == "project"

    alias_map = ctx.load_alias_map()
    assert ctx.alias_snapshot_path(alias_map.content_sha256).is_file()
    assert alias_map.canonical_key_for_token("VulpesVulpes") == "Vulpes vulpes"


def test_rows_keep_null_alias_hashes_unless_backfill_is_asked_for(imported):
    """NULL means "resolved before provenance was recorded". True, and better
    than inventing an attribution."""
    ctx = imported["ctx"]
    conn = sqlite3.connect(f"file:{ctx.db_path}?mode=ro", uri=True)
    try:
        hashes = {r[0] for r in conn.execute(
            "SELECT alias_table_sha256 FROM detection_events")}
    finally:
        conn.close()
    assert hashes == {None}


def test_backfill_is_opt_in_and_declares_itself_an_assertion(corpus, root):
    plan = _plan(corpus, root, alias_source=corpus["alias"])
    said = []
    run_import(plan, root=root, backfill_alias_hash=True, notify=said.append)

    from ttr.projects.service import context_for

    ctx = context_for(Registry.load(root).entries[0], root)
    conn = sqlite3.connect(f"file:{ctx.db_path}?mode=ro", uri=True)
    try:
        hashes = {r[0] for r in conn.execute(
            "SELECT alias_table_sha256 FROM detection_events")}
    finally:
        conn.close()

    assert len(hashes) == 1 and None not in hashes
    joined = "\n".join(said)
    assert "ASSERTION BY THE OPERATOR" in joined
    assert "not a recovered fact" in joined


# =========================================================================== #
# Warnings and failure
# =========================================================================== #
def test_importing_without_an_alias_table_warns_prominently(corpus, root):
    """The consistency trap: the imported rows were resolved under SOME table,
    and without it new rows may resolve differently inside one corpus."""
    from typer.testing import CliRunner

    from ttr.cli import app

    result = CliRunner().invoke(app, [
        "project", "import", "--name", "No Table",
        "--db", str(corpus["db"]), "--images", str(corpus["images"]),
        "--dry-run"], env={ppaths.ROOT_ENV_VAR: str(root)})

    assert result.exit_code == 0
    assert "WARNING: no --alias-table was given" in result.output
    assert "may resolve to different species" in result.output
    assert "bundled illustrative example" in result.output


def test_a_digest_mismatch_fails_the_import_and_leaves_nothing(corpus, root, monkeypatch):
    """A mismatch is a migration BUG, not a finding — so it raises rather than
    warning, and the half-made project is removed."""
    import ttr.projects.importer as importer

    plan = _plan(corpus, root)
    monkeypatch.setattr(importer, "verdict_digest",
                        lambda path: "deadbeef" if str(path) != str(corpus["db"])
                        else plan.source_digest)

    with pytest.raises(ImportVerificationError) as exc:
        run_import(plan, root=root)

    assert "does not match the source" in str(exc.value)
    assert "not a finding" in str(exc.value)
    assert not plan.project_dir.exists()
    assert Registry.load(root).entries == []


def test_a_missing_source_is_refused_before_anything_happens(corpus, root, tmp_path):
    with pytest.raises(ProjectError) as exc:
        build_plan(name="Nope", db_source=tmp_path / "nothing.db",
                   images_source=corpus["images"], root=root)
    assert "no such database" in str(exc.value)


def test_the_wal_sidecar_travels_and_the_copy_is_checkpointed(imported, corpus):
    """A 0-byte WAL today is not a guarantee for whenever this actually runs, so
    the sidecars are copied and the COPY is flattened — never the original."""
    assert "corpus.db-wal" in imported["plan"].sidecars
    ctx = imported["ctx"]
    conn = sqlite3.connect(str(ctx.db_path))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()
    # The source's own sidecar is untouched.
    assert corpus["db"].with_name(corpus["db"].name + "-wal").exists()


# =========================================================================== #
# Every byte in the store is accounted for
# =========================================================================== #
def test_the_plan_accounts_for_the_whole_image_store(corpus, root):
    """Four buckets, and they must add up to the measured size.

    A plan that reports only what it copies and what it drops at the top level
    leaves a remainder nobody has looked at. The remainder is exactly where an
    unpleasant surprise would sit, so the plan measures the store as a whole and
    reconciles against it.
    """
    plan = _plan(corpus, root)

    # Every category is populated, including the two the real corpus has none
    # of — so this is a test of the arithmetic, not of one store's happenstance.
    assert len(plan.referenced) == corpus["rows"]
    assert len(plan.orphans) == ORPHANS
    assert len(plan.nested_orphans) == 1
    assert len(plan.loose_files) == 1
    assert plan.nested_orphan_bytes > 0
    assert plan.loose_bytes > 0

    # And they reconcile exactly.
    assert plan.store_bytes == sum(
        p.stat().st_size for p in corpus["images"].rglob("*") if p.is_file())
    assert plan.accounted_bytes == plan.store_bytes
    assert plan.unaccounted_bytes == 0


def test_a_nested_orphan_is_not_mistaken_for_a_referenced_directory(imported, corpus):
    """It is dropped like any other orphan, and left alone in the source."""
    plan = imported["plan"]
    (nested,) = plan.nested_orphans

    assert nested.is_dir()                                    # dropped, not deleted
    images = imported["plan"].images_dest
    assert nested.name not in {p.name for p in images.iterdir()}
    assert not (images / "live").exists()                     # roots are flattened


def test_the_reconciliation_is_printed_in_the_dry_run(corpus, root):
    from typer.testing import CliRunner

    from ttr.cli import app

    result = CliRunner().invoke(app, [
        "project", "import", "--name", "Imported Site",
        "--db", str(corpus["db"]), "--images", str(corpus["images"]),
        "--seen", str(corpus["seen"]), "--alias-table", str(corpus["alias"]),
        "--dry-run"], env={ppaths.ROOT_ENV_VAR: str(root)})

    assert result.exit_code == 0
    assert "orphan (inside a root)" in result.output
    assert "loose files" in result.output
    assert "measured store size" in result.output
    assert "unaccounted                        0 B" in result.output
    assert "UNACCOUNTED" not in result.output


# =========================================================================== #
# --from-env carries the site across, and says what it could not find
# =========================================================================== #
def _from_env_dry_run(corpus, root, tmp_path, monkeypatch, env_text):
    """Invoke the dry run with --from-env against a .env we control.

    `_read_env_file` reads "./.env", so the CWD is moved first: a test that
    reached the developer's real .env would be reading live IMAP settings.
    """
    from typer.testing import CliRunner

    from ttr.cli import app

    workdir = tmp_path / "cwd"
    workdir.mkdir()
    (workdir / ".env").write_text(env_text, encoding="utf-8")
    monkeypatch.chdir(workdir)
    for key in ("SITE_NAME", "WEATHER_LATITUDE", "WEATHER_LONGITUDE",
                "IMAP_USER", "IMAP_HOST", "IMAP_FOLDER"):
        monkeypatch.delenv(key, raising=False)

    return CliRunner().invoke(app, [
        "project", "import", "--name", "Imported Site",
        "--db", str(corpus["db"]), "--images", str(corpus["images"]),
        "--seen", str(corpus["seen"]), "--alias-table", str(corpus["alias"]),
        "--from-env", "--dry-run"], env={ppaths.ROOT_ENV_VAR: str(root)})


def test_from_env_recovers_the_site_as_well_as_the_mailbox(corpus, root, tmp_path,
                                                           monkeypatch):
    """SITE_NAME and the coordinates were removed from Settings alongside the
    mailbox keys, so `.env` is equally the last place they survive. Losing them
    silently would leave the imported project unable to backfill weather."""
    result = _from_env_dry_run(corpus, root, tmp_path, monkeypatch, (
        "IMAP_USER=alerts@example.com\n"
        "IMAP_HOST=imap.example.com\n"
        "SITE_NAME=Back Garden\n"
        "WEATHER_LATITUDE=51.5\n"
        "WEATHER_LONGITUDE=-0.12\n"
        "IMAP_PASSWORD=not-read-here\n"))

    assert result.exit_code == 0
    assert "site       Back Garden" in result.output
    assert "coords     51.5, -0.12" in result.output
    assert "could NOT find" not in result.output
    assert "not-read-here" not in result.output          # the password is not a site


def test_from_env_names_what_it_could_not_find(corpus, root, tmp_path, monkeypatch):
    """Absent is reported, not left looking like a deliberate 'unset'."""
    result = _from_env_dry_run(corpus, root, tmp_path, monkeypatch,
                               "IMAP_USER=alerts@example.com\n"
                               "IMAP_HOST=imap.example.com\n")

    assert result.exit_code == 0
    assert "could NOT find" in result.output
    assert "SITE_NAME" in result.output
    assert "WEATHER_LATITUDE" in result.output
    assert "WEATHER_LONGITUDE" in result.output
    assert "site       Imported Site" in result.output    # falls back to --name
    assert "coords     (unset" in result.output


def test_half_a_coordinate_pair_is_not_a_location(corpus, root, tmp_path, monkeypatch):
    """Weather enrichment would have to invent the other half, so neither is used."""
    result = _from_env_dry_run(corpus, root, tmp_path, monkeypatch,
                               "SITE_NAME=Back Garden\nWEATHER_LATITUDE=51.5\n")

    assert result.exit_code == 0
    assert "coords     (unset" in result.output
    assert "only one coordinate found" in result.output


def test_the_recovered_site_reaches_the_manifest(corpus, root, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from ttr.cli import app
    from ttr.projects.manifest import ProjectManifest

    workdir = tmp_path / "cwd"
    workdir.mkdir()
    (workdir / ".env").write_text("SITE_NAME=Back Garden\nWEATHER_LATITUDE=51.5\n"
                                  "WEATHER_LONGITUDE=-0.12\n", encoding="utf-8")
    monkeypatch.chdir(workdir)

    result = CliRunner().invoke(app, [
        "project", "import", "--name", "Imported Site",
        "--db", str(corpus["db"]), "--images", str(corpus["images"]),
        "--seen", str(corpus["seen"]), "--alias-table", str(corpus["alias"]),
        "--from-env"], env={ppaths.ROOT_ENV_VAR: str(root)})

    assert result.exit_code == 0, result.output
    entry = Registry.load(root).entries[0]
    manifest = ProjectManifest.read(root / "projects" / entry.directory)
    assert manifest.site.name == "Back Garden"
    assert manifest.site.latitude == 51.5
    assert manifest.site.longitude == -0.12
