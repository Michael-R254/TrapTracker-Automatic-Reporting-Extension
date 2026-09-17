"""Generate the RECONSTRUCTED Stage 1 email fixtures + sample images.

Run: ``python tests/fixtures/_generate_fixtures.py``

These ``.eml`` files are built against the reconstructed TrapTracker RT alert
format — they are NOT captured real emails.

HONESTY CAVEAT (do not present the passing suite as format confirmation): these
fixtures were produced from the *same reading* of the reconstructed format that
produced the parser. A shared misreading of the real email format — e.g. a different
field label, a different attachment-naming convention, or header quirks a real MTA
introduces — would be baked identically into both sides and the tests would still
pass. The suite therefore proves the parser is internally consistent with our
reconstruction; it CANNOT prove the reconstruction matches reality.

DECISION-7 GATE CLOSED (2026-07-16): that gap is now closed. A real captured
alert (``emails/golden/real_alert.eml``) validated the reconstruction field-for-
field. This generator no longer writes the golden — the golden is a real,
hand-captured message, not generated. It still generates the STABLE non-golden
reconstructed fixtures below, which value-specific tests depend on (so the golden
stays swap-safe).
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent
EMAILS = FIXTURES / "emails"
IMAGES = FIXTURES / "images"

# A minimal but valid 1x1 JPEG (decodable), used for both attachment roles;
# role is determined by the filename marker, not the pixels.
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkI"
    "CQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAAAP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
)

BOXED_NAME = "20260714_021301842_IMG_0091_boxed.jpg"
ORIGINAL_NAME = "20260714_021301842_IMG_0091.jpg"

_ATTACH = "attachment"


def _alert_body(project: str, rule: str, conf: str, image_id: str, time_utc: str) -> str:
    # Verbatim template order from the reconstructed format.
    return (
        f"Project: {project}\n"
        f"Rule: {rule}\n"
        f"Best confidence: {conf}\n"
        f"ImageId: {image_id}\n"
        f"Time (UTC): {time_utc}\n"
    )


def _build(
    *,
    subject: str,
    body: str,
    message_id: str,
    attachments: list[str],
    # RFC 2606 reserved domain, matching the redacted golden fixture. A
    # syntactically valid address on a real provider would be scraped once
    # this repository is public, and could belong to someone.
    from_addr: str = "garden-detections@example.com",
    to_addr: str = "traptracker-alerts@example.com",
    date: str = "Tue, 14 Jul 2026 02:13:47 +0000",
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = date
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    for name in attachments:
        msg.add_attachment(_TINY_JPEG, maintype="image", subtype="jpeg", filename=name)
    return msg


def _write(msg: EmailMessage, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(msg.as_bytes())
    print(f"wrote {path.relative_to(FIXTURES)}")


def main() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    (IMAGES / BOXED_NAME).write_bytes(_TINY_JPEG)
    (IMAGES / ORIGINAL_NAME).write_bytes(_TINY_JPEG)

    # NOTE: the golden fixture is NOT generated here. Since the Decision-7 gate
    # closed (2026-07-16) the golden is a real captured message,
    # emails/golden/real_alert.eml — hand-captured, never regenerated.

    # Both attachments (a second, distinct valid alert).
    _write(
        _build(
            subject="TrapTrackerRT Alert: MelesMeles (0.72) — Garden Detections",
            body=_alert_body("Garden Detections", "MelesMeles", "0.72", "5150", "2026-07-14 23:58:10"),
            message_id="<both-meles-5150@garden.trapcam.alerts>",
            attachments=[BOXED_NAME, ORIGINAL_NAME],
        ),
        EMAILS / "both_attachments.eml",
    )

    # Missing boxed: annotate step produced nothing → only original attached.
    _write(
        _build(
            subject="TrapTrackerRT Alert: CapreolusCapreolus (0.63) — Garden Detections",
            body=_alert_body("Garden Detections", "CapreolusCapreolus", "0.63", "6012", "2026-07-13 21:04:33"),
            message_id="<missing-boxed-6012@garden.trapcam.alerts>",
            attachments=[ORIGINAL_NAME],
        ),
        EMAILS / "missing_boxed.eml",
    )

    # Zero attachments: failed attachment read swallowed upstream.
    _write(
        _build(
            subject="TrapTrackerRT Alert: ErinaceusEuropaeus (0.55) — Garden Detections",
            body=_alert_body("Garden Detections", "ErinaceusEuropaeus", "0.55", "6789", "2026-07-12 03:19:00"),
            message_id="<zero-attach-6789@garden.trapcam.alerts>",
            attachments=[],
        ),
        EMAILS / "zero_attachments.eml",
    )

    # Malformed body: valid alert subject, but body missing 3 of 5 fields + junk.
    malformed = (
        "Project: Garden Detections\n"
        "Rule: VulpesVulpes\n"
        "-- delivery notice: some lines were dropped --\n"
    )
    _write(
        _build(
            subject="TrapTrackerRT Alert: VulpesVulpes (0.91) — Garden Detections",
            body=malformed,
            message_id="<malformed-body-0001@garden.trapcam.alerts>",
            attachments=[ORIGINAL_NAME],
        ),
        EMAILS / "malformed_body.eml",
    )

    # Test email: send_test_email path — must be IGNORED.
    _write(
        _build(
            subject="TrapTrackerRT — test email",
            body="This is a test email from TrapTrackerRT.\n",
            message_id="<test-email-9999@garden.trapcam.alerts>",
            attachments=[],
        ),
        EMAILS / "test_email_ignore.eml",
    )


if __name__ == "__main__":
    main()
