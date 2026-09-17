"""Stage 1: sender provenance is RECORDED at write time, and judged nowhere.

The corpus's provenance could not be established from the database alone because
the parser built `raw_source_metadata` and the repository discarded it
(`docs/corpus-inventory.md` §5). These tests pin the four columns that close that
gap, and — more importantly — pin the one distinction the storage layer must never
lose: a message carrying NO `Authentication-Results` header is not the same as one
carrying a failing verdict. A boolean or a pass/fail enum would collapse them.

Nothing here asserts that a verdict is acted on. This stage records; enforcement is
a later stage with decisions attached to it.
"""

from __future__ import annotations

import email
import email.policy
import json
import re
import sqlite3

import pytest

from ttr.sources.email_parser import parse_alert_email
from ttr.storage.digest import verdict_digest
from ttr.storage.mapping import persisted_from_detection_event
from ttr.storage.migrations import connect
from ttr.storage.repository import DetectionRepository

PROVENANCE_COLUMNS = (
    "source_auth_results_json",
    "source_from",
    "source_return_path",
    "source_received_shape",
)

_PASSING_AR = ("mx.google.com; dkim=pass header.i=@gmail.com; spf=pass "
               "smtp.mailfrom=alerts@example.com; dmarc=pass "
               "header.from=gmail.com")
_FAILING_AR = ("mx.google.com; dkim=fail header.i=@gmail.com; spf=softfail "
               "smtp.mailfrom=elsewhere@example.net; dmarc=fail "
               "header.from=gmail.com")


def _alert(message_id: str, *, auth_results=None, received=(), return_path=None):
    """A minimal but genuinely well-formed alert, built as bytes so the header
    block is what the parser actually sees."""
    lines = [f"Message-ID: <{message_id}>",
             "From: alerts@example.com",
             "To: inbox@example.com",
             "Subject: TrapTrackerRT Alert: ColumbaPalumbus (0.97) - GardenCatBird",
             "Date: Wed, 05 Aug 2026 09:11:29 -0700"]
    if return_path:
        lines.append(f"Return-Path: <{return_path}>")
    for r in received:
        lines.append(f"Received: {r}")
    if auth_results is not None:
        lines.append(f"Authentication-Results: {auth_results}")
    body = ("Project: GardenCatBird\n"
            "Rule: ColumbaPalumbus\n"
            "Best confidence: 0.97\n"
            "ImageId: 42\n"
            "Time (UTC): 2026-08-05 16:11:29\n")
    return email.message_from_string("\n".join(lines) + "\n\n" + body,
                                     policy=email.policy.default)


def _store(repo, msg):
    return repo.upsert_event(persisted_from_detection_event(parse_alert_email(msg)))


def _get(repo, message_id):
    """Read a stored row back. The repository has no by-message-id getter, and
    adding one to production code for a test's convenience would be the wrong
    way round, so this goes through the same row hydration the queries use."""
    cur = repo._conn.execute(
        "SELECT * FROM detection_events WHERE source_message_id = ?", (message_id,))
    row = cur.fetchone()
    return DetectionRepository._row_to_event(row) if row else None


