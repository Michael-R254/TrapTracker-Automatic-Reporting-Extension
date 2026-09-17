"""Stage 1: the project registry, manifest, identity rules and lifecycle.

Everything runs against a tmp_path projects root via TTR_PROJECTS_ROOT, so no
test can see or touch the developer's real projects.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ttr.cli import app
from ttr.projects import paths as ppaths
from ttr.projects.errors import (AmbiguousProject, ProjectError, ProjectNotFound,
                                 UnsupportedProvider)
from ttr.projects.manifest import ProjectManifest
from ttr.projects.registry import Registry, RegistryEntry
from ttr.projects.service import (context_for, create_project, delete_project,
                                  project_dir_for, removal_plan, rename_project,
                                  set_archived, set_site, use_project)

from conftest import mailbox_factory

runner = CliRunner()

#: Shaped like a real Gmail app password (four groups of four).
TEST_PASSWORD = "abcd efgh ijkl mnop"


def make_project(name, email="x@gmail.com", *, root, messages=7, **kw):
    """Create a project with the mailbox mocked.

    Every successful creation now runs a connection test (Stage 2), so the suite
    drives a fake mailbox rather than a network. Tests that exercise a REFUSAL
    call `create_project` directly, because the provider check happens before any
    connection is attempted.
    """
    kw.setdefault("mailbox_factory", mailbox_factory(messages=messages))
    return create_project(name, email, TEST_PASSWORD, root=root, **kw)


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """An isolated projects root, selected the way a user would select one."""
    r = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(r))
    return r


# --------------------------------------------------------------------------- #
# Root resolution
# --------------------------------------------------------------------------- #
def test_projects_root_ignores_the_working_directory(tmp_path, monkeypatch):
    """The bug this guards is real and lives in Settings: env_file=".env" is
    CWD-relative (config.py:30), so running ttr from elsewhere silently loads
    nothing. Resolving a project ROOT that way would be worse -- the same command
    would operate on a different set of projects depending on where it ran."""
    monkeypatch.delenv(ppaths.ROOT_ENV_VAR, raising=False)

    monkeypatch.chdir(tmp_path)
    here = ppaths.projects_root()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    there = ppaths.projects_root()

    assert here == there
    assert here.is_absolute()


def test_a_relative_root_override_is_refused(monkeypatch):
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, "some/relative/dir")
    with pytest.raises(ProjectError) as exc:
        ppaths.projects_root()
    assert "absolute" in str(exc.value)


def test_root_override_wins_over_the_platform_default(tmp_path, monkeypatch):
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(tmp_path / "chosen"))
    assert ppaths.projects_root() == tmp_path / "chosen"


# --------------------------------------------------------------------------- #
# Slugs and identity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,expected", [
    ("North Field Camera", "north-field-camera"),
    ("  Spaced   Out  ", "spaced-out"),
    ("UPPER case", "upper-case"),
    ("hyphen--collapse", "hyphen-collapse"),
    ("trailing---", "trailing"),
    ("Site #3 (west)", "site-3-west"),
    # Non-ASCII is stripped, not transliterated.
    ("Café Nord", "caf-nord"),
    # Nothing survives -> the documented fallback. The slug is a readability
    # hint on a directory name, never identity, so this is harmless.
    ("研究地点", "project"),
    ("!!!", "project"),
    ("...", "project"),
    ("", "project"),
])
def test_slugify(name, expected):
    assert ppaths.slugify(name) == expected


def test_slug_is_truncated_to_32_characters():
    slug = ppaths.slugify("a" * 100)
    assert len(slug) == 32


def test_directory_name_makes_same_named_projects_collision_proof(root):
    a, dir_a = make_project("Same Name", "a@gmail.com", root=root)
    b, dir_b = make_project("Same Name", "b@gmail.com", root=root)

    assert a.slug == b.slug == "same-name"
    assert dir_a != dir_b
    assert dir_a.exists() and dir_b.exists()
    assert dir_a.name.startswith("same-name-")
    assert dir_b.name.startswith("same-name-")


# --------------------------------------------------------------------------- #
# Creation, round-trip, isolation
# --------------------------------------------------------------------------- #
def test_two_projects_round_trip_and_are_isolated_on_disk(root):
    m1, d1 = make_project("North Field Camera", "north@gmail.com", root=root)
    m2, d2 = make_project("Back Garden", "garden@googlemail.com", root=root)

    # Each has its own directory, database, image store and seen-store location.
    assert d1 != d2
    for m, d in ((m1, d1), (m2, d2)):
        assert (d / "project.toml").is_file()
        assert m.db_path(d).is_file()
        assert m.image_store_dir(d).is_dir()
        assert m.db_path(d).parent == d

    assert m1.db_path(d1) != m2.db_path(d2)
    assert m1.image_store_dir(d1) != m2.image_store_dir(d2)

    # The manifest round-trips through TOML unchanged.
    for m, d in ((m1, d1), (m2, d2)):
        assert ProjectManifest.read(d) == m

    # Both are registered, and the first created became active.
    registry = Registry.load(root)
    assert {e.id for e in registry.entries} == {m1.id, m2.id}
    assert registry.active_project == m1.id


def test_the_database_gets_the_real_schema(root):
    manifest, project_dir = make_project("Schema", "s@gmail.com", root=root)
    conn = sqlite3.connect(manifest.db_path(project_dir))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(detection_events)")}
    finally:
        conn.close()
    assert "detection_events" in tables and "weather_cache" in tables
    # Built by migrations.connect, so it carries the migrated columns too -- not
    # a hand-rolled CREATE TABLE that could drift from the real one.
    assert {"canonical_binomial", "crop_status", "taxonomic_distance_crop"} <= cols


def test_a_new_project_stores_a_credential_and_defers_coordinates(root):
    manifest, _ = make_project("Deferred", "d@gmail.com", root=root)
    # The credential IS set up at creation now (Stage 2) -- as a reference.
    assert manifest.mailbox.credential_ref == f"traptracker-report:{manifest.id}"
    # Coordinates stay unset: `backfill-weather` refuses without them, which is
    # the correct loud failure rather than a guessed location.
    assert manifest.site.latitude is None
    assert manifest.site.longitude is None
    assert manifest.site.name == "Deferred"
    assert manifest.species.alias_path is None          # bundled tables apply
    assert manifest.species.taxonomy_path is None


def test_the_manifest_holds_a_reference_and_never_the_secret(root):
    manifest, project_dir = make_project("Secretless", "x@gmail.com", root=root)
    text = (project_dir / "project.toml").read_text(encoding="utf-8")

    # The reference is a locator built from the project id -- readable, and
    # useless to anyone who has the file but not the credential store.
    assert f'credential_ref = "traptracker-report:{manifest.id}"' in text
    # The secret itself is nowhere in it, in either the spaced or stripped form.
    assert TEST_PASSWORD not in text
    assert TEST_PASSWORD.replace(" ", "") not in text


def test_creation_is_atomic_on_failure(root, monkeypatch):
    """A failure partway through leaves no half-built project directory."""
    import ttr.storage.migrations as migrations

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(migrations, "connect", boom)
    with pytest.raises(RuntimeError):
        make_project("Doomed", "d@gmail.com", root=root)

    assert not list(ppaths.projects_dir(root).glob("doomed-*"))
    assert Registry.load(root).entries == []


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("domain", ["outlook.com", "hotmail.com", "live.com", "msn.com"])
def test_microsoft_addresses_are_refused_with_the_actual_reason(root, domain):
    with pytest.raises(UnsupportedProvider) as exc:
        make_project("MS", f"someone@{domain}", root=root)
    message = str(exc.value)
    # The DATE matters: without it a user reasonably assumes a misconfiguration
    # and spends an evening on it.
    assert "16 September 2024" in message
    assert "app-password" in message.lower()
    assert "gmail" in message.lower()
    assert not list(ppaths.projects_dir(root).glob("*")) if ppaths.projects_dir(root).exists() else True


def test_an_untested_provider_is_refused_with_the_manual_route(root):
    with pytest.raises(UnsupportedProvider) as exc:
        make_project("Other", "someone@fastmail.com", root=root)
    message = str(exc.value)
    assert "fastmail.com" in message
    assert "project.toml" in message          # names the manual route
    assert "[mailbox]" in message


@pytest.mark.parametrize("address", ["", "no-at-sign", "two@@at.com", "@nolocal.com",
                                     "nodomain@", "trailing@dotless"])
def test_a_malformed_address_is_refused(root, address):
    with pytest.raises(UnsupportedProvider):
        make_project("Bad", address, root=root)


def test_gmail_and_googlemail_both_configure_gmail(root):
    a, _ = make_project("A", "a@gmail.com", root=root)
    b, _ = make_project("B", "b@googlemail.com", root=root)
    for m in (a, b):
        assert m.mailbox.imap_host == "imap.gmail.com"
        assert m.mailbox.imap_use_ssl is True
        assert m.mailbox.imap_folder == "INBOX"
        assert m.mailbox.imap_user == m.mailbox.address


# --------------------------------------------------------------------------- #
# Resolution and ambiguity
# --------------------------------------------------------------------------- #
def test_an_ambiguous_name_is_refused_and_lists_the_ids(root):
    a, _ = make_project("Twin", "a@gmail.com", root=root)
    b, _ = make_project("Twin", "b@gmail.com", root=root)

    with pytest.raises(AmbiguousProject) as exc:
        Registry.load(root).resolve("Twin")

    assert {pid for pid, _ in exc.value.candidates} == {a.id, b.id}
    message = str(exc.value)
    assert a.id in message and b.id in message
    assert "Use the id" in message


def test_resolution_accepts_an_id_prefix_and_a_name(root):
    manifest, _ = make_project("Prefixed", "p@gmail.com", root=root)
    registry = Registry.load(root)
    assert registry.resolve(manifest.id).id == manifest.id
    assert registry.resolve(manifest.id[:8]).id == manifest.id
    assert registry.resolve("prefixed").id == manifest.id      # case-insensitive


def test_an_unknown_term_lists_what_does_exist(root):
    make_project("Known", "k@gmail.com", root=root)
    with pytest.raises(ProjectNotFound) as exc:
        Registry.load(root).resolve("nope")
    assert "Known" in str(exc.value)


def test_an_archived_project_stays_resolvable_by_explicit_reference(root):
    manifest, _ = make_project("Hidden", "h@gmail.com", root=root)
    set_archived(manifest.id, True, root=root)
    # Hidden from listings, but naming it explicitly must still work.
    assert Registry.load(root).resolve("Hidden").id == manifest.id


# --------------------------------------------------------------------------- #
# Rename
# --------------------------------------------------------------------------- #
def test_rename_changes_only_the_display_name(root):
    manifest, project_dir = make_project("Before", "r@gmail.com", root=root)
    use_project(manifest.id, root=root)
    before = Registry.load(root)

    entry, previous = rename_project("Before", "After", root=root)
    after = Registry.load(root)

    assert previous == "Before"
    assert entry.name == "After"
    # The three things that must NOT move.
    assert entry.directory == before.get(manifest.id).directory
    assert project_dir.exists()
    assert after.active_project == before.active_project == manifest.id
    assert entry.slug == "before"                     # slug frozen at creation
    assert ProjectManifest.read(project_dir).name == "After"
    assert ProjectManifest.read(project_dir).slug == "before"


def test_rename_does_not_disturb_the_site_name(root):
    manifest, project_dir = make_project("Original", "o@gmail.com", root=root)
    assert manifest.site.name == "Original"
    rename_project("Original", "Renamed", root=root)
    # site.name is the DEPLOYMENT's name, printed in reports. A project rename is
    # not a claim about where the camera is.
    assert ProjectManifest.read(project_dir).site.name == "Original"


# --------------------------------------------------------------------------- #
# Archive
# --------------------------------------------------------------------------- #
def test_archived_projects_leave_the_listing_and_the_single_project_count(root):
    keep, _ = make_project("Keep", "k@gmail.com", root=root)
    hide, _ = make_project("Hide", "h@gmail.com", root=root)

    assert len(Registry.load(root).visible()) == 2
    set_archived("Hide", True, root=root)

    registry = Registry.load(root)
    visible = registry.visible()
    # Stage 5's "exactly one project exists, so use it" rule reads visible().
    # An archived project must not keep the remaining one ambiguous.
    assert [e.id for e in visible] == [keep.id]
    assert len(registry.entries) == 2                  # nothing was deleted

    set_archived("Hide", False, root=root)
    assert len(Registry.load(root).visible()) == 2


def test_archive_state_lives_in_the_registry_not_the_manifest(root):
    manifest, project_dir = make_project("Flagged", "f@gmail.com", root=root)
    before = (project_dir / "project.toml").read_text(encoding="utf-8")
    set_archived("Flagged", True, root=root)

    assert Registry.load(root).get(manifest.id).archived is True
    # Archiving is this installation's view OF a project, not a property of it,
    # so the project's own file is untouched.
    assert (project_dir / "project.toml").read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# use / last_opened_utc
# --------------------------------------------------------------------------- #
def test_use_is_the_only_writer_of_last_opened(root):
    first, _ = make_project("First", "1@gmail.com", root=root)
    second, _ = make_project("Second", "2@gmail.com", root=root)

    # Creation does not stamp it.
    assert Registry.load(root).get(second.id).last_opened_utc is None

    use_project("Second", root=root)
    registry = Registry.load(root)
    assert registry.active_project == second.id
    assert registry.get(second.id).last_opened_utc is not None
    # The one not selected is untouched.
    assert registry.get(first.id).last_opened_utc is None


def test_use_survives_a_failed_last_opened_write(root, monkeypatch):
    """last_opened_utc is best-effort: its only consumer is display order."""
    manifest, _ = make_project("Best Effort", "b@gmail.com", root=root)

    def boom(self, project_id):
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr(Registry, "touch_last_opened", boom)
    entry = use_project("Best Effort", root=root)

    assert entry.id == manifest.id
    assert Registry.load(root).active_project == manifest.id


# --------------------------------------------------------------------------- #
# set-site
# --------------------------------------------------------------------------- #
def test_set_site_requires_both_coordinates(root):
    make_project("Coords", "c@gmail.com", root=root)
    with pytest.raises(ProjectError) as exc:
        set_site("Coords", latitude=54.0, root=root)
    assert "together" in str(exc.value)


@pytest.mark.parametrize("lat,lon", [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)])
def test_set_site_rejects_out_of_range_coordinates(root, lat, lon):
    make_project("Range", "r@gmail.com", root=root)
    with pytest.raises(ProjectError):
        set_site("Range", latitude=lat, longitude=lon, root=root)


def test_set_site_round_trips(root):
    _, project_dir = make_project("Sited", "s@gmail.com", root=root)
    set_site("Sited", name="North Field, Exampleshire",
             latitude=54.9783, longitude=-1.6178, root=root)
    manifest = ProjectManifest.read(project_dir)
    assert manifest.site.name == "North Field, Exampleshire"
    assert manifest.site.latitude == pytest.approx(54.9783)
    assert manifest.site.longitude == pytest.approx(-1.6178)


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #
def _make_linked(root, name, external_db: Path):
    """A project whose database is an external (--mode link) target.

    Nothing writes these keys in Stage 1 -- the importer that does arrives in
    Stage 8 -- so the manifest is written directly here to exercise the reading
    and the delete refusal now, while they are cheap to get right.
    """
    manifest, project_dir = make_project(name, "l@gmail.com", root=root)
    manifest.db_path(project_dir).replace(external_db)
    manifest.storage.database = str(external_db)
    manifest.storage.database_external = True
    manifest.write(project_dir)
    return manifest, project_dir


def test_delete_never_removes_an_external_target(root, tmp_path):
    external = tmp_path / "kept-elsewhere.sqlite"
    manifest, project_dir = _make_linked(root, "Linked", external)
    assert external.exists()

    plan = delete_project("Linked", root=root)

    assert project_dir.exists() is False               # the project went
    assert external.exists() is True                   # the target stayed
    assert plan.external == [external]
    assert Registry.load(root).entries == []


def test_a_linked_project_is_marked_in_the_manifest(root, tmp_path):
    external = tmp_path / "linked.sqlite"
    manifest, project_dir = _make_linked(root, "Marked", external)
    reread = ProjectManifest.read(project_dir)
    assert reread.is_linked is True
    assert reread.external_paths(project_dir) == [external]


def test_an_external_path_must_be_absolute(root):
    manifest, project_dir = make_project("Bogus", "b@gmail.com", root=root)
    manifest.storage.database = "relative.sqlite"
    manifest.storage.database_external = True
    manifest.write(project_dir)
    with pytest.raises(ProjectError) as exc:
        ProjectManifest.read(project_dir).db_path(project_dir)
    assert "absolute" in str(exc.value)


def test_delete_refuses_unaccounted_files_without_force(root):
    _, project_dir = make_project("Cluttered", "c@gmail.com", root=root)
    stray = project_dir / "notes.txt"
    stray.write_text("something the manifest knows nothing about", encoding="utf-8")

    with pytest.raises(ProjectError) as exc:
        delete_project("Cluttered", root=root)
    assert "notes.txt" in str(exc.value)
    assert project_dir.exists()
    assert stray.exists()

    delete_project("Cluttered", root=root, force=True)
    assert not project_dir.exists()


def test_the_removal_plan_reports_what_would_go(root):
    manifest, project_dir = make_project("Planned", "p@gmail.com", root=root)
    plan = removal_plan("Planned", root=root)

    assert plan.project_dir == project_dir
    assert plan.file_count >= 2                        # manifest + database
    assert plan.total_bytes > 0
    assert plan.row_count == 0                         # fresh database
    assert plan.external == []
    assert plan.unaccounted == []


def test_deleting_the_active_project_clears_the_pointer(root):
    manifest, _ = make_project("Active", "a@gmail.com", root=root)
    use_project("Active", root=root)
    assert Registry.load(root).active_project == manifest.id

    delete_project("Active", root=root)
    assert Registry.load(root).active_project is None


# --------------------------------------------------------------------------- #
# Registry durability
# --------------------------------------------------------------------------- #
def test_no_tmp_file_survives_a_successful_write(root):
    make_project("Tidy", "t@gmail.com", root=root)
    assert (root / "registry.toml").exists()
    assert list(root.glob("*.tmp")) == []


def test_a_failure_mid_write_leaves_the_original_registry_intact(root, monkeypatch):
    """A torn registry would make EVERY project unloadable. A lost update costs
    one re-selection. The atomic write buys the first at the price of the second."""
    manifest, _ = make_project("Durable", "d@gmail.com", root=root)
    registry_file = root / "registry.toml"
    original = registry_file.read_bytes()

    registry = Registry.load(root)
    registry.add(RegistryEntry(id="new-id", slug="new", name="New",
                               directory="new-00000000"))

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        registry.save()
    monkeypatch.setattr(os, "replace", real_replace)

    assert registry_file.read_bytes() == original      # untorn, unchanged
    assert list(root.glob("*.tmp")) == []              # no debris left behind
    assert {e.id for e in Registry.load(root).entries} == {manifest.id}


def test_a_display_name_with_toml_metacharacters_round_trips(root):
    awkward = 'He said "north", C:\\field\\1\ttab'
    manifest, project_dir = make_project(awkward, "q@gmail.com", root=root)
    assert ProjectManifest.read(project_dir).name == awkward
    assert Registry.load(root).get(manifest.id).name == awkward


# --------------------------------------------------------------------------- #
# ProjectContext
# --------------------------------------------------------------------------- #
def test_context_exposes_the_projects_own_paths(root):
    manifest, project_dir = make_project("Ctx", "c@gmail.com", root=root)
    entry = Registry.load(root).get(manifest.id)
    ctx = context_for(entry, root)

    assert ctx.id == manifest.id
    assert ctx.dir == project_dir
    assert ctx.db_path == manifest.db_path(project_dir)
    assert ctx.image_store_dir == manifest.image_store_dir(project_dir)
    # Delegated to pipeline.seen_store_path, not reimplemented -- so a project's
    # idempotency file follows its database automatically.
    assert ctx.seen_store_path == ctx.db_path.parent / "seen_message_ids.txt"


def test_context_open_repo_is_a_method_not_a_held_connection(root):
    """sqlite connections are thread-bound (migrations.connect leaves
    check_same_thread=True), so a context that eagerly held a repo could not be
    handed to Stage 6's background ingest thread."""
    manifest, _ = make_project("Threaded", "t@gmail.com", root=root)
    ctx = context_for(Registry.load(root).get(manifest.id), root)

    assert callable(ctx.open_repo)
    a = ctx.open_repo()
    b = ctx.open_repo()
    try:
        assert a is not b                              # a fresh one each call
        assert a.species_counts() == {}
    finally:
        a.close()
        b.close()


