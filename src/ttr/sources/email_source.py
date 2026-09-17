"""`EmailSource` — the first concrete `DetectionSource` (plan §5.1).

``poll()`` = fetcher -> seen-store filter (by Message-ID) -> parser -> yield.
Non-alert messages (test emails, wrong subject) are ignored and marked seen so
they are not re-evaluated. Idempotency is the source's responsibility.

The fetcher and seen-store are injected, so the whole path is testable offline
with a fake fetcher over ``.eml`` fixtures (no live inbox needed).
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Callable, Iterator, Protocol, Tuple

from ..logging import get_logger
from .base import DetectionEvent, DetectionSource, NotAnAlertEmail
from .email_parser import parse_alert_email
from .seen_store import SeenStore

logger = get_logger(__name__)


class _Fetcher(Protocol):
    def fetch_unseen(self) -> Iterator[Tuple[str, EmailMessage]]: ...


class EmailSource(DetectionSource):
    def __init__(
        self,
        fetcher: _Fetcher,
        seen_store: SeenStore,
        parser: Callable[[EmailMessage], DetectionEvent] = parse_alert_email,
    ) -> None:
        self._fetcher = fetcher
        self._seen = seen_store
        self._parse = parser

    def poll(self) -> Iterator[DetectionEvent]:
        # Yielded-but-not-yet-acknowledged ids, so one batch containing the same
        # message twice does not enrich it twice. Cleared each poll: anything
        # genuinely stored has been retired to the seen-store by then, and
        # anything not stored SHOULD come round again.
        in_flight: set[str] = set()

        for message_id, msg in self._fetcher.fetch_unseen():
            # Cheap pre-skip when the Message-ID header is present and known.
            if message_id and (message_id in self._seen or message_id in in_flight):
                logger.debug("skip_already_seen", extra={"message_id": message_id})
                continue

            try:
                event = self._parse(msg)
            except NotAnAlertEmail as exc:
                logger.info("ignored_non_alert", extra={"reason": str(exc), "message_id": message_id})
                if message_id:
                    # Safe to retire immediately: there is nothing downstream to
                    # fail. A non-alert is a decision, not a pending unit of work.
                    self._seen.add(message_id)
                continue

            # Definitive key is the parsed id (covers synthesised ids too).
            if event.source_message_id in self._seen or event.source_message_id in in_flight:
                logger.debug("skip_already_seen", extra={"message_id": event.source_message_id})
                continue

            # NOT retired here — see DetectionSource.mark_processed. The consumer
            # acknowledges once the event is durably stored; until then a failure
            # must leave it eligible for the next poll rather than dropping it.
            in_flight.add(event.source_message_id)
            if event.parse_warnings:
                logger.warning(
                    "parsed_with_warnings",
                    extra={"message_id": event.source_message_id,
                           "warnings": "; ".join(event.parse_warnings)},
                )
            else:
                logger.info("parsed_event", extra={"message_id": event.source_message_id,
                                                    "label": event.upstream_label})
            yield event

    def mark_processed(self, source_message_id: str) -> None:
        """Retire an event once the consumer has stored it (see the base class)."""
        if source_message_id:
            self._seen.add(source_message_id)
