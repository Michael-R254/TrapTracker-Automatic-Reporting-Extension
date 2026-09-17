"""`RetrievalAgent` — DB-read only (§5.4).

Resolves the user's term (common name OR binomial) to a canonical key via the
alias map BEFORE any query (Decision 5), then reads from the repository. No LLM,
no enrichment, no ingestion. An unresolved term returns ``[]`` with a logged
warning (never a silent empty); an ambiguous term propagates
:class:`AmbiguousSpecies` so the caller can offer a 'did you mean' list.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..logging import get_logger
from ..species.aliases import AmbiguousSpecies, SpeciesAliasMap
from ..storage.repository import DetectionRepository, PersistedEvent
from .window import TimeWindow

logger = get_logger(__name__)


class RetrievalAgent:
    def __init__(self, repo: DetectionRepository, alias_map: SpeciesAliasMap) -> None:
        self._repo = repo
        self._alias_map = alias_map

    def resolve_identity(self, species: str) -> tuple[Optional[str], Optional[str]]:
        """Return ``(canonical_key, display_common_name)`` for a term.

        ``(None, None)`` if unrecognised; raises :class:`AmbiguousSpecies` for an
        ambiguous common name. Lets the report name the species even with zero
        matching records.
        """
        key = self._alias_map.resolve(species)   # may raise AmbiguousSpecies
        if key is None:
            return None, None
        return key, self._alias_map.display_common_name(key)

    def find(
        self,
        species: str,
        window: TimeWindow,
        camera: Optional[str] = None,   # forward hook; never populated by email source
    ) -> list[PersistedEvent]:
        try:
            key = self._alias_map.resolve(species)
        except AmbiguousSpecies:
            logger.warning("species_ambiguous", extra={"term": species})
            raise
        if key is None:
            logger.warning("species_unresolved", extra={"term": species})
            return []

        results = self._repo.query(
            binomial=key,
            start_utc=window.start_utc,
            end_utc=window.end_utc,
            camera=camera,
        )
        logger.info("retrieval", extra={"term": species, "canonical_key": key, "n": len(results)})
        return results

    def find_all(self, window: TimeWindow) -> list[PersistedEvent]:
        """Every dated, in-window detection regardless of species — the all-species
        summary basis. Includes ``unmapped:``/``nonbio:`` keys so the breakdown
        reconciles against the total. No term to resolve (all species)."""
        results = self._repo.query_all(start_utc=window.start_utc, end_utc=window.end_utc)
        logger.info("retrieval_all", extra={"n": len(results)})
        return results

    def count_undated_all(self) -> int:
        """Rows of any species with no usable timestamp (all-species view)."""
        return self._repo.count_undated_all()

    def find_unmapped(self, window: TimeWindow) -> list[PersistedEvent]:
        """Detections whose upstream token was not in the alias table (or absent
        entirely) — reserved ``unmapped:`` key. Dated rows are window-bounded;
        undated ones are always included. Lets the report disclose them rather
        than let them vanish from every species query."""
        return self._repo.query_unmapped(start_utc=window.start_utc, end_utc=window.end_utc)

    def find_others(self, canonical_key: str, window: TimeWindow) -> list[PersistedEvent]:
        """Mapped detections of OTHER species in the window — non-target activity.

        Takes an ALREADY-RESOLVED canonical key (the caller resolved the user's
        term once); unmapped rows are excluded here and disclosed separately.
        """
        results = self._repo.query_others(
            exclude_binomial=canonical_key,
            start_utc=window.start_utc,
            end_utc=window.end_utc,
        )
        logger.info("retrieval_others",
                    extra={"canonical_key": canonical_key, "n": len(results)})
        return results

    def data_span(self) -> tuple[Optional[datetime], Optional[datetime]]:
        """Earliest and latest detection held across ALL species — the extent of
        the dataset, so a report can distinguish a quiet day from a day its window
        covers but the data does not."""
        return self._repo.data_span()

    def delivery_timeline(
        self, window: TimeWindow, canonical_key: Optional[str] = None
    ) -> list[tuple[datetime, Optional[datetime]]]:
        """``(send_time, capture_time)`` in the window, for gap detection.

        Without ``canonical_key`` this spans ALL species — the only basis on which
        a system outage can be judged, since the mailbox stops for everything at
        once. With one, it is that species' own stream, so recovery counts and
        absence periods belong to the report's subject instead of the dataset.
        """
        return self._repo.delivery_timeline(
            start_utc=window.start_utc, end_utc=window.end_utc, binomial=canonical_key)

    def send_time_span(self) -> tuple[Optional[datetime], Optional[datetime]]:
        """Earliest/latest SEND time across all species — the operational-coverage
        bound (last confirmed-operational point). Distinct from ``data_span``
        (effective/capture time): a delivered alert proves the system was up even
        when its capture is dated earlier by a backlog flush."""
        return self._repo.send_time_span()

    def weather_base_rate(self, window: TimeWindow) -> dict[str, int]:
        """Category → cached weather-hours in the window (the exposure denominator
        for the weather view). Empty until the weather cache is populated."""
        return self._repo.weather_base_rate(window.start_utc, window.end_utc)

    def count_undated(self, canonical_key: str) -> int:
        """How many rows for this already-resolved canonical key have no usable
        timestamp (excluded from the day-by-day window figures)."""
        return self._repo.count_undated(canonical_key)

    def earliest_dated(self, canonical_key: str) -> Optional[datetime]:
        """Earliest dated event held for an already-resolved canonical key (or
        None). Lets the report distinguish a period-over-period baseline that
        predates our data from one that is simply empty (Change 2)."""
        return self._repo.earliest_event_time(canonical_key)
