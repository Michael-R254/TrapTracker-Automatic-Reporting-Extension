"""Lineages and taxonomic distance, for the cross-check's audit layer (Phase 3).

The cross-check VERDICT is species-level and binary: BioCLIP either resolves to the
same binomial as the upstream label or it does not. Nothing in this module can turn
a non-match into an ``agree`` — no genus or family "near miss" is ever promoted.

What this module adds is the *explanation* of a disagreement. Every disagreement
carries a ``taxonomic_distance``: was BioCLIP one genus away (a near miss on a real
bird) or in another kingdom entirely (a read of the grass behind the feeder)? That
replaces the old ``indeterminate`` bucket, which lumped both together and so read as
a mystery pile rather than as evidence.

Two lineages are needed to measure a distance:
  - the UPSTREAM target's, from the hand-entered ``data/target_taxonomy.yaml``;
  - the PREDICTION's, which comes from BioCLIP's own output and is stored per event
    (``bioclip_top1_*`` columns). It is never looked up in a vendored copy of
    BioCLIP's label table — that table is not redistributed (LICENSING_REVIEW.md §4),
    and taking the ranks straight from the prediction is exact rather than inferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

#: Distance between two identifications, nearest rank first. ``same_species`` is the
#: ONLY value that accompanies an ``agree``; every other value describes a
#: disagreement and is what the report shows in place of the old 'indeterminate'.
DISTANCES = (
    "same_species",     # identical binomial (or a subspecies of it)
    "same_genus",       # e.g. Columba oenas vs Columba palumbus
    "same_family",      # e.g. Streptopelia decaocto vs Columba palumbus
    "same_order",       # e.g. Quiscalus quiscula vs Corvus corone
    "same_class",       # a bird, but a different order — a real misidentification
    "other_animal",     # Animalia, different class: fish, reptile, insect...
    # BioCLIP read a non-animal kingdom. NOT "no animal was present" — the
    # upstream detector found one and alerted on it; this is the second
    # opinion disagreeing about what it was. The stored value stays
    # ``non_animal``: it is persisted on every row, so renaming it is a data
    # migration, not a relabelling. The display phrase carries the meaning.
    "non_animal",       # Plantae / Fungi / Bacteria / Chromista
    "undetermined",     # a lineage was unavailable; distance not measurable
)

#: Human-readable phrase per distance, for report prose. Kept beside the vocabulary
#: so a new distance value cannot be added without a phrase to render it.
#:
#: Every phrase describes BIOCLIP's identification relative to the upstream label,
#: never the scene. The comparative ones carry that in the wording — "same genus,
#: DIFFERENT species" is self-evidently a comparison between two identifications.
#: ``non_animal`` did not: read on its own in a table, "not an animal" states that
#: there was no animal present, which is the opposite of what it means. TrapTracker
#: detected and alerted on an animal; BioCLIP's second opinion landed on a plant or
#: a fungus instead. It names BioCLIP in the cell for that reason.
DISTANCE_LABELS = {
    "same_species": "same species",
    "same_genus": "same genus, different species",
    "same_family": "same family, different genus",
    "same_order": "same order, different family",
    "same_class": "same class, different order",
    "other_animal": "different class of animal",
    # Not only plants and fungi on the real corpus: Plantae 119, Fungi 83,
    # Bacteria 2, Chromista 1 — hence "or other non-animal" rather than a
    # parenthetical that would be wrong on a handful of rows.
    "non_animal": "BioCLIP read a plant, fungus or other non-animal",
    "undetermined": "distance undetermined",
}


@dataclass(frozen=True)
class Lineage:
    """One taxon's ranks. ``genus``/``species_epithet`` may be empty when only the
    higher ranks are known; the distance function degrades rather than guessing."""

    kingdom: Optional[str] = None
    phylum: Optional[str] = None
    class_name: Optional[str] = None        # 'class' is a keyword
    order: Optional[str] = None
    family: Optional[str] = None
    genus: Optional[str] = None
    species_epithet: Optional[str] = None

    @property
    def binomial(self) -> Optional[str]:
        if not (self.genus and self.species_epithet):
            return None
        return f"{self.genus} {self.species_epithet}"

    @classmethod
    def from_mapping(cls, body: dict) -> "Lineage":
        """Build from a dict keyed by rank name, accepting BioCLIP's own key
        spellings (``class``, ``species_epithet``) as well as this class's fields."""
        body = body or {}
        return cls(
            kingdom=body.get("kingdom") or None,
            phylum=body.get("phylum") or None,
            class_name=body.get("class") or body.get("class_name") or None,
            order=body.get("order") or None,
            family=body.get("family") or None,
            genus=body.get("genus") or None,
            species_epithet=body.get("species_epithet") or None,
        )

    def as_mapping(self) -> dict:
        """Round-trips through :meth:`from_mapping`; used for JSON storage."""
        return {
            "kingdom": self.kingdom, "phylum": self.phylum, "class": self.class_name,
            "order": self.order, "family": self.family, "genus": self.genus,
            "species_epithet": self.species_epithet,
        }

    def is_empty(self) -> bool:
        return not any(self.as_mapping().values())