def test_context_falls_back_to_the_bundled_alias_table(root):
    manifest, project_dir = make_project("Aliases", "a@gmail.com", root=root)
    ctx = context_for(Registry.load(root).get(manifest.id), root)

    assert ctx.alias_path is None                      # no manifest override
    # ...which resolves to the project's conventional location, not a
    # CWD-relative one, so a project can never pick up whatever table happens to
    # sit beside the process.
    assert ctx.effective_alias_path == project_dir / "species_aliases.yaml"
    assert not ctx.effective_alias_path.exists()

    notices = []
    alias_map = ctx.load_alias_map(notify=notices.append)
    assert alias_map.resolve("Vulpes vulpes") == "Vulpes vulpes"
    assert notices and "bundled example" in notices[0]
    assert ctx.load_target_taxonomy() is not None


def test_a_project_can_carry_its_own_alias_table(root):
    """Dropping a table at the conventional location overrides the bundled one
    for that project alone -- the per-project alias question Phase 1b §3 raised,
    with no new mechanism needed."""
    manifest, project_dir = make_project("Own Table", "o@gmail.com", root=root)
    (project_dir / "species_aliases.yaml").write_text(
        'Vulpes vulpes:\n  raw_tokens: ["VulpesVulpes"]\n  common_names: ["reynard"]\n',
        encoding="utf-8")
    ctx = context_for(Registry.load(root).get(manifest.id), root)

    alias_map = ctx.load_alias_map()
    assert alias_map.resolve("reynard") == "Vulpes vulpes"
    # The bundled table has no "reynard", so this could only have come from the
    # project's own file.
    assert alias_map.display_common_name("Vulpes vulpes") == "reynard"