def _columns(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(detection_events)")}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Both migration paths. schema.sql serves a FRESH database and _ADDED_COLUMNS an
# EXISTING one; a column in only one of the two works until the other path is
# taken, which is the Stage 8 lesson this repeats deliberately.
# --------------------------------------------------------------------------- #
def test_a_fresh_database_has_the_provenance_columns(tmp_path):
    repo = DetectionRepository(tmp_path / "fresh.db")
    try:
        missing = set(PROVENANCE_COLUMNS) - _columns(tmp_path / "fresh.db")
        assert not missing, f"CREATE TABLE is missing {sorted(missing)}"
    finally:
        repo.close()


def test_a_database_predating_the_columns_gains_them_on_migration(tmp_path):
    """A table built without them, then opened through the normal path."""
    from ttr.storage.migrations import SCHEMA_PATH

    # The real schema with exactly these four columns removed — a database as it
    # stood the day before Stage 1, rather than a toy table. Built this way so the
    # test exercises the ALTER path against something schema.sql's indexes can
    # actually be applied to.
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    for col in PROVENANCE_COLUMNS:
        ddl = re.sub(rf"^\s*{col}\s+TEXT,?[^\S\n]*\n", "", ddl, flags=re.MULTILINE)
    ddl = ddl.replace("alias_table_sha256          TEXT,", "alias_table_sha256          TEXT")

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(ddl)
    conn.commit()
    conn.close()
    assert not (set(PROVENANCE_COLUMNS) & _columns(db)), "precondition: absent"

    connect(db).close()

    missing = set(PROVENANCE_COLUMNS) - _columns(db)
    assert not missing, f"_ADDED_COLUMNS is missing {sorted(missing)}"


# --------------------------------------------------------------------------- #
# The distinction the whole column shape exists to protect.
# --------------------------------------------------------------------------- #
def test_an_absent_authentication_results_header_stores_as_NULL(repo):
    """A message that never left the provider carries no such header at all.

    Observed on the real mailbox: the self-sent test message among the five
    non-alerts (`corpus-inventory.md` §5.1) had no `Authentication-Results`,
    hop shape ``1:ESMTPSA``.
    """
    _store(repo, _alert("absent@mx.google.com", auth_results=None))
    row = _get(repo, "<absent@mx.google.com>")
    assert row.source_auth_results_json is None


def test_a_failing_verdict_is_stored_and_is_not_confused_with_absence(repo):
    """present-and-failing must be distinguishable from absent, by name."""
    _store(repo, _alert("failing@mx.google.com", auth_results=_FAILING_AR))
    row = _get(repo, "<failing@mx.google.com>")

    assert row.source_auth_results_json is not None, "a failing verdict is not absence"
    stored = json.loads(row.source_auth_results_json)
    assert stored == [_FAILING_AR], "the header is kept raw, not parsed to a flag"
    assert "dkim=fail" in stored[0]


def test_absent_and_failing_do_not_collapse_to_the_same_stored_value(repo):
    """The two cases side by side — the assertion a boolean column would fail."""
    _store(repo, _alert("a@mx.google.com", auth_results=None))
    _store(repo, _alert("b@mx.google.com", auth_results=_FAILING_AR))
    _store(repo, _alert("c@mx.google.com", auth_results=_PASSING_AR))

    absent = _get(repo, "<a@mx.google.com>").source_auth_results_json
    failing = _get(repo, "<b@mx.google.com>").source_auth_results_json
    passing = _get(repo, "<c@mx.google.com>").source_auth_results_json

    assert absent is None
    assert failing is not None and passing is not None
    assert failing != passing
    assert len({absent, failing, passing}) == 3, "three inputs, three stored values"


def test_every_authentication_results_header_is_kept_not_just_the_first(repo):
    """A second header claiming the receiver's authserv-id is itself evidence, so
    the count must survive storage."""
    msg = _alert("dup@mx.google.com", auth_results=_PASSING_AR)
    msg["Authentication-Results"] = _FAILING_AR          # a second, contradicting one
    _store(repo, msg)

    stored = json.loads(_get(repo, "<dup@mx.google.com>").source_auth_results_json)
    assert len(stored) == 2, "storing only the first would erase the signal"


# --------------------------------------------------------------------------- #
# A new ingest records provenance; a back-filled row keeps what it was given.
# --------------------------------------------------------------------------- #
def test_a_new_ingest_records_the_delivery_provenance(repo):
    msg = _alert("new@mx.google.com", auth_results=_PASSING_AR,
                 return_path="alerts@example.com",
                 received=["by 2002:a05 with SMTP id x; Wed, 05 Aug 2026 09:11:30 -0700",
                           "from mail-sor.google.com by mx.google.com with SMTPS id y",
                           "from [10.0.0.1] by smtp.gmail.com with ESMTPSA id z"])
    _store(repo, msg)
    row = _get(repo, "<new@mx.google.com>")

    assert json.loads(row.source_auth_results_json) == [_PASSING_AR]
    assert row.source_from == "alerts@example.com"
    assert row.source_return_path == "<alerts@example.com>"
    assert row.source_received_shape == "3:SMTP,SMTPS,ESMTPSA", (
        "the hop shape is the cheap anomaly signal; it was identical across all "
        "787 audited events")


def test_a_backfilled_row_keeps_its_recovered_provenance(repo):
    """A row stored before the columns existed, then given recovered evidence."""
    _store(repo, _alert("old@mx.google.com", auth_results=None))
    # Simulate the pre-Stage-1 state: the row exists, the columns are empty
    # because nothing recorded them at the time.
    with repo._conn:
        repo._conn.execute("UPDATE detection_events SET source_from = NULL, "
                           "source_return_path = NULL, source_received_shape = NULL")
    assert _get(repo, "<old@mx.google.com>").source_from is None

    matched = repo.backfill_source_provenance(
        "<old@mx.google.com>",
        auth_results_json=json.dumps([_PASSING_AR]),
        from_header="alerts@example.com",
        return_path="<alerts@example.com>",
        received_shape="3:SMTP,SMTPS,ESMTPSA")
    assert matched is True

    row = _get(repo, "<old@mx.google.com>")
    assert json.loads(row.source_auth_results_json) == [_PASSING_AR]
    assert row.source_from == "alerts@example.com"
    assert row.source_received_shape == "3:SMTP,SMTPS,ESMTPSA"


def test_backfilling_an_unknown_message_id_matches_nothing(repo):
    assert repo.backfill_source_provenance(
        "<gone@mx.google.com>", auth_results_json=None, from_header="x",
        return_path=None, received_shape=None) is False


# --------------------------------------------------------------------------- #
# Recording provenance changes no analytical value.
# --------------------------------------------------------------------------- #
def test_the_verdict_digest_is_unmoved_by_recording_provenance(tmp_path):
    """The four columns are not verdict columns, so the digest must not move.

    Asserted rather than assumed: if it moves, the digest's column set is wrong.
    """
    db = tmp_path / "digest.db"
    repo = DetectionRepository(db)
    try:
        _store(repo, _alert("d1@mx.google.com", auth_results=None))
        _store(repo, _alert("d2@mx.google.com", auth_results=_PASSING_AR))
        before = verdict_digest(db)

        repo.backfill_source_provenance(
            "<d1@mx.google.com>", auth_results_json=json.dumps([_FAILING_AR]),
            from_header="someone@example.net", return_path=None,
            received_shape="9:WEIRD")
    finally:
        repo.close()

    assert verdict_digest(db) == before, (
        "provenance is not a verdict; writing it must not move the digest")


def test_a_malformed_alert_is_still_stored_with_its_provenance(repo):
    """The existing contract is unchanged: this stage records, it does not reject.

    A body missing every field still parses into a row (with warnings), and that
    row still carries whatever the envelope said.
    """
    msg = email.message_from_string(
        "Message-ID: <bad@mx.google.com>\n"
        "From: alerts@example.com\n"
        "Subject: TrapTrackerRT Alert: nothing parseable here\n"
        f"Authentication-Results: {_FAILING_AR}\n\n"
        "no recognised field prefixes at all\n",
        policy=email.policy.default)
    _store(repo, msg)

    row = _get(repo, "<bad@mx.google.com>")
    assert row is not None, "a malformed alert is stored, never dropped"
    assert row.parse_warnings, "and its warnings are recorded"
    assert json.loads(row.source_auth_results_json) == [_FAILING_AR], (
        "a failing verdict is recorded, not acted on")
