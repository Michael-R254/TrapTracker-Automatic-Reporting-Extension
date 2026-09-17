"""Pure alert-email parser: ``EmailMessage -> DetectionEvent`` (plan §5.1).

No I/O. Fully unit-testable on saved ``.eml`` fixtures. Built against the
reconstructed TrapTracker RT alert format.
Its NOMINAL shape was validated against a real captured alert email on 2026-07-16
(Decision 7 — matched field-for-field); edge-case variants (zero/missing
attachments, malformed body, test emails) remain validated against the
reconstruction only. Every event carries an ``ingestion_validation`` provenance
note recording that scope.

The parser NEVER fabricates: a field the email did not carry becomes ``None``
with an ``absent`` provenance entry and a parse warning — never an inferred
value.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from email.message import EmailMessage, Message
from typing import Optional

from .base import (
    DetectionEvent,
    FieldProvenance,
    NotAnAlertEmail,
    RawImage,
)

# Subject markers.
ALERT_SUBJECT_PREFIX = "TrapTrackerRT Alert:"
TEST_EMAIL_SUBJECT = "TrapTrackerRT — test email"

# Body is plain text, five line-prefixed fields. Prefixes are the
# stable contract; values are parsed leniently so a partial body still yields
# what it can. Maps body prefix -> DetectionEvent field name.
_BODY_FIELDS: tuple[tuple[str, str], ...] = (
    ("Project:", "upstream_project"),
    ("Rule:", "upstream_label"),
    ("Best confidence:", "upstream_best_confidence"),
    ("ImageId:", "upstream_image_id"),
    ("Time (UTC):", "upstream_event_time_utc"),
)

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_BOXED_MARKER = "_boxed"

# Provenance note wording is constant, so a note-per-field stays consistent.
_CONF_LIMITATION = "2dp-rounded, best-per-label only (source email discards granularity)"
_IMAGEID_LIMITATION = "foreign key into the upstream DB we cannot access"
_TIME_LIMITATION = "alert-send time, NOT camera capture time (no EXIF time exists upstream)"
# Decision-7 gate CLOSED 2026-07-16 for the NOMINAL alert path only: a real
# captured email validated the well-formed, both-attachments, single-rule shape
# field-for-field. Edge-case variants (zero/missing attachments, malformed body,
# test emails) are handled by the parser but not yet confirmed by a real message.
_INGESTION_VALIDATION_NOTE = (
    "nominal alert format validated field-for-field against a real captured alert email "
    "on 2026-07-16 (Decision 7); edge-case variants (zero/missing attachments, malformed "
    "body) validated against the reconstruction only"
)


def parse_alert_email(msg: EmailMessage | Message) -> DetectionEvent:
    """Parse a TrapTracker RT alert email into a :class:`DetectionEvent`.

    Raises :class:`NotAnAlertEmail` for the test-email path or any message
    lacking the alert subject prefix. A malformed *alert* is not rejected: it is
    returned with ``None`` fields, ``absent`` provenance, and parse warnings.
    """
    subject = _decode_header(msg, "Subject")
    _reject_if_not_alert(subject)

    warnings: list[str] = []
    provenance: dict[str, FieldProvenance] = {}

    body = _extract_plaintext_body(msg)
    if body is None:
        warnings.append("no plain-text body found")
        body = ""
    values = _scan_body_lines(body)

    project = _take_project(values, provenance, warnings)
    label = _take_label(values, provenance, warnings)
    confidence = _take_confidence(values, provenance, warnings)
    image_id = _take_image_id(values, provenance, warnings)
    event_time = _take_event_time(values, provenance, warnings)

    images = _extract_images(msg, warnings)

    # Decision 7 gate (CLOSED 2026-07-16): record that the format is validated
    # against a real captured alert email — events are no longer provisional.
    provenance["ingestion_validation"] = FieldProvenance(
        present=True, source="derived", limitation=_INGESTION_VALIDATION_NOTE, resolvable=True
    )

    message_id = _message_id(msg, subject, body, warnings)

    return DetectionEvent(
        source_type="email",
        source_message_id=message_id,
        ingested_at_utc=datetime.now(timezone.utc),
        upstream_project=project,
        upstream_label=label,
        upstream_best_confidence=confidence,
        upstream_image_id=image_id,
        upstream_event_time_utc=event_time,
        images=images,
        field_provenance=provenance,
        parse_warnings=warnings,
        raw_source_metadata={
            "subject": subject,
            "from": _decode_header(msg, "From"),
            "to": _decode_header(msg, "To"),
            "date": _decode_header(msg, "Date"),
            "message_id_header": _decode_header(msg, "Message-ID"),
            # Delivery provenance. RECORDED, never judged: nothing here changes
            # whether a message parses or is stored. See `_auth_results_json`.
            "authentication_results": _auth_results_json(msg),
            "return_path": _decode_header(msg, "Return-Path"),
            "received_shape": received_shape(msg),
        },
    )


# --------------------------------------------------------------------------- #
# Rejection / subject
# --------------------------------------------------------------------------- #
def _reject_if_not_alert(subject: Optional[str]) -> None:
    if subject is None:
        raise NotAnAlertEmail("message has no Subject header")
    stripped = subject.strip()
    if stripped == TEST_EMAIL_SUBJECT:
        raise NotAnAlertEmail("send_test_email path (test email) — ignored")
    if not stripped.startswith(ALERT_SUBJECT_PREFIX):
        raise NotAnAlertEmail(f"subject lacks alert prefix: {subject!r}")


# --------------------------------------------------------------------------- #
# Body scanning
# --------------------------------------------------------------------------- #
def _scan_body_lines(body: str) -> dict[str, str]:
    """Return {field_name: raw_value_string} for whatever prefixed lines exist.

    Line-prefix parsing only (no structured markup upstream). A field
    absent from the body simply does not appear in the returned dict.
    """
    found: dict[str, str] = {}
    for line in body.splitlines():
        for prefix, field_name in _BODY_FIELDS:
            if line.startswith(prefix) and field_name not in found:
                found[field_name] = line[len(prefix):].strip()
                break
    return found


def _absent(provenance: dict, key: str, warnings: list[str], what: str) -> None:
    provenance[key] = FieldProvenance(present=False, source="absent")
    warnings.append(f"missing or unparseable field: {what}")


def _take_project(values, provenance, warnings) -> Optional[str]:
    raw = values.get("upstream_project")
    if raw:
        provenance["upstream_project"] = FieldProvenance(present=True, source="email_body")
        return raw
    _absent(provenance, "upstream_project", warnings, "Project")
    return None


def _take_label(values, provenance, warnings) -> Optional[str]:
    raw = values.get("upstream_label")
    if raw:
        # Authoritative raw class token — stored verbatim, never mutated.
        provenance["upstream_label"] = FieldProvenance(present=True, source="email_body")
        return raw
    _absent(provenance, "upstream_label", warnings, "Rule (upstream label)")
    return None


def _take_confidence(values, provenance, warnings) -> Optional[float]:
    raw = values.get("upstream_best_confidence")
    if raw is not None:
        try:
            value = float(raw)
            provenance["upstream_best_confidence"] = FieldProvenance(
                present=True, source="email_body", limitation=_CONF_LIMITATION
            )
            return value
        except ValueError:
            warnings.append(f"unparseable Best confidence value: {raw!r}")
    _absent(provenance, "upstream_best_confidence", warnings, "Best confidence")
    return None


def _take_image_id(values, provenance, warnings) -> Optional[int]:
    raw = values.get("upstream_image_id")
    if raw is not None:
        try:
            value = int(raw)
            # Stored but explicitly unresolvable (FK into inaccessible DB).
            provenance["upstream_image_id"] = FieldProvenance(
                present=True, source="email_body",
                limitation=_IMAGEID_LIMITATION, resolvable=False,
            )
            return value
        except ValueError:
            warnings.append(f"unparseable ImageId value: {raw!r}")
    _absent(provenance, "upstream_image_id", warnings, "ImageId")
    return None


def _take_event_time(values, provenance, warnings) -> Optional[datetime]:
    raw = values.get("upstream_event_time_utc")
    if raw is not None:
        try:
            naive = datetime.strptime(raw, _TIME_FORMAT)
            value = naive.replace(tzinfo=timezone.utc)  # body time is UTC
            provenance["upstream_event_time_utc"] = FieldProvenance(
                present=True, source="email_body", limitation=_TIME_LIMITATION
            )
            return value
        except ValueError:
            warnings.append(f"unparseable Time (UTC) value: {raw!r}")
    _absent(provenance, "upstream_event_time_utc", warnings, "Time (UTC)")
    return None


# --------------------------------------------------------------------------- #
# Body / header extraction helpers
# --------------------------------------------------------------------------- #
def _extract_plaintext_body(msg: EmailMessage | Message) -> Optional[str]:
    """Return the text/plain body (plain text only, no HTML part)."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not _is_attachment(part):
                return _part_text(part)
        return None
    if msg.get_content_type() == "text/plain":
        return _part_text(msg)
    return None


