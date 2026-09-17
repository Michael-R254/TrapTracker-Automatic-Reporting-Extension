"""``project.toml`` - one project's configuration, and never its secrets.

The manifest holds what a project IS: its identity, which mailbox it reads, where
its site is, and where its data lives. It never holds the mailbox password.
Stage 2 adds ``mailbox.credential_ref``, a REFERENCE to an entry in the OS
credential store - so a manifest that is copied, synced or accidentally shared
discloses nothing.

Paths in ``[storage]`` are relative to the project directory, so the directory can
be moved or synced as a unit. The single exception, read here and written by the
Stage 8 importer, is a linked path: ``database`` / ``image_store`` may be absolute
when the matching ``*_external`` flag is true, marking a target the tool does not
own and must never delete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import toml_io
from .errors import RegistryError
from .paths import DEFAULT_DATABASE, DEFAULT_IMAGE_STORE, MANIFEST_NAME

SCHEMA_VERSION = 1

_HEADER = (
    "TrapTracker Report - project manifest.",
    "",
    "This file contains NO secrets. The mailbox password lives in the OS",
    "credential store; only a reference to it is recorded here.",
    "",
    "Paths under [storage] are relative to this directory unless the matching",
    "*_external flag is true, which marks a linked target this project does not",
    "own and must never delete.",
)


def _mailbox_comments(mailbox: "Mailbox") -> list[str]:
    """Document the [mailbox] keys that are supported but currently unset.

    An unset key is omitted from the file (see ``toml_io.render``), which is
    correct but makes the setting undiscoverable - a user reading their manifest
    cannot find a knob that is not written down. So each omitted key is left
    behind as a commented example.
    """
    notes: list[str] = []
    if mailbox.imap_lookback_days is None:
        notes += [
            "",
            "imap_lookback_days: only scan mail newer than N days. Unset (the",
            "default) scans the whole folder, which is right for a dedicated",
            "alert mailbox. Set it if this inbox has years of history:",
            "imap_lookback_days = 30",
        ]
    if mailbox.credential_ref is None:
        notes += [
            "",
            "credential_ref: written by `ttr project set-password`. It is a",
            "LOCATOR for the OS credential store, never the password itself.",
        ]
    return notes


@dataclass
class Mailbox:
    address: str = ""
    imap_host: str = ""
    imap_user: str = ""
    imap_folder: str = "INBOX"
    imap_use_ssl: bool = True
    imap_lookback_days: Optional[int] = None
    poll_seconds: int = 30
    #: Written in Stage 2. A reference, never the secret.
    credential_ref: Optional[str] = None


@dataclass
class Site:
    name: Optional[str] = None
    #: Deliberately unset by default. `backfill-weather` already refuses to run
    #: without coordinates, which is the correct loud failure: a guessed location
    #: would fetch weather for somewhere the camera is not.
    latitude: Optional[float] = None
    longitude: Optional[float] = None


@dataclass
class Species:
    #: Absent means "use the bundled tables", exactly as `load_alias_map` and
    #: `load_target_taxonomy` already behave when their configured path is unset.
    alias_path: Optional[str] = None
    taxonomy_path: Optional[str] = None


@dataclass
class Storage:
    database: str = DEFAULT_DATABASE
    image_store: str = DEFAULT_IMAGE_STORE
    database_external: bool = False
    image_store_external: bool = False


@dataclass
class ProjectManifest:
    id: str
    name: str
    slug: str
    created_utc: str
    schema_version: int = SCHEMA_VERSION
    mailbox: Mailbox = field(default_factory=Mailbox)
    site: Site = field(default_factory=Site)
    species: Species = field(default_factory=Species)
    storage: Storage = field(default_factory=Storage)

    # ------------------------------------------------------------------ paths
    def resolve(self, project_dir: Path, value: str, external: bool) -> Path:
        """A ``[storage]`` value as an absolute path.

        Relative values join the project directory; an external value is taken as
        given. An external value that is NOT absolute is a corrupted manifest -
        the flag exists precisely to mark an absolute target - so it is refused
        rather than quietly joined.
        """
        candidate = Path(value)
        if external:
            if not candidate.is_absolute():
                raise RegistryError(
                    f"{project_dir / MANIFEST_NAME}: an external storage path must "
                    f"be absolute; got {value!r}")
            return candidate
        return project_dir / candidate

    def db_path(self, project_dir: Path) -> Path:
        return self.resolve(project_dir, self.storage.database,
                            self.storage.database_external)

    def image_store_dir(self, project_dir: Path) -> Path:
        return self.resolve(project_dir, self.storage.image_store,
                            self.storage.image_store_external)

    def external_paths(self, project_dir: Path) -> list[Path]:
        """Every linked target - the paths ``delete`` must leave alone."""
        out = []
        if self.storage.database_external:
            out.append(self.db_path(project_dir))
        if self.storage.image_store_external:
            out.append(self.image_store_dir(project_dir))
        return out

    @property
    def is_linked(self) -> bool:
        return self.storage.database_external or self.storage.image_store_external

    # --------------------------------------------------------------- io
    def to_toml(self) -> str:
        return toml_io.render(
            [
                (None, {"schema_version": self.schema_version}),
                ("project", {"id": self.id, "slug": self.slug, "name": self.name,
                             "created_utc": self.created_utc}),
                ("mailbox", {"address": self.mailbox.address,
                             "imap_host": self.mailbox.imap_host,
                             "imap_user": self.mailbox.imap_user,
                             "imap_folder": self.mailbox.imap_folder,
                             "imap_use_ssl": self.mailbox.imap_use_ssl,
                             "imap_lookback_days": self.mailbox.imap_lookback_days,
                             "poll_seconds": self.mailbox.poll_seconds,
                             "credential_ref": self.mailbox.credential_ref},
                 _mailbox_comments(self.mailbox)),
                ("site", {"name": self.site.name,
                          "latitude": self.site.latitude,
                          "longitude": self.site.longitude}),
                ("species", {"alias_path": self.species.alias_path,
                             "taxonomy_path": self.species.taxonomy_path}),
                ("storage", {"database": self.storage.database,
                             "image_store": self.storage.image_store,
                             "database_external": self.storage.database_external or None,
                             "image_store_external": self.storage.image_store_external or None}),
            ],
            header=_HEADER,
        )

    def write(self, project_dir: Path) -> Path:
        path = project_dir / MANIFEST_NAME
        toml_io.write_atomic(path, self.to_toml())
        return path

    @classmethod
    def from_dict(cls, data: dict, *, source: str = "<manifest>") -> "ProjectManifest":
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise RegistryError(
                f"{source}: schema_version {version!r}, expected {SCHEMA_VERSION}")
        project = data.get("project") or {}
        for key in ("id", "name", "slug", "created_utc"):
            if not project.get(key):
                raise RegistryError(f"{source}: [project] is missing {key}")
        mailbox = data.get("mailbox") or {}
        site = data.get("site") or {}
        species = data.get("species") or {}
        storage = data.get("storage") or {}
        defaults = Mailbox()
        return cls(
            id=project["id"], name=project["name"], slug=project["slug"],
            created_utc=project["created_utc"], schema_version=version,
            mailbox=Mailbox(
                address=mailbox.get("address", ""),
                imap_host=mailbox.get("imap_host", ""),
                imap_user=mailbox.get("imap_user", ""),
                imap_folder=mailbox.get("imap_folder", defaults.imap_folder),
                imap_use_ssl=bool(mailbox.get("imap_use_ssl", defaults.imap_use_ssl)),
                imap_lookback_days=mailbox.get("imap_lookback_days"),
                poll_seconds=mailbox.get("poll_seconds", defaults.poll_seconds),
                credential_ref=mailbox.get("credential_ref"),
            ),
            site=Site(name=site.get("name"), latitude=site.get("latitude"),
                      longitude=site.get("longitude")),
            species=Species(alias_path=species.get("alias_path"),
                            taxonomy_path=species.get("taxonomy_path")),
            storage=Storage(
                database=storage.get("database", DEFAULT_DATABASE),
                image_store=storage.get("image_store", DEFAULT_IMAGE_STORE),
                database_external=bool(storage.get("database_external", False)),
                image_store_external=bool(storage.get("image_store_external", False)),
            ),
        )

    @classmethod
    def read(cls, project_dir: Path) -> "ProjectManifest":
        path = project_dir / MANIFEST_NAME
        return cls.from_dict(toml_io.load(path), source=str(path))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
