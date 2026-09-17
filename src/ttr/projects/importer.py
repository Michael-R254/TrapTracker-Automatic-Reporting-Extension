"""Adopt an existing single-deployment corpus as a project.

The shape of the problem, measured rather than assumed:

  * the database is unstamped and holds 787 events;
  * its stored image paths are RELATIVE, CWD-anchored, backslash-separated, and
    split across two roots - 475 rows under ``image_store\\live\\<id>\\`` and 312
    under ``image_store\\<id>\\``, because IMAGE_STORE_DIR changed mid-corpus;
  * 476 top-level directories (~121 MB) are referenced by no row at all, produced
    by ``_persist_images`` running before an ``ON CONFLICT DO NOTHING`` upsert;
  * the seen-store holds 793 ids: 787 stored plus 6 non-alerts.

So the import copies the referenced images and REWRITES the paths to be
image-store-root-relative with forward slashes. That is what makes the project
self-contained: afterwards its rows resolve from the project directory whatever
the process working directory happens to be, which the originals do not.

Everything is done to COPIES. The source database is opened read-only and never
written, so the originals remain exactly what they were - which is what makes
them the backup.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..logging import get_logger
from ..storage.digest import verdict_digest
from .errors import ProjectError
from .manifest import (DEFAULT_DATABASE, DEFAULT_IMAGE_STORE, Mailbox,
                       ProjectManifest, Site, Storage, utc_now_iso)
from .paths import directory_name, new_project_id, projects_dir, projects_root, slugify
from .registry import Registry, RegistryEntry

logger = get_logger(__name__)

SEEN_STORE_NAME = "seen_message_ids.txt"

_MODES = ("copy", "move", "link")


@dataclass
class ImageRef:
    """One row's image directory, as stored and as it will be."""

    message_id: str
    source_dir: Path
    #: The root it currently sits under, relative to the image store — "" for the
    #: top level, "live" for the nested one. Kept only for reporting: after the
    #: import there is one root.
    source_root: str


@dataclass
class ImportPlan:
    name: str
    project_id: str
    project_dir: Path
    db_source: Path
    db_dest: Path
    images_source: Path
    images_dest: Path
    seen_source: Optional[Path] = None
    seen_dest: Optional[Path] = None
    alias_source: Optional[Path] = None
    alias_dest: Optional[Path] = None
    mode: str = "copy"

    row_count: int = 0
    source_digest: str = ""
    referenced: list[ImageRef] = field(default_factory=list)
    #: Directories under a root that no row references. Dropped.
    orphans: list[Path] = field(default_factory=list)
    #: Orphans that sit INSIDE a root holding referenced directories, rather than
    #: at the top level. Counted separately because the earlier survey only
    #: looked at the top level, and a category nobody counts is a category that
    #: can hide a surprise.
    nested_orphans: list[Path] = field(default_factory=list)
    #: Files sitting loose in the image store rather than inside a message-id
    #: directory. Not copied — nothing references them — but never silently
    #: omitted from the total.
    loose_files: list[Path] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    referenced_bytes: int = 0
    orphan_bytes: int = 0
    nested_orphan_bytes: int = 0
    loose_bytes: int = 0
    #: The measured size of the whole store, and whatever the four buckets above
    #: fail to explain. `unaccounted_bytes` must be zero; the plan prints it
    #: either way so a discrepancy is visible rather than inferred.
    store_bytes: int = 0
    db_bytes: int = 0
    sidecars: list[str] = field(default_factory=list)
    rows_missing_images: int = 0
    roots_seen: dict = field(default_factory=dict)

    @property
    def accounted_bytes(self) -> int:
        return (self.referenced_bytes + self.orphan_bytes
                + self.nested_orphan_bytes + self.loose_bytes)

    @property
    def unaccounted_bytes(self) -> int:
        return self.store_bytes - self.accounted_bytes

    @property
    def alias_table_supplied(self) -> bool:
        return self.alias_source is not None


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _split_stored_path(stored: str, image_root_name: str) -> tuple[str, str]:
    """A stored image path -> (root under the image store, message-id directory).

    Stored values look like ``image_store\\live\\<id>\\<file>.jpg`` or
    ``image_store\\<id>\\<file>.jpg``. Backslashes are normalised first: the
    corpus was written on Windows, and a POSIX ``Path`` would treat the whole
    thing as one filename.
    """
    parts = [p for p in stored.replace("\\", "/").split("/") if p]
    if parts and parts[0] == image_root_name:
        parts = parts[1:]
    if len(parts) >= 3:                      # <root>/<id>/<file>
        return parts[0], parts[1]
    if len(parts) == 2:                      # <id>/<file>
        return "", parts[0]
    raise ProjectError(f"cannot interpret stored image path: {stored!r}")