def _part_text(part: EmailMessage | Message) -> Optional[str]:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return None
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return None


def _is_attachment(part: EmailMessage | Message) -> bool:
    disp = part.get_content_disposition()
    return disp == "attachment" or bool(part.get_filename())


def _extract_images(msg: EmailMessage | Message, warnings: list[str]) -> list[RawImage]:
    """Extract 0, 1, or 2 image attachments, tagging role by filename.

    Role is assigned from the ``_boxed`` filename marker: ``boxed`` if present,
    else ``original``. This convention is a **documented, evidence-backed
    assumption** (confirmed on 475 real messages plus a captured original/boxed
    pair; the reconstructed format gave it only as an illustrative "e.g."), so it
    is NOT flagged per event when the marker resolves cleanly. A per-event warning
    is emitted ONLY where the inference genuinely does not resolve — 2+ attachments
    with no ``_boxed`` marker (annotated vs original indistinguishable). The
    missing-image cases (no original / no boxed) are also reported, since they are
    a real per-event degradation for BioCLIP / the VLM respectively.
    """
    images: list[RawImage] = []
    for part in msg.walk():
        if not _is_attachment(part):
            continue
        content_type = part.get_content_type()
        if not content_type.startswith("image/"):
            continue
        filename = part.get_filename() or "unknown"
        data = part.get_payload(decode=True) or b""
        role = "boxed" if _BOXED_MARKER in filename.lower() else "original"
        images.append(RawImage(role=role, filename=filename,
                               content_type=content_type, data=data))

    if not images:
        warnings.append("no image attachments present")
        return images

    boxed_n = sum(1 for img in images if img.role == "boxed")
    original_n = len(images) - boxed_n

    # The ONLY genuinely ambiguous case: 2+ attachments, none carrying the marker,
    # so we cannot distinguish the annotated image from the original.
    if len(images) >= 2 and boxed_n == 0:
        warnings.append(
            "role uncertain: 2+ attachments but none marked '_boxed' — cannot distinguish "
            "the annotated image from the original; all treated as 'original' (the '_boxed' "
            "convention is an evidence-backed assumption that does not resolve here)"
        )
    # Report which role is missing (a real per-event degradation, not role inference).
    if boxed_n and original_n == 0:
        warnings.append("only a boxed/annotated image present — no original attached")
    elif original_n and boxed_n == 0 and len(images) == 1:
        warnings.append("boxed (annotated) image absent — only the original attached")
    return images


