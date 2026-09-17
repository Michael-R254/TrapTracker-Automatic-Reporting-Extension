"""Map a source-layer `DetectionEvent` to a storage-layer `PersistedEvent`.

Storage consumes the shared normalised event (defined in ``sources/base.py``);
this keeps the direction of coupling correct — sources never import storage.
Species resolution (Decision 5) is *injected*:
``canonical_binomial``/``display_common_name`` are computed by the alias map at
the wiring layer and passed in here, so this mapper stays free of the alias
table (built in Stage 4).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Optional

from ..logging import get_logger
from ..sources.base import DetectionEvent
from ..sources.capture_time import parse_capture_time
from .repository import PersistedEvent

if TYPE_CHECKING:
    from ..species.aliases import SpeciesAliasMap

logger = get_logger(__name__)


def images_present_label(roles: set[str]) -> str:
    has_original = "original" in roles
    has_boxed = "boxed" in roles
    if has_original and has_boxed:
        return "both"
    if has_original:
        return "original_only"
    if has_boxed:
        return "boxed_only"
    return "none"


def persisted_from_detection_event(
    ev: DetectionEvent,
    *,
    alias_map: "Optional[SpeciesAliasMap]" = None,
    canonical_binomial: Optional[str] = None,
    canonical_is_binomial: bool = True,
    display_common_name: Optional[str] = None,
    original_image_path: Optional[str] = None,
    boxed_image_path: Optional[str] = None,
) -> PersistedEvent:
    """Build a PersistedEvent, preserving the honesty layer verbatim.

    Species identity (Decision 5) is resolved from the upstream token via
    ``alias_map`` when provided — yielding the canonical key (binomial OR reserved
    ``nonbio:<Token>``), whether it is a real binomial, and a display name. When
    ``alias_map`` is None, the explicit ``canonical_*`` args are used as-is (tests
    and the pre-Stage-4 ``store --from-fixtures`` path). ``field_provenance``
    (FieldProvenance dataclasses) is flattened to plain dicts; nothing is inferred.
    """
    roles = {img.role for img in ev.images}
    fp = ev.field_provenance
    meta = ev.raw_source_metadata or {}

    # Capture time from the attachment filename (Decision 3, amended). Prefer the
    # ORIGINAL image's name; fall back to the boxed one, then to whatever the
    # source reported. Degrades to send-time (flagged) rather than dropping.
    original_name = next((i.filename for i in ev.images if i.role == "original"), None)
    boxed_name = next((i.filename for i in ev.images if i.role == "boxed"), None)
    capture = parse_capture_time(
        original_name or boxed_name or original_image_path or boxed_image_path,
        send_time_utc=ev.upstream_event_time_utc,
    )

    if alias_map is not None:
        from ..species.aliases import NO_UPSTREAM_LABEL, is_binomial_key, unmapped_key

        # NEVER store a NULL canonical key: that makes a real detection vanish
        # from every query ("stored yet invisible"). Anything we can't resolve —
        # an unmapped token, OR no label at all — gets a reserved 'unmapped:' key
        # so it stays queryable, auditable, disclosed, and never asserted to be a
        # species. WARNING-logged so the alias table can be grown (Watch-item §8).
        if not ev.upstream_label:
            canonical_binomial = unmapped_key(NO_UPSTREAM_LABEL)
            canonical_is_binomial = False
            display_common_name = None
            logger.warning("missing_upstream_label",
                           extra={"message_id": ev.source_message_id})
        else:
            key = alias_map.canonical_key_for_token(ev.upstream_label)
            if key is not None:
                canonical_binomial = key
                canonical_is_binomial = is_binomial_key(key)
                display_common_name = alias_map.display_common_name(key)
            else:
                canonical_binomial = unmapped_key(ev.upstream_label)
                canonical_is_binomial = False
                display_common_name = None
                logger.warning("unmapped_upstream_token",
                               extra={"token": ev.upstream_label, "message_id": ev.source_message_id})

    # Resolvable only if the field is actually present AND marked resolvable.
    # Under the email source ImageId is present-but-unresolvable (FK into a DB we
    # can't access) and absent fields have nothing to resolve — so this is always
    # False here, matching "stored but flagged unresolvable".
    entry = fp.get("upstream_image_id")
    image_id_resolvable = bool(entry and entry.present and entry.resolvable)

    return PersistedEvent(
        source_type=ev.source_type,
        source_message_id=ev.source_message_id,
        ingested_at_utc=ev.ingested_at_utc,
        upstream_project=ev.upstream_project,
        upstream_label=ev.upstream_label,
        canonical_binomial=canonical_binomial,
        canonical_binomial_is_derived=True,
        canonical_is_binomial=canonical_is_binomial,
        display_common_name=display_common_name,
        upstream_confidence=ev.upstream_best_confidence,
        upstream_image_id=ev.upstream_image_id,
        upstream_image_id_resolvable=image_id_resolvable,
        event_time_utc=ev.upstream_event_time_utc,
        event_time_is_capture_time=False,   # it is send time (Decision 3) — never overwritten
        capture_time_utc=capture.capture_time_utc,
        capture_time_source=capture.source,
        capture_time_tz_validated=capture.tz_validated,
        capture_time_note=capture.note,
        original_image_path=original_image_path,
        boxed_image_path=boxed_image_path,
        images_present=images_present_label(roles),
        field_provenance={k: asdict(v) for k, v in fp.items()},
        parse_warnings=list(ev.parse_warnings),
        # Delivery provenance, straight through from the parser. Recorded, not
        # judged: a missing or failing verdict changes nothing about storage.
        source_auth_results_json=meta.get("authentication_results"),
        source_from=meta.get("from"),
        source_return_path=meta.get("return_path"),
        source_received_shape=meta.get("received_shape"),
    )
