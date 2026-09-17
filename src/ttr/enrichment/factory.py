"""Config-driven enricher selection (§5.2).

Providers are chosen by config so a swap is a config change, not a code change.
Only the currently-shipped providers are wired; the seam is here for adding more.
"""

from __future__ import annotations

from ..config import Settings
from .base import DescriptionEnricher, TaxonomicEnricher
from .bioclip_enricher import BioClipEnricher
from .ollama_enricher import OllamaEnricher


def build_taxonomic_enricher(settings: Settings) -> TaxonomicEnricher:
    return BioClipEnricher(
        model_name=settings.bioclip_model,
        device=settings.bioclip_device,
        topk=settings.bioclip_topk,
    )


def build_description_enricher(settings: Settings) -> DescriptionEnricher:
    return OllamaEnricher(
        model=settings.ollama_model,
        endpoint=settings.ollama_endpoint,
        timeout_seconds=settings.ollama_timeout_seconds,
    )
