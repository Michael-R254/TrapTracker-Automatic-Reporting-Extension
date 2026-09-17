"""``registry.toml`` - the index of projects, and which one is active.

App-level state ABOUT projects, as distinct from the manifest's state OF a
project. Two fields live here for that reason:

``archived`` - hiding a project from selection is a property of this
installation's view, not of the deployment itself. Keeping it out of the manifest
means archiving does not rewrite a project's own file.

``last_opened_utc`` - written ONLY by ``ttr project use``. Writing it on every
open would put a write on the read path, from a long-running ``ttr run`` and a
``ttr serve`` concurrently, against a file with no lock - a real concurrency
problem invented to sort a list. It is best-effort: its only consumer is display
order, and a stale sort key is not worth failing a command over.

Every write goes through :func:`toml_io.write_atomic`. See its docstring for why
last-writer-wins is the accepted trade and a torn file is not.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from . import toml_io
from .errors import AmbiguousProject, ProjectNotFound, RegistryError
from .manifest import utc_now_iso
from .paths import registry_path

SCHEMA_VERSION = 1

_HEADER = (
    "TrapTracker Report - project registry.",
    "",
    "An index only. Each project's own configuration lives in its",
    "projects/<dir>/project.toml. Written atomically (temp file + os.replace).",
)


@dataclass(frozen=True)
class RegistryEntry:
    id: str
    slug: str
    name: str
    #: Directory NAME under <root>/projects/, not an absolute path, so the whole
    #: root can be relocated or synced without rewriting the index.
    directory: str
    archived: bool = False
    last_opened_utc: Optional[str] = None


@dataclass
class Registry:
    path: Path
    entries: list[RegistryEntry]
    active_project: Optional[str] = None
    schema_version: int = SCHEMA_VERSION

    # ------------------------------------------------------------------- load
    @classmethod
    def load(cls, root: Optional[Path] = None) -> "Registry":
        """Read the registry, or return an empty one when it does not exist yet.

        A missing registry is the ordinary state before the first project is
        created, so it is not an error.
        """
        path = registry_path(root)
        if not path.exists():
            return cls(path=path, entries=[], active_project=None)
        data = toml_io.load(path)
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise RegistryError(
                f"{path}: schema_version {version!r}, expected {SCHEMA_VERSION}")
        entries = []
        for pid, body in (data.get("projects") or {}).items():
            body = body or {}
            missing = [k for k in ("slug", "name", "directory") if not body.get(k)]
            if missing:
                raise RegistryError(
                    f"{path}: project {pid!r} is missing {', '.join(missing)}")
            entries.append(RegistryEntry(
                id=pid, slug=body["slug"], name=body["name"],
                directory=body["directory"],
                archived=bool(body.get("archived", False)),
                last_opened_utc=body.get("last_opened_utc"),
            ))
        entries.sort(key=lambda e: (e.name.casefold(), e.id))
        return cls(path=path, entries=entries,
                   active_project=data.get("active_project"),
                   schema_version=version)

    # ------------------------------------------------------------------ write
    def to_toml(self) -> str:
        sections: list[tuple[Optional[str], dict]] = [
            (None, {"schema_version": self.schema_version,
                    "active_project": self.active_project}),
        ]
        for e in sorted(self.entries, key=lambda x: (x.name.casefold(), x.id)):
            sections.append((f"projects.{toml_io.dump_string(e.id)}", {
                "slug": e.slug,
                "name": e.name,
                "directory": e.directory,
                # Written only when true, so an ordinary entry stays two lines
                # shorter and `archived` reads as an exception rather than a
                # field everyone has.
                "archived": True if e.archived else None,
                "last_opened_utc": e.last_opened_utc,
            }))
        return toml_io.render(sections, header=_HEADER)

    def save(self) -> None:
        toml_io.write_atomic(self.path, self.to_toml())

    # --------------------------------------------------------------- mutation
    def add(self, entry: RegistryEntry) -> None:
        if any(e.id == entry.id for e in self.entries):
            raise RegistryError(f"project id already registered: {entry.id}")
        self.entries.append(entry)

    def remove(self, project_id: str) -> None:
        self.entries = [e for e in self.entries if e.id != project_id]
        if self.active_project == project_id:
            self.active_project = None

    def update(self, project_id: str, **changes) -> RegistryEntry:
        for i, e in enumerate(self.entries):
            if e.id == project_id:
                self.entries[i] = replace(e, **changes)
                return self.entries[i]
        raise ProjectNotFound(project_id)

    # ------------------------------------------------------------- resolution
    def get(self, project_id: str) -> RegistryEntry:
        for e in self.entries:
            if e.id == project_id:
                return e
        raise ProjectNotFound(project_id, [e.name for e in self.entries])

    def resolve(self, term: str) -> RegistryEntry:
        """A term -> exactly one entry, or a refusal.

        Exact id first, then id prefix, then display name (case-insensitive).
        A name matching more than one project raises :class:`AmbiguousProject`
        with the candidates rather than picking one - duplicate names are
        permitted, so ambiguity is a normal outcome and guessing would silently
        operate on the wrong project's database.

        Archived projects ARE resolvable: hiding one from a listing must not make
        it unreachable by explicit reference.
        """
        if not term:
            raise ProjectNotFound(term, [e.name for e in self.entries])
        for e in self.entries:
            if e.id == term:
                return e
        prefix = [e for e in self.entries if e.id.startswith(term)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise AmbiguousProject(term, [(e.id, e.name) for e in prefix])
        folded = term.casefold()
        by_name = [e for e in self.entries if e.name.casefold() == folded]
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise AmbiguousProject(term, [(e.id, e.name) for e in by_name])
        raise ProjectNotFound(term, [e.name for e in self.entries])

    # ------------------------------------------------------------------ views
    def visible(self) -> list[RegistryEntry]:
        """Entries a selector should offer - archived excluded.

        Stage 5's "exactly one project exists, so use it" rule reads THIS, not
        :attr:`entries`: an archived project must not make the remaining one
        ambiguous, which is most of the point of archiving.
        """
        return [e for e in self.entries if not e.archived]

    def touch_last_opened(self, project_id: str) -> None:
        """Best-effort. A failure here is logged by the caller and ignored."""
        self.update(project_id, last_opened_utc=utc_now_iso())
