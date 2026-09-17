"""Stage 1 verification: the pure parser turns each fixture into a correct
DetectionEvent with accurate provenance and warnings, and rejects non-alerts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ttr.sources.base import NotAnAlertEmail
from ttr.sources.email_parser import parse_alert_email

from conftest import load_eml


# --------------------------------------------------------------------------- #
# Golden — the real captured alert email (Decision-7 gate closed 2026-07-16),
# REDACTED for public release: transport headers stripped, addresses replaced,
# attachment bytes swapped for synthetic JPEGs. Every field asserted below is
# carried through from the real message unchanged, so this remains a regression
# test against real data. See golden/README.md for what was removed and why.
# --------------------------------------------------------------------------- #
def test_golden_parses_all_fields():
    ev = parse_alert_email(load_eml("golden/real_alert.eml"))

    assert ev.source_type == "email"
    assert ev.source_message_id == "<alert-20260716121536-0001@example.com>"
    assert ev.upstream_project == "GardenCatBird"
    assert ev.upstream_label == "ColumbaPalumbus"       # raw token, authoritative
    assert ev.upstream_best_confidence == 0.85
    assert ev.upstream_image_id == 2808
    assert ev.upstream_event_time_utc == datetime(2026, 7, 16, 12, 15, 34, tzinfo=timezone.utc)
    # Nominal both-attachments alert with the '_boxed' marker present: the role
    # convention is an evidence-backed assumption, so it is NOT
    # flagged per event — a clean parse has zero warnings.
    assert ev.parse_warnings == []


def test_golden_event_time_is_tz_aware_utc():
    ev = parse_alert_email(load_eml("golden/real_alert.eml"))
    assert ev.upstream_event_time_utc.tzinfo is not None
    assert ev.upstream_event_time_utc.utcoffset() == timezone.utc.utcoffset(None)


def test_golden_images_have_correct_roles():
    ev = parse_alert_email(load_eml("golden/real_alert.eml"))
    roles = sorted(img.role for img in ev.images)
    assert roles == ["boxed", "original"]
    for img in ev.images:
        assert img.content_type == "image/jpeg"
        assert img.data.startswith(b"\xff\xd8")  # decodable JPEG bytes carried through
    boxed = next(i for i in ev.images if i.role == "boxed")
    assert "_boxed" in boxed.filename


# --------------------------------------------------------------------------- #
# Provenance / honesty layer.
# --------------------------------------------------------------------------- #
def test_provenance_flags_uncertainty_and_unresolvable_fields():
    ev = parse_alert_email(load_eml("golden/real_alert.eml"))
    fp = ev.field_provenance

    assert fp["upstream_best_confidence"].present is True
    assert "2dp" in fp["upstream_best_confidence"].limitation

    # ImageId is a FK into a DB we don't have — stored but unresolvable.
    assert fp["upstream_image_id"].present is True
    assert fp["upstream_image_id"].resolvable is False

    # Event time is send-time, not capture-time.
    assert "capture" in fp["upstream_event_time_utc"].limitation.lower()


def test_ingestion_validation_note_present_decision7():
    ev = parse_alert_email(load_eml("golden/real_alert.eml"))
    note = ev.field_provenance["ingestion_validation"]
    assert note.present is True
    # Decision-7 gate closed for the NOMINAL path only — note records that scope.
    assert "nominal" in note.limitation.lower()
    assert "validated" in note.limitation.lower()
    assert "2026-07-16" in note.limitation
    assert "reconstruction only" in note.limitation.lower()


# --------------------------------------------------------------------------- #
# Attachment edge cases.
# --------------------------------------------------------------------------- #
def test_both_attachments_fixture():
    ev = parse_alert_email(load_eml("both_attachments.eml"))
    assert ev.upstream_label == "MelesMeles"
    assert sorted(i.role for i in ev.images) == ["boxed", "original"]
    # Marker present on the boxed attachment -> convention resolves cleanly, so
    # the evidence-backed assumption is NOT flagged per event.
    assert ev.parse_warnings == []


def test_marked_attachments_are_not_flagged_per_event():
    # The '_boxed' convention is a documented assumption (evidence: 475 real
    # messages). When it resolves, no role warning fires — it is not noise.
    ev = parse_alert_email(load_eml("golden/real_alert.eml"))
    assert not any("role" in w.lower() for w in ev.parse_warnings)
    assert ev.parse_warnings == []


def test_two_unmarked_attachments_flagged_uncertain():
    """Two attachments, neither with a '_boxed' marker: roles cannot be told
    apart, so both default to 'original' AND a role-uncertainty warning fires."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "TrapTrackerRT Alert: VulpesVulpes (0.80) — Garden Detections"
    msg["Message-ID"] = "<two-unmarked@example.com>"
    msg.set_content(
        "Project: Garden Detections\nRule: VulpesVulpes\nBest confidence: 0.80\n"
        "ImageId: 1\nTime (UTC): 2026-07-14 02:13:47\n"
    )
    msg.add_attachment(b"\xff\xd8\xff\xd9", maintype="image", subtype="jpeg",
                       filename="20260714_IMG_A.jpg")
    msg.add_attachment(b"\xff\xd8\xff\xd9", maintype="image", subtype="jpeg",
                       filename="20260714_IMG_B.jpg")

    ev = parse_alert_email(msg)
    assert [i.role for i in ev.images] == ["original", "original"]
    assert any("cannot distinguish" in w.lower() for w in ev.parse_warnings)