def test_context_resolves_its_password_as_a_secret(root):
    """Stage 1 asserted this method did NOT exist. Stage 2 adds it, and it must
    return a SecretStr rather than a str -- the value is handed to the login call
    and nowhere else."""
    from pydantic import SecretStr

    manifest, _ = make_project("Creds", "n@gmail.com", root=root)
    ctx = context_for(Registry.load(root).get(manifest.id), root)

    assert ctx.mailbox.credential_ref == f"traptracker-report:{manifest.id}"
    secret = ctx.imap_password(projects_in_scope=1)
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == TEST_PASSWORD.replace(" ", "")
    # Resolved on each call, never cached onto the context.
    assert "imap_password" not in vars(ctx)
    assert TEST_PASSWORD.replace(" ", "") not in repr(ctx)


def test_two_contexts_never_share_storage(root):
    a, _ = make_project("Alpha", "a@gmail.com", root=root)
    b, _ = make_project("Beta", "b@gmail.com", root=root)
    registry = Registry.load(root)
    ca = context_for(registry.get(a.id), root)
    cb = context_for(registry.get(b.id), root)

    assert ca.db_path != cb.db_path
    assert ca.image_store_dir != cb.image_store_dir
    assert ca.seen_store_path != cb.seen_store_path

    # Writing through one is invisible to the other.
    from conftest import make_persisted_event
    import datetime as dt

    repo = ca.open_repo()
    try:
        repo.upsert_event(make_persisted_event(
            "<only-in-alpha@x>", canonical_binomial="Vulpes vulpes",
            event_time=dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)))
    finally:
        repo.close()

    other = cb.open_repo()
    try:
        assert other.species_counts() == {}
    finally:
        other.close()


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
def _cli(root, *args, password=None, **kw):
    """`password=` goes through TTR_IMAP_PASSWORD; there is no flag for it.

    A secret passed on a command line is written to shell history and is
    visible in the process list, so the flag was removed; the env var is
    what a headless user uses, and what these tests use.
    """
    env = {ppaths.ROOT_ENV_VAR: str(root)}
    if password is not None:
        env["TTR_IMAP_PASSWORD"] = password
    return runner.invoke(app, ["project", *args], env=env, **kw)