def _eq(a: Optional[str], b: Optional[str]) -> bool:
    """Rank equality. Two MISSING ranks are not a match — an absent value is
    unknown, not 'the same as the other unknown one'."""
    return bool(a) and bool(b) and a.casefold() == b.casefold()


def taxonomic_distance(target: Optional[Lineage], predicted: Optional[Lineage]) -> str:
    """How far apart two identifications are; one of :data:`DISTANCES`.

    Descends the ranks and returns the first that matches, so the value is always
    the NEAREST shared rank. ``non_animal`` is reported off the prediction's own
    kingdom rather than off a mismatch, because "BioCLIP named a plant" is the
    finding worth reporting — it is what a whole-frame read of an un-cropped alert
    image produces, and it is not a taxonomic near-miss at all.
    """
    if target is None or predicted is None or target.is_empty() or predicted.is_empty():
        return "undetermined"
    if _eq(target.genus, predicted.genus) and _eq(target.species_epithet, predicted.species_epithet):
        return "same_species"
    if _eq(target.genus, predicted.genus):
        return "same_genus"
    if _eq(target.family, predicted.family):
        return "same_family"
    if _eq(target.order, predicted.order):
        return "same_order"
    if _eq(target.class_name, predicted.class_name):
        return "same_class"
    if predicted.kingdom and predicted.kingdom.casefold() != "animalia":
        return "non_animal"
    if _eq(target.kingdom, predicted.kingdom):
        return "other_animal"
    return "undetermined"


class TargetTaxonomy:
    """Higher ranks for the alias table's canonical binomials.

    Lookup is by canonical binomial key, so it joins to the alias table on exactly
    the key the alias table itself is built on (Decision 5). Reserved ``nonbio:``
    keys are absent by construction and return ``None`` — those classes are
    reported as not-evaluable, never compared.
    """

    def __init__(self, entries: dict) -> None:
        self._by_binomial: dict[str, Lineage] = {}
        for key, body in (entries or {}).items():
            if ":" in key:
                continue                    # reserved non-binomial key: no lineage
            genus, _, epithet = key.partition(" ")
            body = dict(body or {})
            body.setdefault("genus", genus)
            body.setdefault("species_epithet", epithet)
            self._by_binomial[key.casefold()] = Lineage.from_mapping(body)

    def lineage_for(self, binomial: Optional[str]) -> Optional[Lineage]:
        if not binomial:
            return None
        return self._by_binomial.get(binomial.casefold())

    def binomials(self) -> list[str]:
        return sorted(self._by_binomial)

    def __len__(self) -> int:
        return len(self._by_binomial)

    # ---------------------------------------------------------------- loaders
    @classmethod
    def from_yaml(cls, path: str | Path) -> "TargetTaxonomy":
        return cls.from_text(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def from_text(cls, text: str) -> "TargetTaxonomy":
        return cls(yaml.safe_load(text) or {})

    @classmethod
    def bundled(cls) -> "TargetTaxonomy":
        """The shipped table, read as PACKAGE DATA. Same mechanism as the alias
        table's bundled example: walking up from ``__file__`` assumes a source
        checkout and breaks under a plain ``pip install``."""
        from importlib.resources import files

        text = (files(__package__).joinpath("data", _TAXONOMY_RESOURCE)
                .read_text(encoding="utf-8"))
        return cls.from_text(text)


_TAXONOMY_RESOURCE = "target_taxonomy.yaml"


def load_target_taxonomy(settings=None) -> TargetTaxonomy:
    """Load the target-taxonomy table, honouring an optional config override.

    Unlike the alias table there is no user-supplied default location: the shipped
    file is authoritative because its keys must stay in lockstep with the alias
    table's canonical keys (``test_config_consistency`` enforces that). An override
    exists only so a deployment with a different class list can supply lineages to
    match it.
    """
    path = getattr(settings, "target_taxonomy_path", None) if settings else None
    if path:
        candidate = Path(path)
        if candidate.exists():
            return TargetTaxonomy.from_yaml(candidate)
    return TargetTaxonomy.bundled()