def test_missing_boxed_yields_original_only_with_warning():
    ev = parse_alert_email(load_eml("missing_boxed.eml"))
    assert [i.role for i in ev.images] == ["original"]
    assert any("boxed" in w.lower() for w in ev.parse_warnings)


def test_zero_attachments_warns_but_still_parses():
    ev = parse_alert_email(load_eml("zero_attachments.eml"))
    assert ev.images == []
    assert ev.upstream_label == "ErinaceusEuropaeus"          # body still parsed
    assert any("no image attachments" in w.lower() for w in ev.parse_warnings)


# --------------------------------------------------------------------------- #
# Malformed body — never fabricate; record absence.
# --------------------------------------------------------------------------- #
def test_malformed_body_records_absence_not_fabrication():
    ev = parse_alert_email(load_eml("malformed_body.eml"))

    # Present fields survive.
    assert ev.upstream_project == "Garden Detections"
    assert ev.upstream_label == "VulpesVulpes"

    # Absent fields are None with 'absent' provenance and a warning — never guessed.
    for field_name in ("upstream_best_confidence", "upstream_image_id", "upstream_event_time_utc"):
        assert getattr(ev, field_name) is None
        assert ev.field_provenance[field_name].present is False
        assert ev.field_provenance[field_name].source == "absent"
    assert len(ev.parse_warnings) >= 3


# --------------------------------------------------------------------------- #
# Rejection of non-alert emails.
# --------------------------------------------------------------------------- #
def test_test_email_is_rejected():
    with pytest.raises(NotAnAlertEmail):
        parse_alert_email(load_eml("test_email_ignore.eml"))


def test_wrong_subject_is_rejected():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "Your Amazon order has shipped"
    msg["Message-ID"] = "<spam@example.com>"
    msg.set_content("Project: nope\n")
    with pytest.raises(NotAnAlertEmail):
        parse_alert_email(msg)


# --------------------------------------------------------------------------- #
# Message-ID synthesis when the header is absent.
# --------------------------------------------------------------------------- #
def test_synthesises_stable_message_id_when_header_absent():
    from email.message import EmailMessage

    def _msg():
        m = EmailMessage()
        m["Subject"] = "TrapTrackerRT Alert: VulpesVulpes (0.80) — Garden Detections"
        m["From"] = "garden-detections@example.com"
        m["Date"] = "Tue, 14 Jul 2026 02:13:47 +0000"
        m.set_content(
            "Project: Garden Detections\nRule: VulpesVulpes\nBest confidence: 0.80\n"
            "ImageId: 1\nTime (UTC): 2026-07-14 02:13:47\n"
        )
        return m

    ev1 = parse_alert_email(_msg())
    ev2 = parse_alert_email(_msg())
    assert ev1.source_message_id.startswith("synthetic:")
    assert ev1.source_message_id == ev2.source_message_id  # deterministic
    assert any("synthesised" in w.lower() for w in ev1.parse_warnings)
