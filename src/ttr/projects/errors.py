"""Failures the project layer raises. All carry an actionable message."""

from __future__ import annotations

from typing import Optional


class ProjectError(Exception):
    """Base for every project-layer failure."""


class ProjectNotFound(ProjectError):
    def __init__(self, term: str, known: Optional[list[str]] = None) -> None:
        self.term = term
        listing = ""
        if known:
            listing = "\nknown projects: " + ", ".join(sorted(known))
        super().__init__(f"no project matches {term!r}{listing}")


class AmbiguousProject(ProjectError):
    """A name resolves to more than one project.

    Carries the candidates so the caller can list them instead of guessing -
    the same discipline ``SpeciesAliasMap.resolve`` applies via
    :class:`~ttr.species.aliases.AmbiguousSpecies`. Duplicate display names are
    permitted (a rename must never be blocked by one), so this is a normal
    outcome of an ambiguous term, not a corrupted registry.
    """

    def __init__(self, term: str, candidates: list[tuple[str, str]]) -> None:
        self.term = term
        self.candidates = candidates          # [(id, name), ...]
        listing = ", ".join(f"{name} ({pid})" for pid, name in candidates)
        super().__init__(
            f"{term!r} is ambiguous - {len(candidates)} projects share that name: "
            f"{listing}. Use the id.")


class UnsupportedProvider(ProjectError):
    """The mailbox domain is not one this project can configure automatically.

    Raised rather than guessing a host: a wrong IMAP host surfaces later as a
    generic authentication failure, which is expensive to diagnose.
    """


class MailboxNotConfigured(ProjectError):
    """The project has no inbox to poll, so it cannot ingest.

    A real state rather than a broken one: a project opened from a published
    extract has no mailbox behind it and can report on its rows forever. What it
    cannot do is poll, and every ingest entry point refuses through
    ``ProjectContext.require_mailbox`` rather than failing inside the fetcher.
    """


class RegistryError(ProjectError):
    """The registry or a manifest could not be read, or is not a shape we know."""