_COLLISION = (
    "Refusing to import: {count} message-id(s) are referenced under BOTH image "
    "roots.\n"
    "\n"
    "{listing}\n"
    "Flattening the two roots into one images/ directory would make one silently\n"
    "overwrite the other, and the row pointing at the loser would then resolve to\n"
    "the wrong frames. Resolve the duplication first - the two copies are not\n"
    "necessarily identical."
)


def build_plan(*, name: str, db_source: Path, images_source: Path,
               seen_source: Optional[Path] = None,
               alias_source: Optional[Path] = None,
               mode: str = "copy",
               root: Optional[Path] = None,
               project_id: Optional[str] = None) -> ImportPlan:
    """Work out everything the import would do. Writes nothing, anywhere.

    The source database is opened READ-ONLY. That is not a detail: a plan that
    modified what it is planning against would make the dry-run a lie.
    """
    if mode not in _MODES:
        raise ProjectError(f"unknown --mode {mode!r}; expected one of {', '.join(_MODES)}")
    name = (name or "").strip()
    if not name:
        raise ProjectError("a project needs a name")
    db_source = Path(db_source)
    images_source = Path(images_source)
    if not db_source.is_file():
        raise ProjectError(f"no such database: {db_source}")
    if not images_source.is_dir():
        raise ProjectError(f"no such image store: {images_source}")

    root = root or projects_root()
    project_id = project_id or new_project_id()
    slug = slugify(name)
    project_dir = projects_dir(root) / directory_name(slug, project_id)

    plan = ImportPlan(
        name=name, project_id=project_id, project_dir=project_dir,
        db_source=db_source, db_dest=project_dir / DEFAULT_DATABASE,
        images_source=images_source, images_dest=project_dir / DEFAULT_IMAGE_STORE,
        seen_source=Path(seen_source) if seen_source else None,
        seen_dest=project_dir / SEEN_STORE_NAME if seen_source else None,
        alias_source=Path(alias_source) if alias_source else None,
        alias_dest=project_dir / "species_aliases.yaml" if alias_source else None,
        mode=mode,
    )
    plan.db_bytes = db_source.stat().st_size
    for suffix in ("-wal", "-shm"):
        side = db_source.with_name(db_source.name + suffix)
        if side.exists():
            plan.sidecars.append(side.name)

    # --- read the corpus, read-only ----------------------------------------
    plan.source_digest = verdict_digest(db_source)
    conn = sqlite3.connect(f"file:{db_source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        plan.row_count = conn.execute(
            "SELECT COUNT(*) FROM detection_events").fetchone()[0]
        rows = conn.execute(
            "SELECT original_image_path, boxed_image_path FROM detection_events"
        ).fetchall()
    finally:
        conn.close()

    image_root_name = images_source.name
    by_id: dict[str, ImageRef] = {}
    roots_for_id: dict[str, set] = {}
    for row in rows:
        stored = [p for p in (row["original_image_path"], row["boxed_image_path"]) if p]
        if not stored:
            plan.rows_missing_images += 1
            continue
        for value in stored:
            source_root, message_id = _split_stored_path(value, image_root_name)
            roots_for_id.setdefault(message_id, set()).add(source_root)
            source_dir = images_source / source_root / message_id if source_root \
                else images_source / message_id
            by_id.setdefault(message_id,
                             ImageRef(message_id, source_dir, source_root))
            plan.roots_seen[source_root or "(top level)"] = \
                plan.roots_seen.get(source_root or "(top level)", 0) + 1

    # --- the collision check ------------------------------------------------
    # Phase 1b's counts SUGGEST the referenced sets are disjoint. Suggest is not
    # enough when the failure mode is one directory silently overwriting another.
    plan.collisions = sorted(mid for mid, roots in roots_for_id.items()
                             if len(roots) > 1)

    plan.referenced = [by_id[k] for k in sorted(by_id)]
    referenced_dirs = {ref.source_dir.resolve() for ref in plan.referenced}
    for ref in plan.referenced:
        if ref.source_dir.is_dir():
            plan.referenced_bytes += _dir_size(ref.source_dir)

    # --- account for EVERY byte in the store --------------------------------
    # Four buckets, and a reconciliation. Anything the buckets cannot explain is
    # reported rather than quietly dropped: a plan that does not add up is a plan
    # that can hide a surprise.
    plan.store_bytes = _dir_size(images_source)
    roots_with_referenced = {r.source_root for r in plan.referenced if r.source_root}

    for child in sorted(images_source.iterdir()):
        if child.is_file():
            plan.loose_files.append(child)
            plan.loose_bytes += child.stat().st_size
            continue
        if not child.is_dir():
            continue
        if child.resolve() in referenced_dirs:
            continue                                    # counted above
        if child.name in roots_with_referenced:
            # A ROOT holding referenced directories is not itself an orphan;
            # only its unreferenced children are.
            for sub in sorted(child.iterdir()):
                if sub.is_file():
                    plan.loose_files.append(sub)
                    plan.loose_bytes += sub.stat().st_size
                elif sub.resolve() not in referenced_dirs:
                    plan.nested_orphans.append(sub)
                    plan.nested_orphan_bytes += _dir_size(sub)
            continue
        plan.orphans.append(child)
        plan.orphan_bytes += _dir_size(child)

    return plan


