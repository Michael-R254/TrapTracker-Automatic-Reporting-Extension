"""Enrichment contracts: result dataclasses + enricher ABCs (plan §5.2)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaxonomicRead:
    provider: str                          # "bioclip"
    model_name: str
    topk: list[tuple[str, float]] = field(default_factory=list)  # [(taxon, score), ...], desc
    #: Full taxonomic hierarchy per top-k entry, positionally aligned with ``topk``:
    #: [{kingdom, phylum, class, order, family, genus, species_epithet}, ...].
    #: BioCLIP returns every rank, not just the binomial; keeping them lets a
    #: disagreement be measured (same genus? another kingdom?) instead of merely
    #: recorded. Empty on a failed read, and on rows enriched before this was
    #: captured — an absent hierarchy yields a distance of 'undetermined', never a
    #: guess reconstructed from the binomial.
    topk_lineages: list[dict] = field(default_factory=list)
    embedding: Optional[list[float]] = None
    ok: bool = False
    error: Optional[str] = None            # populated on failure; record still stored


@dataclass
class DescriptionRead:
    provider: str                          # "ollama"
    model_name: str
    text: Optional[str] = None             # plain-English description of the boxed image
    ok: bool = False
    error: Optional[str] = None


class TaxonomicEnricher(ABC):
    @abstractmethod
    def classify(self, original_image: bytes) -> TaxonomicRead:   # runs on ORIGINAL
        ...


class DescriptionEnricher(ABC):
    @abstractmethod
    def describe(self, boxed_image: bytes) -> DescriptionRead:    # runs on BOXED
        ...
