"""`SpeciesAliasMap` — canonical species identity (Decision 5, §5.2).

The scientific binomial is the canonical key for species. Three real classes
have NO binomial (Person, Car, CalibrationPole — from the deployment's class
list); they use a reserved ``nonbio:<Token>`` key so they stay queryable rather
than vanishing behind a NULL join key, and are flagged non-binomial so nothing
downstream asserts species-hood.

Design points forced by a real 31-class detector list. That list came from the
TrapTracker RT deployment and is NOT redistributed (its terms are unconfirmed);
the shipped ``species/data/species_aliases.example.yaml`` is an illustrative
table that exercises the same shapes:
  - Many tokens per key: life-stage (``NumeniusArquata``/``…Chick``) and sex
    (``CappercaillieCock``/``…Hen``) variants collapse to one binomial, so the
    reverse lookup returns a LIST (``tokens_for_binomial``), not a scalar.
  - ``bioclip_comparable``: Person/Car/CalibrationPole are outside BioCLIP's Tree
    of Life; comparing them yields a permanent false 'disagree', so agreement is
    reported as 'not_evaluable' for them — an excluded denominator, never a
    disagreement (Decision 2 spirit).
  - Ambiguous common names: a bare "deer" (4 species) or "squirrel" (2) resolves
    to multiple binomials; ``resolve`` raises :class:`AmbiguousSpecies` with the
    candidates rather than silently picking one.

All lookups are case-insensitive; the canonical spelling from the table is what
is returned. Coverage of this hand-curated table is a documented known
limitation. An unmapped UPSTREAM token yields ``None`` and makes the row
not-evaluable; an unmapped BIOCLIP label yields ``None`` too, but that is a
DISAGREEMENT — BioCLIP named something, and it was not the upstream species.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


def content_hash(text: str) -> str:
    """Short, stable identifier for an alias table's contents.

    First 16 hex characters of SHA-256 over the UTF-8 bytes: enough to name a
    snapshot file and to tell two tables apart, short enough to read in a row.
    """
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_NONBIO_PREFIX = "nonbio:"
# Reserved canonical-key prefixes never assert species-hood. A real scientific
# binomial ("Genus species") never contains a colon, so the colon is the marker.
UNMAPPED_PREFIX = "unmapped:"


class AmbiguousSpecies(Exception):
    """A user term resolves to more than one species. Carries the candidates so
    the caller can present a 'did you mean' list instead of guessing."""

    def __init__(self, term: str, candidates: list[tuple[str, Optional[str]]]) -> None:
        self.term = term
        self.candidates = candidates   # [(canonical_key, display_common_name), ...]
        listing = ", ".join(
            f"{common or key} ({key})" for key, common in candidates
        )
        super().__init__(f"'{term}' is ambiguous — did you mean: {listing}?")


def _norm(text: str) -> str:
    return " ".join(text.strip().casefold().split())


def is_binomial_key(key: str) -> bool:
    """True unless the key is a reserved non-binomial key.

    Reserved keys use a ``prefix:`` form (``nonbio:``, ``unmapped:``); a real
    scientific binomial never contains a colon, so the colon is the marker.
    """
    return ":" not in key


def unmapped_key(upstream_token: str) -> str:
    """Reserved canonical key for an upstream token absent from the alias table."""
    return f"{UNMAPPED_PREFIX}{upstream_token}"


# Sentinel used when the email carried no upstream label at all — still gets a
# reserved key (not NULL) so a label-less detection stays queryable and disclosed.
NO_UPSTREAM_LABEL = "(no upstream label)"


@dataclass(frozen=True)
class SpeciesEntry:
    """One enumerated alias-table entry, for populating a UI class list."""

    canonical_key: str          # binomial, or reserved 'nonbio:<Token>'
    display_name: str           # common name for display
    query_term: str             # a term resolve() accepts (binomial, or common name for nonbio)
    is_binomial: bool           # False for reserved non-binomial classes


class SpeciesAliasMap:
    """Loaded from a project's alias table. Binomial is canonical.

    Carries ``content_sha256``: the first 16 hex characters of the SHA-256 of the
    bytes this map was built from, or None when it was built from a dict in
    memory (tests, fixtures). It is stamped onto every row this map resolves, so
    a stored ``canonical_binomial`` can later be traced to the table that
    produced it.

    It takes NO part in resolution. Nothing below reads it, and nothing should:
    it is recorded ALONGSIDE the result, never consulted to reach one. This class
    is analytically load-bearing — what it decides is what every query then sees.
    """

    def __init__(self, entries: dict[str, dict],
                 content_sha256: Optional[str] = None) -> None:
        #: Provenance only. See the class docstring.
        self.content_sha256 = content_sha256
        self._binomial_lookup: dict[str, str] = {}          # norm(binomial) -> key (species only)
        self._token_to_key: dict[str, str] = {}             # norm(token)    -> key
        self._common_to_keys: dict[str, set[str]] = {}      # norm(common)   -> {keys}
        self._key_to_tokens: dict[str, list[str]] = {}      # key -> [tokens, verbatim]
        self._key_to_display: dict[str, Optional[str]] = {}
        self._key_comparable: dict[str, bool] = {}

        for key, body in (entries or {}).items():
            body = body or {}
            canonical = key.strip()
            tokens = self._read_tokens(body)
            commons = tuple(body.get("common_names") or ())
            comparable = bool(body.get("bioclip_comparable", True))

            self._key_to_tokens[canonical] = list(tokens)
            self._key_to_display[canonical] = commons[0] if commons else None
            self._key_comparable[canonical] = comparable
            if is_binomial_key(canonical):
                self._binomial_lookup[_norm(canonical)] = canonical
            for token in tokens:
                self._token_to_key[_norm(token)] = canonical
            for common in commons:
                self._common_to_keys.setdefault(_norm(common), set()).add(canonical)

    @staticmethod
    def _read_tokens(body: dict) -> tuple[str, ...]:
        # Prefer raw_tokens (list); accept a scalar raw_token for convenience.
        if body.get("raw_tokens"):
            return tuple(body["raw_tokens"])
        if body.get("raw_token"):
            return (body["raw_token"],)
        return ()

    # ---------------------------------------------------------------- loaders
    @classmethod
    def from_yaml(cls, path: str | Path) -> "SpeciesAliasMap":
        return cls.from_text(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def from_text(cls, text: str) -> "SpeciesAliasMap":
        """Parse from YAML source. Split out from :meth:`from_yaml` so the bundled
        example can be read as package data, which has no filesystem path of its
        own on every install layout.

        Hashes the SOURCE TEXT rather than the parsed structure: two files that
        differ only in comments or key order are different tables to a reader,
        and the snapshot written beside the hash is the text, not the parse.
        """
        return cls(yaml.safe_load(text) or {}, content_sha256=content_hash(text))

    # ------------------------------------------------------------ token -> key
    def canonical_key_for_token(self, upstream_token: str) -> Optional[str]:
        """Upstream ``.txt`` token -> canonical key (binomial OR ``nonbio:<Token>``)."""
        if not upstream_token:
            return None
        return self._token_to_key.get(_norm(upstream_token))

    def binomial_for_token(self, upstream_token: str) -> Optional[str]:
        """Upstream token -> scientific binomial, or None for non-binomial classes."""
        key = self.canonical_key_for_token(upstream_token)
        return key if (key and is_binomial_key(key)) else None

    def is_bioclip_comparable(self, upstream_token: str) -> bool:
        """Whether this class can be cross-checked against BioCLIP's taxonomy."""
        key = self.canonical_key_for_token(upstream_token)
        return self._key_comparable.get(key, False) if key else False

    # ----------------------------------------------------------- bioclip -> key
    def binomial_for_bioclip(self, bioclip_label: str) -> Optional[str]:
        """BioCLIP output (a binomial, e.g. ``Vulpes vulpes``) -> canonical binomial.

        Falls back to the first two tokens for a trinomial subspecies
        (``Ursus arctos syriacus`` -> ``Ursus arctos``). Only matches species
        keys — never a reserved ``nonbio:`` key.
        """
        if not bioclip_label:
            return None
        direct = self._binomial_lookup.get(_norm(bioclip_label))
        if direct:
            return direct
        parts = bioclip_label.split()
        if len(parts) > 2:
            return self._binomial_lookup.get(_norm(" ".join(parts[:2])))
        return None

    # -------------------------------------------------------------- user query
    def resolve(self, user_term: str) -> Optional[str]:
        """A user's common name OR binomial -> canonical key (case-insensitive).

        Raises :class:`AmbiguousSpecies` when a common name maps to more than one
        species (e.g. "deer"), so the caller can offer a 'did you mean' list
        rather than silently choosing one.
        """
        if not user_term:
            return None
        key = _norm(user_term)
        exact = self._binomial_lookup.get(key)
        if exact:
            return exact
        candidates = self._common_to_keys.get(key)
        if not candidates:
            return None
        if len(candidates) == 1:
            return next(iter(candidates))
        pairs = sorted((k, self._key_to_display.get(k)) for k in candidates)
        raise AmbiguousSpecies(user_term, pairs)

    # -------------------------------------------------------------- key -> info
    def tokens_for_binomial(self, key: str) -> list[str]:
        """Canonical key (binomial or reserved) -> its raw upstream tokens.

        Returns a LIST because several tokens can share one key (life stage/sex).
        """
        canonical = self._binomial_lookup.get(_norm(key), key)
        return list(self._key_to_tokens.get(canonical, []))

    def display_common_name(self, key: str) -> Optional[str]:
        """Canonical key -> first common name, for readable report output."""
        canonical = self._binomial_lookup.get(_norm(key), key)
        return self._key_to_display.get(canonical)

    def entries(self) -> list[SpeciesEntry]:
        """Enumerate every entry (species + reserved nonbio classes), sorted by
        display name — for populating a UI class list. Each carries a
        ``query_term`` that ``resolve()`` accepts: the binomial for a species, or
        the common name for a non-binomial class (which has no binomial)."""
        out: list[SpeciesEntry] = []
        for key, display in self._key_to_display.items():
            name = display or key
            binomial = is_binomial_key(key)
            out.append(SpeciesEntry(
                canonical_key=key,
                display_name=name,
                query_term=key if binomial else name,
                is_binomial=binomial,
            ))
        return sorted(out, key=lambda e: e.display_name.casefold())


