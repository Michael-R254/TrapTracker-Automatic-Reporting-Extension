"""Idempotent schema application, and database ownership.

``schema.sql`` uses ``CREATE TABLE/INDEX IF NOT EXISTS`` and enables WAL, so
applying it on every startup is safe and repeatable. WAL is a persistent
per-database setting; enabling it here means agents can read while ingestion
writes (plan section 5.3).

OWNERSHIP is checked only when the caller says which project it expects. That is
deliberate: a bare ``DetectionRepository(path)`` - which the test suite and
``ttr enrich`` both do - has no manifest in sight and must keep working.
``ProjectContext.open_repo()`` supplies the id; everything else supplies nothing
and skips the check. Baking the check in unconditionally would make every unowned
database unopenable, which is a far bigger change than the one being made.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Stamped into the SQLite header at creation, so any tool can tell a TrapTracker
#: Report database from an arbitrary SQLite file. That is a DIFFERENT question
#: from "which project owns this", and worth answering separately: one identifies
#: the format, the other the deployment. 0x54545231 == b"TTR1".
APPLICATION_ID = 0x54545231

#: Bumped when `project_meta`'s own shape changes, which is not the same event as
#: the detection schema changing.
PROJECT_META_VERSION = 1


class DatabaseOwnershipError(Exception):
    """This database belongs to a different project.

    Raised INSTEAD of writing. The failure it prevents - plausible-looking rows
    landing in another deployment's record - leaves nothing behind to find it by
    afterwards.
    """


def connect(db_path: str | Path, *, expected_project_id: Optional[str] = None,
            adopt: bool = False) -> sqlite3.Connection:
    """Open (creating parent dirs) a connection with the schema applied.

    ``expected_project_id`` opts in to the ownership check. ``adopt`` claims the
    database for that id even if it is stamped for another - an explicit flag for
    the migration command, which legitimately opens a database that is not yet
    anyone's, and never a silent special case.
    """
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    # Set ONLY when unset. `PRAGMA application_id=` writes the database header, so
    # doing it on every open takes a write lock — which turns an ordinary
    # concurrent read into "database is locked". Reading it first costs nothing
    # and keeps this a creation-time stamp, which is all it was ever meant to be.
    if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        conn.execute("PRAGMA application_id=%d;" % APPLICATION_ID)
    apply_migrations(conn)
    if expected_project_id is not None:
        try:
            verify_ownership(conn, expected_project_id, adopt=adopt)
        except BaseException:
            conn.close()
            raise
    return conn


def read_owner(conn: sqlite3.Connection) -> Optional[str]:
    """The project id stamped on this database, or None if it carries no stamp.

    None covers both "no `project_meta` table" and "a `project_meta` table with no
    row". Neither names an owner, and a table on its own is not a claim.
    """
    try:
        row = conn.execute(
            "SELECT project_id FROM project_meta WHERE singleton = 1").fetchone()
    except sqlite3.Error:
        return None
    return row["project_id"] if row else None


def _stamp(conn: sqlite3.Connection, project_id: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO project_meta (project_id, schema_version, created_utc, singleton) "
            "VALUES (?, ?, ?, 1) "
            "ON CONFLICT(singleton) DO UPDATE SET project_id = excluded.project_id",
            (project_id, PROJECT_META_VERSION,
             datetime.now(timezone.utc).isoformat(timespec="seconds")))


_MISMATCH = (
    "This database belongs to a different project.\n"
    "\n"
    "  expected: {expected}\n"
    "  stamped:  {found}\n"
    "\n"
    "Refusing to open it. Writing here would put this project's detections into\n"
    "another deployment's record, and nothing afterwards could tell them apart.\n"
    "\n"
    "Usually this means a project's [storage] database path was edited by hand, or\n"
    "a project directory was copied and only half renamed. Check that path against\n"
    "the project it belongs to."
)

_ADOPTING = (
    "NOTE: adopting an existing unstamped database ({rows} detection event(s)) for\n"
    "      project {project}. It predates database ownership stamping, and there is\n"
    "      exactly one candidate owner, so it is claimed rather than refused."
)


def verify_ownership(conn: sqlite3.Connection, expected_project_id: str, *,
                     adopt: bool = False, notify=None) -> str:
    """Check - and on a fresh or legacy database, establish - who owns this file.

    Four cases:

    * no stamp, no rows  -> a fresh database. Stamp it silently.
    * no stamp, rows     -> a database from before this feature. There is exactly
      one candidate owner, so adopt it, but SAY SO: silently claiming somebody's
      existing corpus is the kind of thing they should get to see happen.
    * an EMPTY `project_meta` -> the table exists but carries no row, which is
      what `apply_migrations` leaves behind when it runs against a database
      nothing then stamps. That is a table, not an owner: it is read as UNSTAMPED
      and falls into one of the two cases above. Observed in the wild, not
      hypothetical - a stale process migrated this project's own corpus this way.
    * stamped, different -> refuse. See :class:`DatabaseOwnershipError`.

    Returns the owning project id.
    """
    owner = read_owner(conn)
    if owner == expected_project_id:
        return owner

    if owner is not None and not adopt:
        raise DatabaseOwnershipError(
            _MISMATCH.format(expected=expected_project_id, found=owner))

    if owner is None:
        try:
            rows = conn.execute("SELECT COUNT(*) FROM detection_events").fetchone()[0]
        except sqlite3.Error:
            rows = 0
        if rows:
            emit = notify or (lambda m: print(m, file=sys.stderr))
            emit(_ADOPTING.format(rows=rows, project=expected_project_id))

    _stamp(conn, expected_project_id)
    return expected_project_id


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a no-op on
# an existing table, so new columns must be ALTERed in. Each entry is
# (column, DDL) and is applied only when absent — idempotent, and it never
# rewrites or back-fills a value (that is the backfill command's job, so an
# unmigrated row is visibly NULL rather than silently defaulted to a guess).
_ADDED_COLUMNS = (
    ("capture_time_utc", "TEXT"),
    ("capture_time_source", "TEXT NOT NULL DEFAULT 'none'"),
    ("capture_time_tz_validated", "INTEGER NOT NULL DEFAULT 0"),
    ("capture_time_note", "TEXT"),
    # Weather correlation enrichment (additive; NULL until the backfill runs).
    ("weather_category", "TEXT"),
    ("weather_code_raw", "INTEGER"),
    ("temperature_c", "REAL"),
    ("precipitation_mm", "REAL"),
    ("weather_match_source", "TEXT"),
    ("weather_used_send_fallback", "INTEGER NOT NULL DEFAULT 0"),
    # Two-state cross-check + its audit trail. Additive: an unmigrated row keeps its
    # agreement_flag and shows NULL audit fields, which is visibly "not recomputed"
    # rather than a silently defaulted verdict. `recompute-crosscheck` fills them.
    ("bioclip_top1_kingdom", "TEXT"),
    ("bioclip_top1_class", "TEXT"),
    ("bioclip_top1_order", "TEXT"),
    ("bioclip_top1_family", "TEXT"),
    ("bioclip_top1_genus", "TEXT"),
    ("cross_check_status", "TEXT NOT NULL DEFAULT 'evaluable'"),
    ("resolution_basis", "TEXT"),
    ("matched_rank", "TEXT"),
    ("taxonomic_distance", "TEXT"),
    # Crop path (Phase 2c). Additive and PARALLEL: the existing bioclip_* columns
    # keep their full-frame values and agreement.py keeps reading exactly those,
    # so no corroboration verdict changes when these are filled. Whether the
    # cross-check switches to the cropped read is a Phase 3 decision.
    #   -- what happened, and why
    ("crop_status", "TEXT"),
    ("crop_basis", "TEXT"),
    ("crop_error", "TEXT"),
    #   -- the crop itself
    ("crop_box_json", "TEXT"),
    ("crop_pad_frac", "REAL"),
    #   -- the independent detector that produced it
    ("detector_provider", "TEXT"),
    ("detector_model", "TEXT"),
    ("detector_box_json", "TEXT"),
    ("detector_score", "REAL"),
    ("detector_candidate_count", "INTEGER"),
    ("detector_error", "TEXT"),
    #   -- TrapTracker's recovered box: comparison only, NEVER crop geometry
    ("tt_box_json", "TEXT"),
    ("tt_box_recovered", "INTEGER"),
    ("tt_box_self_check", "TEXT"),
    ("tt_box_clipped", "INTEGER"),
    ("tt_banner_ocr_label", "TEXT"),
    ("tt_banner_ocr_confidence", "REAL"),
    ("tt_banner_ocr_score", "REAL"),
    ("tt_banner_basis", "TEXT"),
    ("tt_label_mismatch", "INTEGER"),
    ("frame_detection_count", "INTEGER"),
    #   -- do the two agree on WHERE the animal is (never on WHAT it is)
    ("localisation_iou", "REAL"),
    ("localisation_agreement", "TEXT"),
    #   -- BioCLIP re-run on the crop, stored ALONGSIDE the full-frame read
    ("bioclip_crop_ok", "INTEGER"),
    ("bioclip_crop_model", "TEXT"),
    ("bioclip_crop_topk_json", "TEXT"),
    ("bioclip_crop_error", "TEXT"),
    ("bioclip_crop_top1_kingdom", "TEXT"),
    ("bioclip_crop_top1_class", "TEXT"),
    ("bioclip_crop_top1_order", "TEXT"),
    ("bioclip_crop_top1_family", "TEXT"),
    ("bioclip_crop_top1_genus", "TEXT"),
    # The cropped-read cross-check, as a PARALLEL audit trail (Phase 3, Option B).
    # These mirror agreement_flag / resolution_basis / matched_rank /
    # taxonomic_distance exactly, one column for one column, so the two reads can be
    # crosstabbed in SQL. The published verdict stays in the UNSUFFIXED columns and
    # is not recomputed: this phase reports a comparison, it does not correct a
    # number. NULL until `recompute-crop-crosscheck` runs.
    ("agreement_flag_crop", "TEXT"),
    ("agreement_rationale_crop", "TEXT"),
    ("cross_check_status_crop", "TEXT"),
    ("resolution_basis_crop", "TEXT"),
    ("matched_rank_crop", "TEXT"),
    ("taxonomic_distance_crop", "TEXT"),
    # WHICH species table resolved this row. `canonical_binomial` is derived
    # at write time and stored, so the table in force at ingest is baked into
    # every row — and with per-project tables, "which one was that" is a real
    # question. NULL means "resolved before provenance was recorded", which is
    # true; back-filling it would be inventing an attribution.
    ("alias_table_sha256", "TEXT"),
    # Sender provenance, recorded at write time and never judged here. The
    # parser already built these and the repository threw them away, which is why
    # the 787-event corpus could not be attributed from the database alone and
    # had to be audited against the mailbox instead.
    # `source_auth_results_json` is EVERY Authentication-Results header verbatim,
    # as a JSON array: NULL means the header was ABSENT (a message that never left
    # the provider carries none), which a pass/fail column could not distinguish
    # from present-and-failing. Nothing reads these to accept or reject a message.
    ("source_auth_results_json", "TEXT"),
    ("source_from", "TEXT"),
    ("source_return_path", "TEXT"),
    ("source_received_shape", "TEXT"),
)


def apply_migrations(conn: sqlite3.Connection) -> None:
    # ORDER MATTERS. schema.sql indexes the capture_time_* columns, which an
    # already-existing table does not have yet — so ALTER them in FIRST, then run
    # the script. On a fresh DB the ALTER step is a no-op (no table yet) and
    # CREATE TABLE supplies the columns directly; on an existing DB CREATE TABLE
    # IF NOT EXISTS is the no-op and the ALTERs have already made the indexes
    # valid. Both paths converge on the same schema.
    _add_missing_columns(conn)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(detection_events)")}
    if not existing:
        return                      # fresh DB: CREATE TABLE below supplies them
    for name, ddl in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE detection_events ADD COLUMN {name} {ddl}")