@pytest.fixture
def offline_cli(monkeypatch):
    """Point the CLI's connection test at the fake mailbox.

    `check_connection` looks `default_mailbox_factory` up on the module at call
    time, so replacing it here covers every CLI path without threading a
    parameter through the command handlers.
    """
    import ttr.projects.connection as connection

    monkeypatch.setattr(connection, "default_mailbox_factory",
                        mailbox_factory(messages=12))
    return connection


def test_cli_create_list_and_use(root, offline_cli):
    assert _cli(root, "create", "--name", "CLI One", "--email", "one@gmail.com",
                password=TEST_PASSWORD).exit_code == 0
    assert _cli(root, "create", "--name", "CLI Two", "--email", "two@gmail.com",
                password=TEST_PASSWORD).exit_code == 0

    listing = _cli(root, "list")
    assert listing.exit_code == 0
    assert "CLI One" in listing.output and "CLI Two" in listing.output
    assert "active" in listing.output

    assert _cli(root, "use", "CLI Two").exit_code == 0
    assert "CLI Two" in _cli(root, "list").output


def test_cli_create_refuses_outlook_with_the_reason(root):
    result = _cli(root, "create", "--name", "MS", "--email", "me@outlook.com")
    assert result.exit_code == 2
    assert "16 September 2024" in result.output