#: The bundled example table, as PACKAGE DATA (see pyproject's package-data glob).
#: Read through ``importlib.resources`` rather than by walking up from
#: ``__file__``: the walk assumed a source checkout, so on a normal
#: ``pip install`` the fallback file did not exist and species resolution — which
#: is not optional, a NULL canonical key loses the row from every query — failed
#: with FileNotFoundError. Same mechanism ``agentdefs.loader`` already uses.
_EXAMPLE_RESOURCE = "species_aliases.example.yaml"
_DATA_DIR = "data"


def bundled_example_alias_text() -> str:
    """The bundled example alias table's YAML source."""
    from importlib.resources import files

    return (files(__package__).joinpath(_DATA_DIR, _EXAMPLE_RESOURCE)
            .read_text(encoding="utf-8"))


def bundled_example_alias_path():
    """A real filesystem path to the bundled example, for callers that need one
    (tests pointing ``SPECIES_ALIAS_PATH`` at it). Returns a context manager, since
    package data is not guaranteed to exist on disk on every install layout."""
    from importlib.resources import as_file, files

    return as_file(files(__package__).joinpath(_DATA_DIR, _EXAMPLE_RESOURCE))


def load_alias_map(settings=None, *, notify=None) -> "SpeciesAliasMap":
    """Load the alias table from config, falling back to the bundled example so the
    demo commands work out of the box.

    Defined ONCE here (rather than once in ``cli.py`` and again in ``web/app.py``)
    because species resolution must never be silently absent, and two copies of the
    fallback rule can disagree about where the table lives. ``notify`` receives a
    one-line message when the fallback is used — the CLI routes it to stderr, the
    web ingest console shows it inline.

    ``settings`` may be omitted, or carry no path, meaning "nothing configured" —
    the same shape ``load_target_taxonomy`` has always accepted. It reads the
    attribute rather than requiring a ``Settings``: the per-deployment fields left
    that class for projects, so ``ttr enrich`` with no project handed it a
    ``Settings`` that no longer has one and died on the attribute.
    """
    configured = getattr(settings, "species_alias_path", None) if settings else None
    if configured is None:
        if notify is not None:
            notify(f"[no alias table configured; using bundled example {_EXAMPLE_RESOURCE}]")
        return SpeciesAliasMap.from_text(bundled_example_alias_text())

    path = Path(configured)
    if path.exists():
        return SpeciesAliasMap.from_yaml(path)
    if notify is not None:
        notify(f"[alias table {path} not found; using bundled example {_EXAMPLE_RESOURCE}]")
    # The bundled table is hashed too. A project resolving against the shipped
    # example records THAT table's hash, which is more honest than NULL and keeps
    # "resolved against the example" distinguishable from "we do not know".
    return SpeciesAliasMap.from_text(bundled_example_alias_text())