def _decode_header(msg: EmailMessage | Message, name: str) -> Optional[str]:
    value = msg[name]
    return str(value) if value is not None else None


def _auth_results_json(msg: EmailMessage | Message) -> Optional[str]:
    """EVERY ``Authentication-Results`` header, verbatim, as a JSON array.

    Raw, and all of them, for three reasons that a parsed verdict would lose.

    **Absent is not failing.** A message sent from the mailbox to itself never
    leaves the provider and carries no such header at all. Stored as a boolean or
    a pass/fail enum, "no header" and "dkim=fail" collapse into one value and the
    difference is gone from the record permanently. ``None`` here means the header
    was absent; a JSON array means it was present, whatever it says.

    **Which one is trustworthy is not this layer's call.** Only the header added
    by the RECEIVING MX can be believed — a receiver strips inbound copies bearing
    its own authserv-id before adding its own — but which authserv-id that is
    depends on the deployment's mail provider. Selecting it is a decision for an
    enforcement stage that has been taken; keeping every header verbatim leaves
    that decision open rather than pre-empting it here.

    **More than one is itself evidence.** A second header claiming the receiver's
    authserv-id should be impossible, so preserving the count preserves the
    signal. Storing only the first would erase it.
    """
    values = msg.get_all("Authentication-Results")
    if not values:
        return None
    return json.dumps([str(v) for v in values], ensure_ascii=False)


def received_shape(msg: EmailMessage | Message) -> Optional[str]:
    """Hop count plus each hop's transport token, e.g. ``3:SMTP,SMTPS,ESMTPSA``.

    A cheap anomaly signal: the shape was identical across all 787 events of the
    2026-09-09 provenance audit, so a message
    whose delivery path differs is visible without storing the chain itself —
    which carries hostnames and originating IPs this project has no reason to
    keep. Deliberately coarse, and never used to accept or reject anything.

    NOTE: `docs/evaluation/provenance_audit.py` carries its own copy of this rule,
    frozen as the Stage 0 evidence it produced. This is the live definition.
    """
    values = msg.get_all("Received")
    if not values:
        return None
    toks = []
    for v in values:
        m = re.search(r"\bwith\s+(\w+)", re.sub(r"\s+", " ", str(v)))
        toks.append(m.group(1).upper() if m else "?")
    return f"{len(values)}:" + ",".join(toks)


def _message_id(msg, subject: Optional[str], body: str, warnings: list[str]) -> str:
    """Return the Message-ID, or a stable synthetic id if the header is absent.

    Message-ID is the idempotency key. Real alerts relayed via Gmail carry one;
    if a message lacks it, we synthesise a deterministic id from stable content
    so dedup still works, and warn.
    """
    raw = msg["Message-ID"]
    if raw:
        return str(raw).strip()
    warnings.append("no Message-ID header — synthesised a stable id from content")
    basis = "|".join([
        _decode_header(msg, "From") or "",
        _decode_header(msg, "Date") or "",
        subject or "",
        body,
    ])
    digest = hashlib.sha1(basis.encode("utf-8", errors="replace")).hexdigest()
    return f"synthetic:{digest}"


__all__ = ["parse_alert_email", "ALERT_SUBJECT_PREFIX", "TEST_EMAIL_SUBJECT"]