def test_cli_create_refuses_an_untested_provider(root):
    result = _cli(root, "create", "--name", "Other", "--email", "me@fastmail.com")
    assert result.exit_code == 2
    assert "fastmail.com" in result.output
    assert "project.toml" in result.output


def test_cli_delete_is_gated_by_typing_the_name(root):
    make_project("Gated", "g@gmail.com", root=root)

    wrong = _cli(root, "delete", "Gated", input="not the name\n")
    assert wrong.exit_code == 1
    assert "did not match" in wrong.output
    assert Registry.load(root).entries                 # still there

    right = _cli(root, "delete", "Gated", input="Gated\n")
    assert right.exit_code == 0
    assert Registry.load(root).entries == []


def test_cli_delete_dry_run_changes_nothing(root):
    make_project("Rehearsal", "r@gmail.com", root=root)
    result = _cli(root, "delete", "Rehearsal", "--dry-run")
    assert result.exit_code == 0
    assert "dry run" in result.output
    assert Registry.load(root).entries


def test_cli_delete_reports_an_external_target_as_left_in_place(root, tmp_path):
    external = tmp_path / "external.sqlite"
    _make_linked(root, "CliLinked", external)
    result = _cli(root, "delete", "CliLinked", "--yes")
    assert result.exit_code == 0
    assert "left in place (external):" in result.output
    assert external.exists()


def test_cli_ambiguous_name_refuses_and_lists_ids(root):
    a, _ = make_project("Dup", "a@gmail.com", root=root)
    b, _ = make_project("Dup", "b@gmail.com", root=root)
    result = _cli(root, "use", "Dup")
    assert result.exit_code == 2
    assert "ambiguous" in result.output
    assert a.id in result.output and b.id in result.output


def test_cli_list_is_empty_and_calm_before_any_project_exists(root):
    result = _cli(root, "list")
    assert result.exit_code == 0
    assert "no projects" in result.output


def test_cli_rename_tells_the_user_to_key_scripts_on_the_id(root):
    manifest, _ = make_project("Scripted", "s@gmail.com", root=root)
    result = _cli(root, "rename", "Scripted", "Rescripted")
    assert result.exit_code == 0
    assert manifest.id in result.output


def test_cli_path_prints_the_directory(root):
    manifest, project_dir = make_project("Pathed", "p@gmail.com", root=root)
    result = _cli(root, "path", "Pathed")
    assert result.exit_code == 0
    assert result.output.strip() == str(project_dir)
