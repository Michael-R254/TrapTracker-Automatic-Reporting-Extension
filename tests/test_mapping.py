"""The mapper's alias_map= branch: species resolution on the way into storage,
and the unmapped:<Token> fallback so a real detection is never stored NULL."""

from __future__ import annotations

from datetime import datetime, timezone

from ttr.sources.base import DetectionEvent
from ttr.sources.email_parser import parse_alert_email
from ttr.storage.mapping import persisted_from_detection_event

from conftest import load_eml

UTC = timezone.utc


def _event(label):
    return DetectionEvent(
        source_type="email", source_message_id="<m@x>",
        ingested_at_utc=datetime(2026, 7, 15, tzinfo=UTC),
        upstream_project="P", upstream_label=label, upstream_best_confidence=0.8,
        upstream_image_id=None, upstream_event_time_utc=datetime(2026, 7, 14, tzinfo=UTC),
        images=[], field_provenance={}, parse_warnings=[],
    )


def test_alias_map_resolves_binomial(alias_map):
    # Stable reconstructed fixture (MelesMeles), not the swappable golden.
    ev = parse_alert_email(load_eml("both_attachments.eml"))              # MelesMeles
    pe = persisted_from_detection_event(ev, alias_map=alias_map)
    assert pe.canonical_binomial == "Meles meles"            # RESOLVED, not NULL
    assert pe.canonical_is_binomial is True
    assert pe.display_common_name == "european badger"


def test_nonbinomial_class_resolves_to_reserved_key(alias_map):
    pe = persisted_from_detection_event(_event("Person"), alias_map=alias_map)
    assert pe.canonical_binomial == "nonbio:Person"
    assert pe.canonical_is_binomial is False


def test_unmapped_token_falls_back_to_reserved_key(alias_map):
    # A token absent from the alias table must NOT become NULL (invisible).
    pe = persisted_from_detection_event(_event("PterodactylusMaximus"), alias_map=alias_map)
    assert pe.canonical_binomial == "unmapped:PterodactylusMaximus"
    assert pe.canonical_is_binomial is False
    assert pe.display_common_name is None


def test_label_less_event_uses_reserved_key(alias_map):
    # No upstream label at all must NOT become NULL/invisible either.
    pe = persisted_from_detection_event(_event(None), alias_map=alias_map)
    assert pe.canonical_binomial == "unmapped:(no upstream label)"
    assert pe.canonical_is_binomial is False


def test_no_alias_map_leaves_explicit_values(alias_map):
    # Back-compat path (no alias_map): explicit args used as-is.
    pe = persisted_from_detection_event(_event("VulpesVulpes"), canonical_binomial="X")
    assert pe.canonical_binomial == "X"