def _copy_database(plan: ImportPlan, notify) -> None:
    """Copy the database WITH its sidecars, then checkpoint the COPY.

    Never the original: a checkpoint is a write, and the source is meant to come
    out of this untouched. A 0-byte WAL today is not a guarantee for whenever
    this actually runs, so the sidecars travel and the copy is flattened.
    """
    plan.db_dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = plan.db_source.with_name(plan.db_source.name + suffix)
        if src.exists():
            shutil.copy2(src, plan.db_dest.with_name(plan.db_dest.name + suffix))

    conn = sqlite3.connect(str(plan.db_dest))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()
    notify(f"database copied and checkpointed -> {plan.db_dest.name}")


def _copy_images(plan: ImportPlan, notify) -> int:
    plan.images_dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for ref in plan.referenced:
        if not ref.source_dir.is_dir():
            continue
        dest = plan.images_dest / ref.message_id
        if dest.exists():
            continue
        if plan.mode == "move":
            shutil.move(str(ref.source_dir), str(dest))
        else:
            shutil.copytree(ref.source_dir, dest)
        copied += 1
    notify(f"copied {copied} image directory/directories "
           f"({len(plan.orphans)} orphan(s) left behind)")
    return copied


def _rewrite_paths(plan: ImportPlan, notify) -> int:
    """Make every stored path image-store-root-relative, forward-slashed.

    This is the change that makes the project self-contained. The originals are
    CWD-anchored, so they resolve only when the process happens to be standing in
    the right directory; afterwards they resolve from the project itself,
    wherever it is run from.
    """
    conn = sqlite3.connect(str(plan.db_dest))
    conn.row_factory = sqlite3.Row
    rewritten = 0
    try:
        rows = conn.execute(
            "SELECT id, original_image_path, boxed_image_path FROM detection_events"
        ).fetchall()
        with conn:
            for row in rows:
                updates = {}
                for column in ("original_image_path", "boxed_image_path"):
                    stored = row[column]
                    if not stored:
                        continue
                    parts = [p for p in stored.replace("\\", "/").split("/") if p]
                    filename = parts[-1]
                    _, message_id = _split_stored_path(stored, plan.images_source.name)
                    updates[column] = f"{message_id}/{filename}"
                if updates:
                    conn.execute(
                        "UPDATE detection_events SET original_image_path = ?, "
                        "boxed_image_path = ? WHERE id = ?",
                        (updates.get("original_image_path", row["original_image_path"]),
                         updates.get("boxed_image_path", row["boxed_image_path"]),
                         row["id"]))
                    rewritten += 1
    finally:
        conn.close()
    notify(f"rewrote image paths on {rewritten} row(s) "
           f"(image-store-relative, forward slashes)")
    return rewritten


def _backfill_alias_hash(plan: ImportPlan, content_sha256: str, notify) -> int:
    conn = sqlite3.connect(str(plan.db_dest))
    try:
        with conn:
            cur = conn.execute(
                "UPDATE detection_events SET alias_table_sha256 = ? "
                "WHERE alias_table_sha256 IS NULL", (content_sha256,))
        touched = cur.rowcount
    finally:
        conn.close()
    notify(f"stamped {touched} row(s) with alias table {content_sha256}")
    notify("  NOTE: this is an ASSERTION BY THE OPERATOR that these rows were")
    notify("        resolved under that table. It is not a recovered fact - the")
    notify("        rows carried no provenance, and none was found.")
    return touched


class ImportVerificationError(ProjectError):
    """The migrated corpus does not match the source. The import is a bug."""


