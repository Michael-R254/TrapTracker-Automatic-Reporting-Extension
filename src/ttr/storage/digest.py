"""The corpus verdict digest — a migration's acceptance test.

Captured at Stage 0 over the 787-event corpus and documented inside
``docs/evaluation/baselines/baseline_phase2c_test.json``. Reimplemented here,
byte-compatibly, so a migration can verify ITSELF rather than being verified by
hand afterwards.

What it covers: every analytical VERDICT — both cross-check paths, full-frame and
cropped, plus the crop decision. What it deliberately does not: ids, timestamps,
paths and prose rationales, which change on any re-import and carry no analytical
meaning; and raw model output, which a migration has no business re-running.

A migration must reproduce this exactly. It moves bytes; it does not re-decide
anything. A digest that moves means the move changed a verdict, which is a
migration bug and not a finding.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Iterable, Optional

#: Fixed decimal places for float fields. Reproducible across platforms and BLAS
#: builds, where a last-bit difference carries no analytical meaning, while a
#: change large enough to matter at IoU scale is far above 1e-6.
FLOAT_DP = 6

#: Sorted by source_message_id, hashed in this order. Absent columns select as
#: SQL NULL, so a 52-column and a 91-column database produce comparable rows.
VERDICT_COLUMNS = [
    "source_message_id",
    # full-frame cross-check
    "agreement_flag", "cross_check_status", "resolution_basis",
    "matched_rank", "taxonomic_distance",
    # cropped cross-check
    "agreement_flag_crop", "cross_check_status_crop", "resolution_basis_crop",
    "matched_rank_crop", "taxonomic_distance_crop",
    # crop decision
    "crop_status", "crop_basis", "crop_box_json",
    "localisation_iou", "localisation_agreement",
    "tt_box_recovered", "tt_box_self_check", "tt_box_clipped",
    "tt_banner_basis", "tt_label_mismatch",
]

_FLOAT_COLUMNS = {"localisation_iou"}


def _round_floats(obj):
    if isinstance(obj, float):
        return float(f"{obj:.{FLOAT_DP}f}")
    if isinstance(obj, list):
        return [_round_floats(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    return obj


def _canon_json(raw):
    """Sorted keys, 6dp floats, compact. An unparseable value is hashed as its
    raw string rather than dropped — a difference is still a difference."""
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return str(raw)
    return json.dumps(_round_floats(decoded), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


def _encode(value, column: str):
    if value is None:
        return None
    if column in _FLOAT_COLUMNS:
        return f"{float(value):.{FLOAT_DP}f}"
    if column.endswith("_json"):
        return _canon_json(value)
    return str(value)


def digest_over(conn: sqlite3.Connection, columns: Iterable[str],
                present: Optional[set] = None) -> str:
    """SHA-256 over the given columns of ``detection_events``.

    Rows sorted by ``source_message_id`` using Python's str ordering (Unicode
    code point), NOT SQLite's collation; each serialised as a compact JSON array
    and terminated with a single newline.
    """
    columns = list(columns)
    if present is None:
        present = {r[1] for r in conn.execute("PRAGMA table_info(detection_events)")}
    select = ", ".join(c if c in present else "NULL" for c in columns)
    rows = conn.execute(f"SELECT {select} FROM detection_events").fetchall()
    encoded = [[_encode(r[i], columns[i]) for i in range(len(columns))] for r in rows]
    encoded.sort(key=lambda row: row[0])
    h = hashlib.sha256()
    for row in encoded:
        h.update(json.dumps(row, ensure_ascii=False,
                            separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def verdict_digest(db_path) -> str:
    """The verdict digest of a database, opened READ-ONLY.

    Read-only because this is used to fingerprint a source corpus before it is
    migrated, and fingerprinting must never be the thing that modifies it.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return digest_over(conn, VERDICT_COLUMNS)
    finally:
        conn.close()
