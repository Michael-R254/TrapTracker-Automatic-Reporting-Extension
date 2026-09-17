"""Source-layer contracts: the normalised event shape and the `DetectionSource`
ABC (plan §5.1).

These dataclasses are the cross-stage payload. All datetimes are timezone-aware
UTC. Every parsed upstream field is Optional because the alert email may be
malformed or partial, and the honesty layer (`field_provenance`,
`parse_warnings`) records exactly what was and was not carried.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Literal, Optional


@dataclass
class RawImage:
    role: Literal["original", "boxed"]     # BioCLIP uses original; VLM uses boxed
    filename: str
    content_type: str                      # e.g. "image/jpeg"
    data: bytes


@dataclass
class FieldProvenance:
    present: bool                          # did the email actually carry this field?
    source: str                            # "email_body" | "email_header" | "derived" | "absent"
    limitation: Optional[str] = None       # e.g. "rounded 2dp, best-per-label only"
    resolvable: bool = True                # False for upstream ImageId (FK we can't resolve)


@dataclass
class DetectionEvent:
    # --- provenance / identity ---
    source_type: str                       # "email"
    source_message_id: str                 # IMAP Message-ID — the idempotency key
    ingested_at_utc: datetime              # when OUR system ingested it
    # --- parsed upstream fields (ALL Optional: email may be malformed/partial) ---
    upstream_project: Optional[str]
    upstream_label: Optional[str]          # raw class token e.g. "VulpesVulpes" — NOT a common name
    upstream_best_confidence: Optional[float]    # as delivered: 2dp, best-per-label only
    upstream_image_id: Optional[int]       # FK into a DB we do NOT have — stored, marked unresolvable
    upstream_event_time_utc: Optional[datetime]  # ALERT SEND time, NOT capture time
    # --- images: may hold 0, 1, or 2 ---
    images: list[RawImage]
    # --- honesty layer ---
    field_provenance: dict[str, FieldProvenance]  # per parsed field above (+ ingestion_validation)
    parse_warnings: list[str] = field(default_factory=list)   # missing attachment, bad line, etc.
    raw_source_metadata: dict = field(default_factory=dict)    # subject, from, to, select headers


class NotAnAlertEmail(Exception):
    """Raised by the parser for messages that are not TrapTracker RT alerts.

    Covers the `send_test_email` path (subject ``TrapTrackerRT — test email``)
    and any message whose subject lacks the ``TrapTrackerRT Alert:`` prefix.
    These are *ignored* (never stored) — distinct from a malformed alert, which
    is still stored with warnings (guardrail: degrade gracefully, never drop).
    """


class DetectionSource(ABC):
    """A swappable ingestion source. `EmailSource` now; `DatabaseSource` /
    `WebhookSource` later, with NO downstream changes."""

    @abstractmethod
    def poll(self) -> Iterator[DetectionEvent]:
        """Yield only NEW, not-previously-seen events. Idempotency is the
        source's job."""
        ...

    def mark_processed(self, source_message_id: str) -> None:
        """Record that an event reached durable storage; it is not yielded again.

        AT-LEAST-ONCE, deliberately. A source must not retire an event at the
        moment it yields one — the consumer can still fail on it (a full disk
        writing the image store, a locked database), and an event retired before
        it was stored is an event silently lost, which "degrade gracefully, never
        drop" (CLAUDE.md 4) forbids. Retiring it only on the consumer's
        acknowledgement means the worst case is a re-delivery, and re-delivery is
        already free: ``detection_events.source_message_id`` is UNIQUE and the
        insert is ``ON CONFLICT DO NOTHING``, so the database is itself a correct
        idempotency oracle. A duplicate costs one wasted enrichment pass; a drop
        costs the record.

        Default no-op, so a stateless source need not implement it.
        """
        return None