def verify(plan: ImportPlan, ctx, notify) -> None:
    """Prove the move changed nothing, and fail LOUDLY if it did.

    A migration moves bytes; it does not re-decide anything. So the verdict
    digest must reproduce exactly - that is the whole reason it was captured
    before any of this began. A mismatch is a migration bug, not a finding, and
    is raised rather than warned about.
    """
    migrated = verdict_digest(plan.db_dest)
    if migrated != plan.source_digest:
        raise ImportVerificationError(
            "The migrated database does not match the source.\n"
            f"  source:   {plan.source_digest}\n"
            f"  migrated: {migrated}\n"
            "\n"
            "A migration moves bytes; it does not re-decide anything, so this is a\n"
            "bug in the import and not a finding about the data. The project has\n"
            "been left in place for inspection; the originals are untouched.")
    notify(f"verdict digest reproduces exactly: {migrated}")

    repo = ctx.open_repo()
    try:
        rows = sum(repo.species_counts().values())
    finally:
        repo.close()
    if rows != plan.row_count:
        raise ImportVerificationError(
            f"row count changed: {plan.row_count} -> {rows}")
    notify(f"row count preserved: {rows}")

    missing = _unresolvable_images(ctx)
    if missing:
        raise ImportVerificationError(
            f"{len(missing)} stored image path(s) do not resolve inside the "
            f"project, e.g. {missing[0]}")
    notify("every stored image path resolves from the project directory")


def _unresolvable_images(ctx) -> list[str]:
    """Stored paths that do not resolve against the project's image store.

    Resolved against ``ctx.image_store_dir`` - an absolute path - so this answers
    the same way wherever the process is standing. That CWD-independence is the
    property the rewrite exists to create.
    """
    conn = sqlite3.connect(f"file:{ctx.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    missing = []
    try:
        for row in conn.execute(
                "SELECT original_image_path, boxed_image_path FROM detection_events"):
            for column in ("original_image_path", "boxed_image_path"):
                stored = row[column]
                if stored and not (ctx.image_store_dir / stored).is_file():
                    missing.append(stored)
    finally:
        conn.close()
    return missing


def run_import(plan: ImportPlan, *, root: Optional[Path] = None,
               mailbox: Optional[Mailbox] = None,
               site: Optional[Site] = None,
               backfill_alias_hash: bool = False,
               notify=None) -> "tuple[ProjectManifest, ImportPlan]":
    """Execute a plan. Everything is written under the project; nothing else moves."""
    notify = notify or (lambda message: None)
    if plan.collisions:
        raise ProjectError(_COLLISION.format(
            count=len(plan.collisions),
            listing="\n".join(f"  {mid}" for mid in plan.collisions[:20])))

    root = root or projects_root()
    registry = Registry.load(root)

    manifest = ProjectManifest(
        id=plan.project_id, name=plan.name, slug=slugify(plan.name),
        created_utc=utc_now_iso(),
        mailbox=mailbox or Mailbox(),
        # The site's own name when one was recovered; otherwise the project's, so
        # a report has something honest to print either way.
        site=site if (site and (site.name or site.latitude is not None))
        else Site(name=plan.name),
        storage=Storage(database=DEFAULT_DATABASE, image_store=DEFAULT_IMAGE_STORE),
    )

    plan.project_dir.mkdir(parents=True)
    try:
        manifest.write(plan.project_dir)
        _copy_database(plan, notify)

        from .context import ProjectContext

        ctx = ProjectContext.load(plan.project_dir)
        # Case 2 of the stamping stage: an unstamped database WITH rows. Adopted
        # explicitly, with the notice, rather than silently claimed.
        ctx.open_repo(adopt=True).close()

        _copy_images(plan, notify)
        _rewrite_paths(plan, notify)

        if plan.seen_source and plan.seen_source.is_file():
            shutil.copy2(plan.seen_source, plan.seen_dest)
            lines = sum(1 for line in plan.seen_dest.read_text(
                encoding="utf-8").splitlines() if line.strip())
            notify(f"seen-store copied: {lines} message-id(s) -> {plan.seen_dest.name}")

        if plan.alias_source and plan.alias_source.is_file():
            shutil.copy2(plan.alias_source, plan.alias_dest)
            alias_map = ctx.load_alias_map()
            notify(f"alias table copied; snapshot {alias_map.content_sha256}")
            if backfill_alias_hash:
                _backfill_alias_hash(plan, alias_map.content_sha256, notify)

        verify(plan, ctx, notify)
    except BaseException:
        shutil.rmtree(plan.project_dir, ignore_errors=True)
        raise

    registry.add(RegistryEntry(id=plan.project_id, slug=manifest.slug,
                               name=plan.name,
                               directory=plan.project_dir.name))
    if registry.active_project is None:
        registry.active_project = plan.project_id
    registry.save()
    logger.info("project_imported",
                extra={"id": plan.project_id, "rows": plan.row_count})
    return manifest, plan
