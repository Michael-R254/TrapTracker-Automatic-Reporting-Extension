"""``ProjectContext`` - the object Stages 3-8 consume instead of global config.

Nothing reads this yet. It is built now so the composition roots have a single
shape to be pointed at, rather than each growing its own idea of what a project
is.

Two properties are load-bearing and must survive later stages:

**``open_repo()`` is a method, not an attribute.** ``storage.migrations.connect``
uses ``sqlite3.connect``'s default ``check_same_thread=True``, and
``PipelineBundle``'s docstring (``pipeline.py:223-232``) records the consequence:
a repository must be constructed in the thread that will use it. A context that
eagerly held an open repo could not be handed to the background ingest job in
Stage 6 - it would fail at the first query, in a thread, at runtime.

**``seen_store_path`` is delegated, not reimplemented.** ``pipeline.seen_store_path``
already defines the idempotency file as ``db_path.parent / "seen_message_ids.txt"``
and says why it is defined exactly once: two copies of that rule can disagree, and
then the CLI and the web trigger re-ingest each other's mail. Pointing a project
at its own database therefore moves its seen-store automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .manifest import Mailbox, ProjectManifest, Site

if TYPE_CHECKING:                                  # imports for typing only
    from pydantic import SecretStr

    from ..species.aliases import SpeciesAliasMap
    from ..storage.repository import DetectionRepository


@dataclass(frozen=True)
class _SettingsView:
    """The narrow surface the existing loaders actually read.

    ``load_alias_map``, ``load_target_taxonomy`` and ``seen_store_path`` each read
    one attribute off whatever they are handed - they were written against an
    attribute, not against ``Settings`` itself (``tests/test_packaging.py:32-35``
    already passes a four-line stand-in). So a project can satisfy them without
    constructing a ``Settings``, which would demand IMAP credentials this stage
    does not have.
    """

    db_path: Path
    species_alias_path: Optional[Path]
    target_taxonomy_path: Optional[Path]


@dataclass(frozen=True)
class ProjectContext:
    id: str
    name: str
    slug: str
    dir: Path

    db_path: Path
    image_store_dir: Path

    alias_path: Optional[Path]
    taxonomy_path: Optional[Path]

    mailbox: Mailbox
    site: Site

    #: Machine-level settings (Ollama, BioCLIP, detector, browser). Left None
    #: this stage: `Settings` still owns the per-project fields, and Stage 3 is
    #: where they move off it. Nothing here reads it yet.
    app: Any = None

    # ------------------------------------------------------------------ build
    @classmethod
    def from_manifest(cls, manifest: ProjectManifest, project_dir: Path,
                      *, app: Any = None) -> "ProjectContext":
        def _opt(value: Optional[str]) -> Optional[Path]:
            if not value:
                return None
            candidate = Path(value)
            return candidate if candidate.is_absolute() else project_dir / candidate

        return cls(
            id=manifest.id, name=manifest.name, slug=manifest.slug,
            dir=project_dir,
            db_path=manifest.db_path(project_dir),
            image_store_dir=manifest.image_store_dir(project_dir),
            alias_path=_opt(manifest.species.alias_path),
            taxonomy_path=_opt(manifest.species.taxonomy_path),
            mailbox=manifest.mailbox, site=manifest.site, app=app,
        )

    @classmethod
    def load(cls, project_dir: Path, *, app: Any = None) -> "ProjectContext":
        return cls.from_manifest(ProjectManifest.read(project_dir), project_dir,
                                 app=app)

    # ------------------------------------------------------------ derived
    #: Where a project's own alias table lives when the manifest does not name
    #: one. A conventional per-project location rather than a global default:
    #: `Settings.species_alias_path` defaults to "config/species_aliases.yaml",
    #: which is CWD-relative and would make a project pick up whatever table
    #: happened to sit beside the process.
    DEFAULT_ALIAS_FILENAME = "species_aliases.yaml"

    @property
    def effective_alias_path(self) -> Path:
        """The path ``load_alias_map`` is actually pointed at.

        The manifest's override when set, otherwise this project's conventional
        location. That file usually does not exist, and `load_alias_map` already
        treats a missing table as "use the bundled example" - so an unset
        override still means the bundled table, while giving a project a
        documented place to put its own.
        """
        return self.alias_path or (self.dir / self.DEFAULT_ALIAS_FILENAME)

    def alias_table_source(self) -> str:
        """Which species table this project actually resolves against.

        ``"manifest"`` - a path named explicitly in project.toml.
        ``"project"``  - a file at the conventional per-project location.
        ``"bundled"``  - neither exists, so the shipped illustrative table applies.

        Exposed because the difference is otherwise invisible: dropping a file at
        the conventional location silently changes how every future event's
        species is resolved, and stored `canonical_binomial` values are derived
        at write time, so rows written before and after would disagree with no
        record of why. `ttr project list` reports this per project.
        """
        if self.alias_path is not None:
            return "manifest"
        return "project" if self.effective_alias_path.exists() else "bundled"

    def _settings_view(self) -> _SettingsView:
        return _SettingsView(db_path=self.db_path,
                             species_alias_path=self.effective_alias_path,
                             target_taxonomy_path=self.taxonomy_path)

    @property
    def seen_store_path(self) -> Path:
        """Delegated to ``pipeline.seen_store_path`` - see the module docstring."""
        from ..pipeline import seen_store_path

        return seen_store_path(self._settings_view())

    # ----------------------------------------------------------- collaborators
    def open_repo(self, *, adopt: bool = False) -> "DetectionRepository":
        """Open a repository FOR THIS PROJECT, in the calling thread.

        A method rather than a cached attribute, deliberately: sqlite connections
        are thread-bound here. Callers own the result and must close it.

        This is the one place that opts in to the ownership check: it is the only
        caller that HAS a manifest, and so the only one that can say which
        project the database is supposed to belong to. A database stamped for
        another project raises rather than being written to. ``adopt`` claims an
        already-stamped database for this project - for the migration importer,
        and never as a default.
        """
        from ..storage.repository import DetectionRepository

        return DetectionRepository(self.db_path, expected_project_id=self.id,
                                   adopt=adopt)

    #: Where content-addressed copies of the alias tables this project has used
    #: are kept. A hash IDENTIFIES a table; it does not preserve one. Both are
    #: needed and neither substitutes for the other: without the hash you cannot
    #: say which rows came from which table, and without the copy you cannot say
    #: what that table contained.
    ALIAS_SNAPSHOT_DIR = "alias_tables"

    def alias_snapshot_path(self, content_sha256: str) -> Path:
        return self.dir / self.ALIAS_SNAPSHOT_DIR / f"{content_sha256}.yaml"

    def load_alias_map(self, *, notify=None, snapshot: bool = True
                       ) -> "SpeciesAliasMap":
        """This project's alias table, or the bundled example.

        Reuses the shared loader, so the fallback rule stays defined once. An
        unset ``alias_path`` means the bundled table applies, which is what
        ``load_alias_map`` already does for an unset configured path.

        On the way, keeps a content-addressed copy of whatever was loaded, so a
        row's recorded hash can be resolved back to the table itself. Writing is
        idempotent: the same table produces the same filename and is not
        rewritten. ``snapshot=False`` is for read-only callers that should not
        create files as a side effect of answering a question.
        """
        from ..species.aliases import load_alias_map

        alias_map = load_alias_map(self._settings_view(), notify=notify)
        if snapshot and alias_map.content_sha256:
            self._write_alias_snapshot(alias_map.content_sha256)
        return alias_map

    def _write_alias_snapshot(self, content_sha256: str) -> None:
        """Copy the loaded table to ``alias_tables/<hash>.yaml`` if not already there.

        Best-effort: a project on a read-only filesystem still resolves species,
        and losing the copy costs traceability rather than correctness.
        """
        target = self.alias_snapshot_path(content_sha256)
        if target.exists():
            return
        try:
            source = self.effective_alias_path
            if source.exists():
                text = source.read_text(encoding="utf-8")
            else:
                from ..species.aliases import bundled_example_alias_text

                text = bundled_example_alias_text()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except OSError as exc:                      # pragma: no cover - rare
            from ..logging import get_logger

            get_logger(__name__).warning(
                "alias_snapshot_not_written",
                extra={"project": self.id, "hash": content_sha256,
                       "error": repr(exc)})

    def load_target_taxonomy(self):
        from ..species.taxonomy import load_target_taxonomy

        return load_target_taxonomy(self._settings_view())

    # --------------------------------------------------------------- mailbox
    def mailbox_configured(self) -> bool:
        """Whether this project has an inbox to poll at all.

        A mailbox-LESS project is a legitimate state: a project opened from a
        published extract has no inbox behind it, and reports on the rows it
        already holds.
        """
        return bool(self.mailbox.imap_host and self.mailbox.imap_user)

    def require_mailbox(self) -> None:
        """Refuse, actionably, when there is no mailbox to poll.

        ONE raise site for every ingest entry point. Before 2026-09-16 `ttr run`
        against a mailbox-less project died inside the fetcher with "EmailFetcher
        needs host, user and a password callable" - the three constructor
        arguments it was missing, which is an internal assertion and not an
        answer. The ingest page had always said it properly; the CLI and the page
        should not disagree about a state the project layer can name.
        """
        if self.mailbox_configured():
            return

        from .credentials import supply_step
        from .errors import MailboxNotConfigured
        from .paths import MANIFEST_NAME

        raise MailboxNotConfigured(
            f'No mailbox is configured for project "{self.name}", so it can '
            f"report on the rows it already holds but cannot ingest.\n"
            f"\n"
            f"Add one by editing [mailbox] in\n"
            f"    {self.dir / MANIFEST_NAME}\n"
            f"and then {supply_step(self.id)}.")

    # ----------------------------------------------------------- credentials
    # `projects_in_scope` has NO default, on purpose. It defaulted to 1 until
    # 2026-09-15 and every caller took that, so `resolve` never refused an
    # unscoped password with several projects in scope. See `Resolution.credential_scope`.
    def imap_password(self, *, projects_in_scope: int) -> "SecretStr":
        """This project's mailbox password, resolved on each call.

        A METHOD, and deliberately not cached on the instance: the value must not
        become an attribute of a long-lived object that could be logged, pickled,
        or rendered by a traceback. It is resolved, handed to the consuming call,
        and dropped - the same discipline `Settings.imap_password` follows, where
        `.get_secret_value()` is unwrapped only at `email_fetcher.py`'s login.

        Raises a distinguishable `CredentialError` subclass, never a generic
        failure: "no backend on this machine" and "backend present, no entry for
        this project" need different things done about them.
        """
        from .credentials import resolve

        return resolve(self.id, projects_in_scope=projects_in_scope).secret

    def credential_source(self, *, projects_in_scope: int = 1) -> Optional[str]:
        """Where the password WOULD come from, without reading its value."""
        from .credentials import describe_source

        return describe_source(self.id, projects_in_scope=projects_in_scope)
