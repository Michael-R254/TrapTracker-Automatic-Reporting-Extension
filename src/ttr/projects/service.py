"""Project lifecycle operations, with no CLI or presentation in them.

Everything here works on a projects root passed in, so the whole layer is
testable against a ``tmp_path`` without touching the user's real projects.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..logging import get_logger
from .context import ProjectContext
from .errors import ProjectError
from .manifest import (Mailbox, ProjectManifest, Site, Storage, utc_now_iso)
from .paths import (DEFAULT_DATABASE, DEFAULT_IMAGE_STORE, MANIFEST_NAME,
                    directory_name, new_project_id, projects_dir, projects_root,
                    slugify)
from .providers import preset_for
from .registry import Registry, RegistryEntry

logger = get_logger(__name__)

#: Files a project directory is allowed to contain. Anything else makes `delete`
#: refuse without --force: an unexpected file means the manifest's [storage] does
#: not describe what is actually on disk, and removing a directory we cannot
#: account for is exactly the mistake worth being slow about.
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def _mailbox_defaults() -> Mailbox:
    """A new project's mailbox defaults, which are ``Mailbox``'s own.

    This used to read them out of ``Settings.model_fields`` so the two could not
    drift. Stage 3 of the multi-project migration moved the whole ``imap_*``
    family off ``Settings`` and onto the project manifest, at which point there
    was nothing left to read and the lookup could only ever raise. It did, on
    every single `ttr project create`, for months - swallowed by a bare `except`
    that logged `KeyError('imap_folder')` at WARNING and fell back to exactly the
    values it had been trying to fetch. Correct output, alarming noise, and the
    first thing a new user ever saw from this tool.

    ``Mailbox`` is now the single place those defaults live, so there is no
    second source for them to drift from.
    """
    return Mailbox()


# --------------------------------------------------------------------- create
def create_project(name: str, address: Optional[str] = None,
                   password: Optional[str] = None, *,
                   root: Optional[Path] = None,
                   skip_connection_test: bool = False,
                   mailbox_factory=None) -> tuple[ProjectManifest, Path]:
    """Create a project - directory, manifest, credential, image store, database.

    Order matters and is the point of this function: **validate, then verify,
    then write.** The domain is checked, the mailbox is actually connected to,
    and only then does anything touch the disk or the credential store. A failure
    at any of the first two steps leaves no directory, no registry entry and no
    keyring entry - nothing to clean up by hand.

    ``password`` may be omitted only with ``skip_connection_test``, for offline
    setup; the caller is expected to warn that the mailbox is unverified.
    """
    name = (name or "").strip()
    if not name:
        raise ProjectError("a project needs a name")

    # --- 1. validate the address before anything else happens ---------------
    #
    # `address=None` makes a MAILBOX-LESS project: one that can report on rows it
    # already holds but can never ingest. That is what a published example is —
    # the extract shipped with this repository has no inbox behind it, and giving
    # it a plausible Gmail address so it could be created would be inventing a
    # mailbox that does not exist and offering to poll it.
    #
    # Everything downstream already copes: `credential_source()` returns None, the
    # ingest page disables its fetch controls and names `ttr project set-password`,
    # and the preflight prints "not configured" for the host. Adding a mailbox
    # later is `ttr project set-password`, the same path a real project uses.
    if address is None:
        preset = None
        skip_connection_test = True
    else:
        preset = preset_for(address)               # refuses non-Gmail, loudly
        address = address.strip()

    if not skip_connection_test and not password:
        raise ProjectError(
            "a mailbox password is required - pass --skip-connection-test to "
            "create the project without verifying the mailbox")

    # A password with nowhere to go is refused HERE, before the mailbox is
    # contacted. Refusing after the connection test rolled the project back, and
    # the message then named the environment variable of a project that no longer
    # existed.
    if password and not skip_connection_test:
        from . import credentials

        if not credentials.backend_available():
            raise credentials.NoCredentialBackend(_CREATE_WITHOUT_BACKEND)

    project_id = new_project_id()

    # --- 2. verify the mailbox BEFORE creating anything ----------------------
    # Deliberately ahead of the directory: a project that cannot reach its
    # mailbox is not a project, and a half-made one the user must delete by hand
    # is a worse outcome than a refusal.
    if not skip_connection_test:
        from pydantic import SecretStr

        from .connection import check_connection
        from .credentials import normalise

        result = check_connection(
            host=preset.imap_host, user=address, folder=preset.imap_folder,
            use_ssl=preset.imap_use_ssl,
            password=SecretStr(normalise(password)),
            credential_source="(entered)", mailbox_factory=mailbox_factory)
        if not result.ok:
            raise ProjectError(result.message)
        logger.info("project_create_connection_ok",
                    extra={"messages": result.message_count})

    root = root or projects_root()
    registry = Registry.load(root)
    slug = slugify(name)
    dirname = directory_name(slug, project_id)
    project_dir = projects_dir(root) / dirname
    if project_dir.exists():
        raise ProjectError(f"project directory already exists: {project_dir}")

    mailbox = _mailbox_defaults()
    if preset is not None:
        mailbox.address = address
        mailbox.imap_user = address
        mailbox.imap_host = preset.imap_host
        mailbox.imap_folder = preset.imap_folder
        mailbox.imap_use_ssl = preset.imap_use_ssl
    # else: a mailbox-less project. `Mailbox()`'s own defaults are empty
    # strings, which is what "no inbox" looks like on disk.

    manifest = ProjectManifest(
        id=project_id, name=name, slug=slug, created_utc=utc_now_iso(),
        mailbox=mailbox,
        # The site defaults to the project's own name so a report has something
        # honest to print. Coordinates stay unset: `backfill-weather` refuses
        # without them, which is the correct loud failure.
        site=Site(name=name),
        storage=Storage(database=DEFAULT_DATABASE, image_store=DEFAULT_IMAGE_STORE),
    )

    # --- 3. write. Anything failing from here rolls the whole thing back. ----
    stored_credential = False
    project_dir.mkdir(parents=True)
    try:
        (project_dir / DEFAULT_IMAGE_STORE).mkdir()
        if password and not skip_connection_test:
            from . import credentials

            manifest.mailbox.credential_ref = credentials.store(project_id, password)
            stored_credential = True
        manifest.write(project_dir)
        # The real schema, from the real migration path — not a hand-rolled
        # CREATE TABLE that could drift from it.
        from ..storage.migrations import connect

        connect(manifest.db_path(project_dir)).close()
    except BaseException:
        shutil.rmtree(project_dir, ignore_errors=True)
        if stored_credential:
            # The credential is part of the project. A rolled-back creation must
            # not leave one behind under an id nothing references.
            from . import credentials

            try:
                credentials.delete(project_id)
            except Exception as exc:
                logger.warning("project_create_rollback_left_credential",
                               extra={"project": project_id, "error": repr(exc)})
        raise

    registry.add(RegistryEntry(id=project_id, slug=slug, name=name,
                               directory=dirname))
    if registry.active_project is None:
        registry.active_project = project_id
    registry.save()

    logger.info("project_created",
                extra={"id": project_id, "slug": slug, "dir": str(project_dir)})
    return manifest, project_dir


# ------------------------------------------------------------------ resolution
def project_dir_for(entry: RegistryEntry, root: Optional[Path] = None) -> Path:
    return projects_dir(root) / entry.directory


def context_for(entry: RegistryEntry, root: Optional[Path] = None,
                *, app=None) -> ProjectContext:
    return ProjectContext.load(project_dir_for(entry, root), app=app)


# ---------------------------------------------------------------------- edits
def rename_project(term: str, new_name: str, *, root: Optional[Path] = None
                   ) -> tuple[RegistryEntry, str]:
    """Change the DISPLAY NAME only.

    The directory and slug are frozen at creation and do not move; the registry
    and every other reference key on the id, so ``active_project`` is unaffected
    and no path changes. Returns the updated entry and the previous name.
    """
    new_name = (new_name or "").strip()
    if not new_name:
        raise ProjectError("a project needs a name")
    root = root or projects_root()
    registry = Registry.load(root)
    entry = registry.resolve(term)
    previous = entry.name

    project_dir = project_dir_for(entry, root)
    manifest = ProjectManifest.read(project_dir)
    manifest.name = new_name
    manifest.write(project_dir)

    updated = registry.update(entry.id, name=new_name)
    registry.save()
    logger.info("project_renamed",
                extra={"id": entry.id, "from": previous, "to": new_name})
    return updated, previous


def set_archived(term: str, archived: bool, *, root: Optional[Path] = None
                 ) -> RegistryEntry:
    """Archive state lives in the REGISTRY only - see its module docstring."""
    root = root or projects_root()
    registry = Registry.load(root)
    entry = registry.resolve(term)
    updated = registry.update(entry.id, archived=archived)
    registry.save()
    logger.info("project_archived" if archived else "project_unarchived",
                extra={"id": entry.id})
    return updated


def use_project(term: str, *, root: Optional[Path] = None) -> RegistryEntry:
    """Set the active project. The ONLY writer of ``last_opened_utc``."""
    root = root or projects_root()
    registry = Registry.load(root)
    entry = registry.resolve(term)
    registry.active_project = entry.id
    try:
        registry.touch_last_opened(entry.id)
    except Exception as exc:
        # Best-effort by design: its only consumer is display order.
        logger.warning("project_last_opened_not_recorded",
                       extra={"id": entry.id, "error": repr(exc)})
    registry.save()
    return registry.get(entry.id)


def set_password(term: str, password: str, *, root: Optional[Path] = None,
                 skip_connection_test: bool = False, mailbox_factory=None):
    """Verify a mailbox password, then store it. Also the rotation path.

    The test runs FIRST and nothing is written on failure - storing a credential
    that has just been shown not to work would mean the next command fails with a
    stale secret, which is exactly the confusion this stage exists to remove.
    """
    from pydantic import SecretStr

    from . import credentials
    from .connection import ConnectionResult, check_connection

    root = root or projects_root()
    registry = Registry.load(root)
    entry = registry.resolve(term)
    project_dir = project_dir_for(entry, root)
    manifest = ProjectManifest.read(project_dir)

    cleaned = credentials.normalise(password)
    if not cleaned:
        raise ProjectError("an empty password cannot be stored")
    # Before the connection test: with nowhere to store the password, verifying
    # it against the mailbox would prove something that is then thrown away.
    credentials._require_backend(entry.id)

    result: Optional[ConnectionResult] = None
    if not skip_connection_test:
        result = check_connection(
            host=manifest.mailbox.imap_host, user=manifest.mailbox.imap_user,
            folder=manifest.mailbox.imap_folder,
            use_ssl=manifest.mailbox.imap_use_ssl,
            password=SecretStr(cleaned), credential_source="(entered)",
            mailbox_factory=mailbox_factory)
        if not result.ok:
            raise ProjectError(result.message)

    manifest.mailbox.credential_ref = credentials.store(entry.id, cleaned)
    manifest.write(project_dir)
    return manifest, result


def set_site(term: str, *, name: Optional[str] = None,
             latitude: Optional[float] = None, longitude: Optional[float] = None,
             root: Optional[Path] = None) -> ProjectManifest:
    """Set the site name and/or coordinates.

    Latitude and longitude are set together or not at all: a half-set coordinate
    pair is not a location, and weather enrichment would have to either guess the
    other half or fail confusingly.
    """
    if (latitude is None) != (longitude is None):
        raise ProjectError(
            "latitude and longitude must be given together - a single "
            "coordinate is not a location")
    if latitude is not None and not (-90 <= latitude <= 90):
        raise ProjectError(f"latitude out of range: {latitude}")
    if longitude is not None and not (-180 <= longitude <= 180):
        raise ProjectError(f"longitude out of range: {longitude}")

    root = root or projects_root()
    registry = Registry.load(root)
    entry = registry.resolve(term)
    project_dir = project_dir_for(entry, root)
    manifest = ProjectManifest.read(project_dir)
    if name is not None:
        manifest.site.name = name.strip() or None
    if latitude is not None:
        manifest.site.latitude = latitude
        manifest.site.longitude = longitude
    manifest.write(project_dir)
    return manifest


# --------------------------------------------------------------------- delete
@dataclass
class RemovalPlan:
    """Everything ``delete`` would touch, computed before anything is asked."""

    entry: RegistryEntry
    project_dir: Path
    total_bytes: int = 0
    file_count: int = 0
    row_count: Optional[int] = None
    paths: list[tuple[Path, int]] = field(default_factory=list)
    external: list[Path] = field(default_factory=list)
    unaccounted: list[Path] = field(default_factory=list)
    #: Whether a stored credential was actually removed, and why not if it was
    #: not. Populated by `delete_project`; None before it runs.
    credential_removed: Optional[bool] = None
    credential_error: Optional[str] = None
    #: Set when the database this project points at is stamped for a DIFFERENT
    #: project. The row count is then withheld rather than reported: presenting
    #: another deployment's events as this project's is the confusion the
    #: ownership stamp exists to end, and a deletion plan is exactly the wrong
    #: place to be reassured by a number that is not yours.
    foreign_database_owner: Optional[str] = None


_CREATE_WITHOUT_BACKEND = (
    "No OS credential store is available on this machine, so the password cannot\n"
    "be stored with the new project, and there is deliberately no file-based\n"
    "fallback. Nothing was created, and the mailbox was not contacted.\n"
    "\n"
    "Create the project without a password instead: leave the password empty in\n"
    "the web form, or pass --skip-connection-test to `ttr project create`. Then\n"
    "supply the password through the environment as TTR_IMAP_PASSWORD__<ID>,\n"
    "using the id the project is given, and check it with\n"
    "    ttr project test-connection <project>"
)


def _accounted_names(manifest: ProjectManifest) -> set[str]:
    from .lock import LOCK_NAME

    # The ingest lock file is permanent by design (`lock.py`), so any project
    # that has ever ingested has one. It is ours, not an unexpected file.
    names = {MANIFEST_NAME, LOCK_NAME}
    if not manifest.storage.database_external:
        db = manifest.storage.database
        names.add(db)
        names.update(db + s for s in _SQLITE_SIDECARS)
    if not manifest.storage.image_store_external:
        names.add(manifest.storage.image_store)
    # Derived from db_path, so it is ours whenever the database is.
    if not manifest.storage.database_external:
        names.add("seen_message_ids.txt")
    return names


def removal_plan(term: str, *, root: Optional[Path] = None) -> RemovalPlan:
    root = root or projects_root()
    registry = Registry.load(root)
    entry = registry.resolve(term)
    project_dir = project_dir_for(entry, root)
    manifest = ProjectManifest.read(project_dir)
    plan = RemovalPlan(entry=entry, project_dir=project_dir)

    plan.external = manifest.external_paths(project_dir)

    accounted = _accounted_names(manifest)
    for child in sorted(project_dir.iterdir()):
        if child.name not in accounted:
            plan.unaccounted.append(child)

    for path in sorted(project_dir.rglob("*")):
        if path.is_file():
            size = path.stat().st_size
            plan.total_bytes += size
            plan.file_count += 1
            plan.paths.append((path, size))

    db = manifest.db_path(project_dir)
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                from ..storage.migrations import read_owner

                owner = read_owner(conn)
                if owner is not None and owner != entry.id:
                    # Read-only, so nothing is refused here — but nothing is
                    # reported either. Answering "N detection events" from a
                    # database belonging to another project would make a wrong
                    # manifest look right at exactly the wrong moment.
                    plan.foreign_database_owner = owner
                else:
                    plan.row_count = conn.execute(
                        "SELECT COUNT(*) FROM detection_events").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            plan.row_count = None
    return plan


def delete_project(term: str, *, root: Optional[Path] = None,
                   force: bool = False) -> RemovalPlan:
    """Remove a project's directory and registry entry.

    An external (``--mode link``) target is NEVER removed: the project does not
    own it. The project goes; the linked database or image store stays, and the
    caller reports it.

    Refuses when the directory holds files the manifest does not account for,
    unless ``force`` - an unexpected file means [storage] no longer describes
    what is on disk, and that is the wrong moment for a recursive delete.
    """
    root = root or projects_root()
    plan = removal_plan(term, root=root)

    # Never delete a project out from under a live ingest: the run would keep
    # writing into a directory that no longer exists, and its images and rows
    # would land nowhere recoverable.
    from .lock import describe, holder

    live = holder(plan.project_dir)
    if live is not None:
        raise ProjectError(
            f"{describe(live)}.\n"
            f"Stop it before deleting this project.")

    if plan.unaccounted and not force:
        listing = "\n".join(f"  {p.name}" for p in plan.unaccounted)
        raise ProjectError(
            f"{plan.project_dir} contains files this project's manifest does not "
            f"account for:\n{listing}\n"
            f"Refusing to delete. Move them out, or pass --force if they really "
            f"are disposable.")

    registry = Registry.load(root)
    shutil.rmtree(plan.project_dir)
    registry.remove(plan.entry.id)
    registry.save()

    # The credential belongs to the project, so it goes with it. An absent entry
    # is not an error — a project may never have had one stored. A FAILED removal
    # is reported rather than swallowed: a leftover credential is not dangerous,
    # but the user is entitled to know it is still in their credential store.
    from . import credentials

    try:
        plan.credential_removed = credentials.delete(plan.entry.id)
    except Exception as exc:
        plan.credential_error = str(exc)
        logger.warning("project_credential_not_removed",
                       extra={"id": plan.entry.id, "error": repr(exc)})

    logger.info("project_deleted",
                extra={"id": plan.entry.id, "dir": str(plan.project_dir),
                       "external_kept": len(plan.external),
                       "credential_removed": plan.credential_removed})
    return plan
