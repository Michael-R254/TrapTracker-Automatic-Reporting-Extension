"""`DetectionRepository` — thin data-access layer over sqlite3 (plan §5.3).

One row per detection event. Provenance and uncertainty are columns, not
comments. This module is the single owner of the storage shape: JSON columns and
datetime<->ISO conversion happen here and nowhere else, so callers stay in
Python-native types and a later backend swap is localised (Fix b).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .migrations import connect

#: The clock a detection is placed on a day by: capture time when it was decoded,
#: else the alert-send time. Defined ONCE here so the window filter and the
#: report's day bucketing (``PersistedEvent.effective_time_utc``) can never drift
#: apart. ISO-8601 strings compare correctly with `<=`/`>=` in SQLite.
_EFFECTIVE_TIME = "COALESCE(capture_time_utc, event_time_utc)"

# Columns written by upsert_event (embedding is handled separately — see below).
_INSERT_COLUMNS = (
    "source_type",
    "source_message_id",
    "ingested_at_utc",
    "upstream_project",
    "upstream_label",
    "canonical_binomial",
    "canonical_binomial_is_derived",
    "canonical_is_binomial",
    "display_common_name",
    "upstream_confidence",
    "upstream_confidence_note",
    "upstream_image_id",
    "upstream_image_id_resolvable",
    "event_time_utc",
    "event_time_is_capture_time",
    "capture_time_utc",
    "capture_time_source",
    "capture_time_tz_validated",
    "capture_time_note",
    "bioclip_ok",
    "bioclip_model",
    "bioclip_topk_json",
    "bioclip_error",
    "bioclip_top1_kingdom",
    "bioclip_top1_class",
    "bioclip_top1_order",
    "bioclip_top1_family",
    "bioclip_top1_genus",
    "agreement_flag",
    "agreement_rationale",
    "cross_check_status",
    "resolution_basis",
    "matched_rank",
    "taxonomic_distance",
    "vlm_ok",
    "vlm_model",
    "vlm_description",
    "vlm_error",
    "weather_category",
    "weather_code_raw",
    "temperature_c",
    "precipitation_mm",
    "weather_match_source",
    "weather_used_send_fallback",
    "original_image_path",
    "boxed_image_path",
    "images_present",
    "provenance_json",
    "parse_warnings_json",
    "created_at_utc",
    "alias_table_sha256",
    "source_auth_results_json",
    "source_from",
    "source_return_path",
    "source_received_shape",
)


@dataclass
class PersistedEvent:
    """Storage-facing view of a detection event (one DB row).

    Native Python types throughout; the repository serialises to/from the DB.
    ``bioclip_embedding`` is intentionally NOT here — embeddings move only
    through ``store_embedding``/``get_embedding`` so the vector storage shape has
    a single owner (Fix b).
    """

    source_type: str
    source_message_id: str
    ingested_at_utc: datetime
    upstream_project: Optional[str] = None
    upstream_label: Optional[str] = None                       # authoritative raw token
    canonical_binomial: Optional[str] = None                   # DERIVED (Decision 5); binomial OR 'nonbio:<Token>'
    canonical_binomial_is_derived: bool = True
    canonical_is_binomial: bool = True                         # False for reserved non-binomial keys
    display_common_name: Optional[str] = None                  # DERIVED, for display only
    upstream_confidence: Optional[float] = None
    upstream_confidence_note: Optional[str] = "2dp_rounded_best_per_label"
    upstream_image_id: Optional[int] = None
    upstream_image_id_resolvable: bool = False                 # FK we can't resolve
    event_time_utc: Optional[datetime] = None                  # SEND time — never overwritten
    event_time_is_capture_time: bool = False
    # Capture time decoded from the attachment filename (Decision 3, amended).
    # Stored ALONGSIDE the send time; the divergence between them is evidence.
    capture_time_utc: Optional[datetime] = None
    capture_time_source: str = "none"                          # filename_block_d | send_time_fallback | none
    capture_time_tz_validated: bool = False                    # True only inside the validated BST window
    capture_time_note: Optional[str] = None
    bioclip_ok: bool = False
    bioclip_model: Optional[str] = None
    bioclip_topk: Optional[list] = None                        # [[taxon, score], ...]
    bioclip_error: Optional[str] = None
    # Top-1 lineage as BioCLIP reported it (never inferred from the binomial);
    # NULL on rows stored before hierarchy capture.
    bioclip_top1_kingdom: Optional[str] = None
    bioclip_top1_class: Optional[str] = None
    bioclip_top1_order: Optional[str] = None
    bioclip_top1_family: Optional[str] = None
    bioclip_top1_genus: Optional[str] = None
    agreement_flag: Optional[str] = None                       # agree | disagree | not_evaluable
    agreement_rationale: Optional[str] = None
    # Was a comparison possible at all? The binary is reported over 'evaluable'
    # rows; 'not_evaluable' is an explicit excluded denominator, not a verdict.
    cross_check_status: str = "evaluable"
    resolution_basis: Optional[str] = None                     # HOW the verdict was reached
    matched_rank: Optional[str] = None                         # species | none
    taxonomic_distance: Optional[str] = None                   # how far apart the two IDs are
    vlm_ok: bool = False
    vlm_model: Optional[str] = None
    vlm_description: Optional[str] = None
    vlm_error: Optional[str] = None
    # Weather correlation (Open-Meteo, matched to capture time). Additive.
    weather_category: Optional[str] = None                     # sunny|cloudy|rainy|unknown|unavailable
    weather_code_raw: Optional[int] = None
    temperature_c: Optional[float] = None
    precipitation_mm: Optional[float] = None
    weather_match_source: Optional[str] = None
    weather_used_send_fallback: bool = False
    original_image_path: Optional[str] = None
    boxed_image_path: Optional[str] = None
    images_present: str = "none"                               # both|original_only|boxed_only|none
    field_provenance: dict = field(default_factory=dict)       # JSON-serialisable
    parse_warnings: list = field(default_factory=list)
    created_at_utc: Optional[datetime] = None                  # set at insert if None
    #: WHICH alias table resolved `canonical_binomial`. NULL on rows written
    #: before provenance was recorded — true, and better than a guess.
    alias_table_sha256: Optional[str] = None
    #: Sender provenance off the delivery envelope, recorded and never judged.
    #: `source_auth_results_json` is every Authentication-Results header verbatim
    #: as a JSON array; None means the header was ABSENT, which is not the same
    #: as present-and-failing and must stay distinguishable.
    source_auth_results_json: Optional[str] = None
    source_from: Optional[str] = None
    source_return_path: Optional[str] = None
    source_received_shape: Optional[str] = None
    id: Optional[int] = None

    @property
    def effective_time_utc(self) -> Optional[datetime]:
        """The time this event is PLACED ON A DAY by: capture time when it was
        decoded, else the send time. One definition, used by the repository's
        window filter and the report's day bucketing alike, so a row can never be
        selected by one clock and then counted under another."""
        return self.capture_time_utc or self.event_time_utc

    @property
    def day_basis(self) -> str:
        """Which clock ``effective_time_utc`` came from — for disclosure."""
        if self.capture_time_utc is not None and self.capture_time_source == "filename_block_d":
            return "capture"
        return "send"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _dumps(obj) -> Optional[str]:
    return json.dumps(obj) if obj is not None else None


def _loads(value: Optional[str]):
    return json.loads(value) if value else None


def _col(row: sqlite3.Row, name: str):
    """A column that may be absent from THIS row's cursor description.

    ``sqlite3.Row`` raises ``IndexError`` for an unknown key, so a hydrate that
    names a column added by a later migration would blow up on a row selected by a
    narrower query. Missing reads as ``None`` — 'not recorded', which is exactly
    what an unmigrated or partially-selected row means. Used only for columns added
    after the first release; an original column stays a hard subscript so a genuine
    schema break still fails loudly."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


class DetectionRepository:
    def __init__(self, db_path: str | Path, *, expected_project_id=None,
                 adopt: bool = False) -> None:
        # `expected_project_id` opts in to the ownership check (see
        # `migrations.connect`). Left None by every caller that has no manifest —
        # `ttr enrich`, the test suite — so those keep working unchanged.
        self._conn = connect(db_path, expected_project_id=expected_project_id,
                             adopt=adopt)

    # ----------------------------------------------------------------- write
    def upsert_event(self, ev: PersistedEvent) -> int:
        """Insert an event; UNIQUE(source_message_id) makes a repeat a no-op.

        Returns the row id in both the inserted and the already-present case.
        """
        created = ev.created_at_utc or datetime.now(ev.ingested_at_utc.tzinfo)
        values = {
            "source_type": ev.source_type,
            "source_message_id": ev.source_message_id,
            "ingested_at_utc": _iso(ev.ingested_at_utc),
            "upstream_project": ev.upstream_project,
            "upstream_label": ev.upstream_label,
            "canonical_binomial": ev.canonical_binomial,
            "canonical_binomial_is_derived": int(ev.canonical_binomial_is_derived),
            "canonical_is_binomial": int(ev.canonical_is_binomial),
            "display_common_name": ev.display_common_name,
            "upstream_confidence": ev.upstream_confidence,
            "upstream_confidence_note": ev.upstream_confidence_note,
            "upstream_image_id": ev.upstream_image_id,
            "upstream_image_id_resolvable": int(ev.upstream_image_id_resolvable),
            "event_time_utc": _iso(ev.event_time_utc),
            "event_time_is_capture_time": int(ev.event_time_is_capture_time),
            "capture_time_utc": _iso(ev.capture_time_utc),
            "capture_time_source": ev.capture_time_source,
            "capture_time_tz_validated": int(ev.capture_time_tz_validated),
            "capture_time_note": ev.capture_time_note,
            "bioclip_ok": int(ev.bioclip_ok),
            "bioclip_model": ev.bioclip_model,
            "bioclip_topk_json": _dumps(ev.bioclip_topk),
            "bioclip_error": ev.bioclip_error,
            "bioclip_top1_kingdom": ev.bioclip_top1_kingdom,
            "bioclip_top1_class": ev.bioclip_top1_class,
            "bioclip_top1_order": ev.bioclip_top1_order,
            "bioclip_top1_family": ev.bioclip_top1_family,
            "bioclip_top1_genus": ev.bioclip_top1_genus,
            "agreement_flag": ev.agreement_flag,
            "agreement_rationale": ev.agreement_rationale,
            "cross_check_status": ev.cross_check_status,
            "resolution_basis": ev.resolution_basis,
            "matched_rank": ev.matched_rank,
            "taxonomic_distance": ev.taxonomic_distance,
            "vlm_ok": int(ev.vlm_ok),
            "vlm_model": ev.vlm_model,
            "vlm_description": ev.vlm_description,
            "vlm_error": ev.vlm_error,
            "weather_category": ev.weather_category,
            "weather_code_raw": ev.weather_code_raw,
            "temperature_c": ev.temperature_c,
            "precipitation_mm": ev.precipitation_mm,
            "weather_match_source": ev.weather_match_source,
            "weather_used_send_fallback": int(ev.weather_used_send_fallback),
            "original_image_path": ev.original_image_path,
            "boxed_image_path": ev.boxed_image_path,
            "images_present": ev.images_present,
            "provenance_json": _dumps(ev.field_provenance) or "{}",
            "parse_warnings_json": _dumps(ev.parse_warnings),
            "created_at_utc": _iso(created),
            "alias_table_sha256": ev.alias_table_sha256,
            "source_auth_results_json": ev.source_auth_results_json,
            "source_from": ev.source_from,
            "source_return_path": ev.source_return_path,
            "source_received_shape": ev.source_received_shape,
        }
        placeholders = ", ".join(f":{c}" for c in _INSERT_COLUMNS)
        columns = ", ".join(_INSERT_COLUMNS)
        with self._conn:
            self._conn.execute(
                f"INSERT INTO detection_events ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(source_message_id) DO NOTHING",
                values,
            )
        row = self._conn.execute(
            "SELECT id FROM detection_events WHERE source_message_id = ?",
            (ev.source_message_id,),
        ).fetchone()
        return int(row["id"])

    def set_capture_time(self, event_id: int, read) -> None:
        """Persist a decoded capture time for an existing row (the backfill path).

        Writes ONLY the capture_time_* columns — ``event_time_utc`` is never
        touched, so the send time and the divergence between the two survive
        intact as evidence.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE detection_events SET capture_time_utc = ?, capture_time_source = ?, "
                "capture_time_tz_validated = ?, capture_time_note = ? WHERE id = ?",
                (_iso(read.capture_time_utc), read.source,
                 int(read.tz_validated), read.note, event_id),
            )

    def backfill_source_provenance(self, message_id: str, *,
                                   auth_results_json: Optional[str],
                                   from_header: Optional[str],
                                   return_path: Optional[str],
                                   received_shape: Optional[str]) -> bool:
        """Record delivery provenance on an already-stored row. True if it matched.

        RECOVERED EVIDENCE, not an operator assertion — and the distinction is
        why this back-fill is allowed where the alias-table one was refused.
        `alias_table_sha256` is deliberately left NULL on old rows because nobody
        knows which table resolved them; writing one would invent an attribution.
        These values were fetched from the mailbox that still holds the source
        messages and checked against the receiving MX's own verdict.
        Recording them restores a fact that the
        repository previously discarded; it does not claim one.

        Writes ONLY the four source_* columns. No verdict column is touched, so
        the corpus digest cannot move.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE detection_events SET source_auth_results_json = ?, "
                "source_from = ?, source_return_path = ?, source_received_shape = ? "
                "WHERE source_message_id = ?",
                (auth_results_json, from_header, return_path, received_shape,
                 message_id),
            )
        return cur.rowcount > 0

    def iter_for_backfill(self) -> list[tuple[int, Optional[str], Optional[datetime]]]:
        """(id, original_image_path, event_time_utc) for every row — the input the
        capture-time backfill needs. Read-only; no filenames are touched on disk."""
        rows = self._conn.execute(
            "SELECT id, original_image_path, event_time_utc FROM detection_events "
            "ORDER BY id"
        ).fetchall()
        return [(int(r["id"]), r["original_image_path"], _parse_dt(r["event_time_utc"]))
                for r in rows]

    def set_weather(self, event_id: int, read) -> None:
        """Persist a weather-enrichment result for an existing row (backfill path).

        Writes ONLY the weather_* columns — additive enrichment, nothing else
        touched. ``read`` is a ``WeatherRead`` (duck-typed here to avoid the DAL
        importing enrichment)."""
        with self._conn:
            self._conn.execute(
                "UPDATE detection_events SET weather_category = ?, weather_code_raw = ?, "
                "temperature_c = ?, precipitation_mm = ?, weather_match_source = ?, "
                "weather_used_send_fallback = ? WHERE id = ?",
                (read.category, read.code_raw, read.temperature_c, read.precipitation_mm,
                 read.match_source, int(read.used_send_fallback), event_id))

    def set_crosscheck(self, event_id: int, check, lineage=None) -> None:
        """Persist a recomputed cross-check verdict for an existing row.

        Writes ONLY the verdict and its audit columns (and the top-1 lineage when
        one was recovered). The upstream label, the canonical binomial and BioCLIP's
        stored top-k are never touched: the recompute re-reads the same evidence and
        re-decides, it does not re-ingest. ``check`` is a ``CrossCheck`` (duck-typed
        here so the DAL keeps not importing enrichment)."""
        with self._conn:
            self._conn.execute(
                "UPDATE detection_events SET agreement_flag = ?, agreement_rationale = ?, "
                "cross_check_status = ?, resolution_basis = ?, matched_rank = ?, "
                "taxonomic_distance = ?, bioclip_top1_kingdom = ?, bioclip_top1_class = ?, "
                "bioclip_top1_order = ?, bioclip_top1_family = ?, bioclip_top1_genus = ? "
                "WHERE id = ?",
                (check.flag, check.rationale, check.status, check.resolution_basis,
                 check.matched_rank, check.taxonomic_distance,
                 getattr(lineage, "kingdom", None), getattr(lineage, "class_name", None),
                 getattr(lineage, "order", None), getattr(lineage, "family", None),
                 getattr(lineage, "genus", None), event_id))

    def set_crop(self, event_id: int, decision, tt, detector, taxo=None,
                 lineage=None) -> None:
        """Persist a crop decision and its provenance for an existing row.

        Writes ONLY the crop_*, detector_*, tt_*, localisation_* and
        bioclip_crop_* columns. The existing bioclip_* full-frame read, the
        agreement verdict and every cross-check audit column are untouched, so
        no corroboration verdict changes when this runs — that is a Phase 3
        decision, not a side effect of producing a better crop.

        Arguments are duck-typed (``CropDecision``, ``TrapTrackerRead``,
        ``DetectorRead``, ``TaxonomicRead``) so the DAL keeps not importing
        enrichment or crop.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE detection_events SET "
                "crop_status = ?, crop_basis = ?, crop_error = ?, "
                "crop_box_json = ?, crop_pad_frac = ?, "
                "detector_provider = ?, detector_model = ?, detector_box_json = ?, "
                "detector_score = ?, detector_candidate_count = ?, detector_error = ?, "
                "tt_box_json = ?, tt_box_recovered = ?, tt_box_self_check = ?, "
                "tt_box_clipped = ?, tt_banner_ocr_label = ?, "
                "tt_banner_ocr_confidence = ?, tt_banner_ocr_score = ?, "
                "tt_banner_basis = ?, tt_label_mismatch = ?, frame_detection_count = ?, "
                "localisation_iou = ?, localisation_agreement = ?, "
                "bioclip_crop_ok = ?, bioclip_crop_model = ?, bioclip_crop_topk_json = ?, "
                "bioclip_crop_error = ?, bioclip_crop_top1_kingdom = ?, "
                "bioclip_crop_top1_class = ?, bioclip_crop_top1_order = ?, "
                "bioclip_crop_top1_family = ?, bioclip_crop_top1_genus = ? "
                "WHERE id = ?",
                (decision.status, decision.basis, decision.error,
                 _dumps(list(decision.crop_box) if decision.crop_box else None),
                 decision.pad_frac,
                 detector.provider, detector.model_name,
                 _dumps(list(decision.detector_box) if decision.detector_box else None),
                 decision.detector_score, decision.detector_candidate_count,
                 detector.error,
                 _dumps(list(tt.box) if tt.box else None),
                 int(tt.recovered), tt.self_check, int(tt.clipped),
                 tt.banner.label, tt.banner.confidence, tt.banner.score,
                 tt.resolution_basis, int(tt.label_mismatch), tt.frame_detection_count,
                 decision.localisation_iou, decision.localisation_agreement,
                 int(getattr(taxo, "ok", False)) if taxo else None,
                 getattr(taxo, "model_name", None) if taxo else None,
                 _dumps(getattr(taxo, "topk", None) or None) if taxo else None,
                 getattr(taxo, "error", None) if taxo else None,
                 getattr(lineage, "kingdom", None), getattr(lineage, "class_name", None),
                 getattr(lineage, "order", None), getattr(lineage, "family", None),
                 getattr(lineage, "genus", None),
                 event_id))

    def set_crop_crosscheck(self, event_id: int, check, lineage=None) -> None:
        """Persist the CROPPED read's cross-check into the parallel audit trail.

        Writes only the six ``*_crop`` verdict columns and the five
        ``bioclip_crop_top1_*`` lineage columns. The published verdict
        (``agreement_flag`` and its audit trail) is never touched: Phase 3 Option B
        reports the two reads side by side as a finding, it does not restate the
        number. ``check`` is a ``CrossCheck``, duck-typed so the DAL keeps not
        importing enrichment.
        """
        with self._conn:
            self._conn.execute(
                "UPDATE detection_events SET agreement_flag_crop = ?, "
                "agreement_rationale_crop = ?, cross_check_status_crop = ?, "
                "resolution_basis_crop = ?, matched_rank_crop = ?, "
                "taxonomic_distance_crop = ?, bioclip_crop_top1_kingdom = ?, "
                "bioclip_crop_top1_class = ?, bioclip_crop_top1_order = ?, "
                "bioclip_crop_top1_family = ?, bioclip_crop_top1_genus = ? "
                "WHERE id = ?",
                (check.flag, check.rationale, check.status, check.resolution_basis,
                 check.matched_rank, check.taxonomic_distance,
                 getattr(lineage, "kingdom", None), getattr(lineage, "class_name", None),
                 getattr(lineage, "order", None), getattr(lineage, "family", None),
                 getattr(lineage, "genus", None), event_id))

    def iter_for_crop_crosscheck(self):
        """``(id, upstream_label, crop_ok, crop_error, crop_topk, published_flag,
        crop_status)`` per row — everything the cropped-read comparison needs.

        The published flag rides along so the four-cell crosstab can be built in one
        pass without a second query, and so the command can prove it changed none of
        them."""
        rows = self._conn.execute(
            "SELECT id, upstream_label, bioclip_crop_ok, bioclip_crop_error, "
            "bioclip_crop_topk_json, agreement_flag, crop_status "
            "FROM detection_events ORDER BY id").fetchall()
        return [(int(r["id"]), r["upstream_label"], bool(r["bioclip_crop_ok"]),
                 r["bioclip_crop_error"], _loads(r["bioclip_crop_topk_json"]) or [],
                 r["agreement_flag"], r["crop_status"]) for r in rows]

    def iter_for_crop_recompute(self):
        """``(id, upstream_label, upstream_confidence, original_path, boxed_path)``
        per row — the crop backfill's whole input. Ordered by id."""
        rows = self._conn.execute(
            "SELECT id, upstream_label, upstream_confidence, original_image_path, "
            "boxed_image_path FROM detection_events ORDER BY id").fetchall()
        return [(int(r["id"]), r["upstream_label"], r["upstream_confidence"],
                 r["original_image_path"], r["boxed_image_path"]) for r in rows]

    def crop_status_counts(self) -> dict:
        """``{crop_status: n}`` including NULL as 'not_computed' — the conservation
        check's input, so a dropped event is visible rather than inferred."""
        rows = self._conn.execute(
            "SELECT COALESCE(crop_status, 'not_computed') AS s, COUNT(*) AS n "
            "FROM detection_events GROUP BY s").fetchall()
        return {r["s"]: int(r["n"]) for r in rows}

    def iter_for_crosscheck_recompute(self):
        """``(id, upstream_label, bioclip_ok, bioclip_error, topk, old_flag)`` per row.

        The recompute's whole input. ``topk`` comes back as the stored
        ``[[taxon, score], ...]``; the old flag rides along so the caller can
        regression-check every verdict change without a second query."""
        rows = self._conn.execute(
            "SELECT id, upstream_label, bioclip_ok, bioclip_error, bioclip_topk_json, "
            "agreement_flag FROM detection_events ORDER BY id").fetchall()
        return [(int(r["id"]), r["upstream_label"], bool(r["bioclip_ok"]),
                 r["bioclip_error"], _loads(r["bioclip_topk_json"]) or [],
                 r["agreement_flag"]) for r in rows]

    def iter_for_weather_backfill(self):
        """Full ``PersistedEvent`` per row — the weather backfill needs the capture
        time, source, and send time to choose the match basis. Ordered by id."""
        rows = self._conn.execute(
            "SELECT * FROM detection_events ORDER BY id").fetchall()
        return [self._row_to_event(r) for r in rows]

    # ------- weather cache (implements the enrichment WeatherCache protocol) -----
    @staticmethod
    def _coord(v: float) -> str:
        return f"{float(v):.4f}"                     # stable text key, no float drift

    def get_day(self, latitude: float, longitude: float, day) -> Optional[list[dict]]:
        row = self._conn.execute(
            "SELECT hourly_json FROM weather_cache WHERE latitude=? AND longitude=? AND day=?",
            (self._coord(latitude), self._coord(longitude), day.isoformat()),
        ).fetchone()
        return json.loads(row["hourly_json"]) if row else None

    def put_day(self, latitude: float, longitude: float, day, records: list[dict]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO weather_cache "
                "(latitude, longitude, day, hourly_json, fetched_at_utc) VALUES (?,?,?,?,?)",
                (self._coord(latitude), self._coord(longitude), day.isoformat(),
                 json.dumps(records), datetime.now(timezone.utc).isoformat()))

    def weather_base_rate(self, start_utc: datetime, end_utc: datetime) -> dict[str, int]:
        """Category → number of CACHED HOURS in the window's UTC-date span.

        The exposure denominator for the weather view: how much of the window each
        condition actually occupied, so detection shares can be read against what
        was available rather than in isolation. Uses the pure mapping table (a
        lookup, not enrichment logic), imported locally to keep the DAL's module
        surface clean. Empty when the weather cache has not been populated."""
        from ..enrichment import weather_codes

        rows = self._conn.execute(
            "SELECT hourly_json FROM weather_cache WHERE day >= ? AND day <= ?",
            (start_utc.date().isoformat(), end_utc.date().isoformat()),
        ).fetchall()
        out: dict[str, int] = {}
        for (hourly_json,) in rows:
            for rec in json.loads(hourly_json):
                cat, _ = weather_codes.categorize(rec.get("weather_code"))
                out[cat] = out.get(cat, 0) + 1
        return out

    def store_embedding(self, event_id: int, embedding: list[float]) -> None:
        """Persist a BioCLIP embedding. Caller passes a list[float]; the JSON
        storage shape is internal to this method (Fix b)."""
        with self._conn:
            self._conn.execute(
                "UPDATE detection_events SET bioclip_embedding_json = ? WHERE id = ?",
                (json.dumps(embedding), event_id),
            )

    # ------------------------------------------------------------------ read
    def exists_message_id(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM detection_events WHERE source_message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
        return row is not None

    def get_embedding(self, event_id: int) -> Optional[list[float]]:
        row = self._conn.execute(
            "SELECT bioclip_embedding_json FROM detection_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None or row["bioclip_embedding_json"] is None:
            return None
        return json.loads(row["bioclip_embedding_json"])

    def query(
        self,
        *,
        binomial: str,
        start_utc: datetime,
        end_utc: datetime,
        camera: Optional[str] = None,   # forward hook — never populated by email source
    ) -> list[PersistedEvent]:
        """Return events for a binomial within [start_utc, end_utc], inclusive.

        Bounded by the EFFECTIVE time (capture time when decoded, else send time)
        so a row is selected by the same clock the report then counts it under.
        Uses the ``canonical_binomial`` index (Decision 5 join key). ``camera``
        is accepted for interface stability but ignored: the email source never
        carries camera identity and there is no camera column (Watch-item §8).
        """
        rows = self._conn.execute(
            "SELECT * FROM detection_events "
            "WHERE canonical_binomial = ? "
            f"AND {_EFFECTIVE_TIME} IS NOT NULL "
            f"AND {_EFFECTIVE_TIME} >= ? AND {_EFFECTIVE_TIME} <= ? "
            f"ORDER BY {_EFFECTIVE_TIME} ASC",
            (binomial, start_utc.isoformat(), end_utc.isoformat()),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def query_all(self, *, start_utc: datetime, end_utc: datetime) -> list[PersistedEvent]:
        """Every DATED, in-window detection regardless of species key — the basis
        for the all-species summary.

        Unlike :meth:`query_others`, this includes the reserved ``unmapped:`` and
        ``nonbio:`` keys: the all-species total counts every event, so the species
        breakdown reconciles against it (per-species reports exclude unmapped and
        disclose it separately — a deliberate difference, stated in the report).
        """
        rows = self._conn.execute(
            "SELECT * FROM detection_events "
            f"WHERE {_EFFECTIVE_TIME} IS NOT NULL "
            f"AND {_EFFECTIVE_TIME} >= ? AND {_EFFECTIVE_TIME} <= ? "
            f"ORDER BY {_EFFECTIVE_TIME} ASC",
            (start_utc.isoformat(), end_utc.isoformat()),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def count_undated_all(self) -> int:
        """Rows of ANY species with no usable timestamp — excluded from the
        all-species day figures, disclosed separately (uncertainty, not absence)."""
        row = self._conn.execute(
            "SELECT count(*) FROM detection_events WHERE " + _EFFECTIVE_TIME + " IS NULL"
        ).fetchone()
        return int(row[0])

    def query_others(
        self, *, exclude_binomial: str, start_utc: datetime, end_utc: datetime
    ) -> list[PersistedEvent]:
        """Mapped detections of OTHER species in the window (non-target activity).

        Excludes the report's own species and the reserved ``unmapped:`` keys —
        unmapped rows carry no species identity to attribute activity to, and are
        disclosed by their own section instead of being silently folded in here.
        Bounded by the same effective time as :meth:`query`.
        """
        rows = self._conn.execute(
            "SELECT * FROM detection_events "
            "WHERE canonical_binomial IS NOT NULL "
            "AND canonical_binomial != ? "
            "AND canonical_binomial NOT LIKE 'unmapped:%' "
            f"AND {_EFFECTIVE_TIME} IS NOT NULL "
            f"AND {_EFFECTIVE_TIME} >= ? AND {_EFFECTIVE_TIME} <= ? "
            f"ORDER BY canonical_binomial, {_EFFECTIVE_TIME} ASC",
            (exclude_binomial, start_utc.isoformat(), end_utc.isoformat()),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def data_span(self) -> tuple[Optional[datetime], Optional[datetime]]:
        """Earliest and latest effective time held, across ALL species.

        Lets a report tell a day with no detections apart from a day the dataset
        does not cover at all: a window may extend past the last record, and those
        trailing days are absence of DATA, not absence of animals.
        """
        row = self._conn.execute(
            f"SELECT min({_EFFECTIVE_TIME}), max({_EFFECTIVE_TIME}) FROM detection_events "
            f"WHERE {_EFFECTIVE_TIME} IS NOT NULL"
        ).fetchone()
        if row is None:
            return None, None
        return _parse_dt(row[0]), _parse_dt(row[1])

    def send_time_span(self) -> tuple[Optional[datetime], Optional[datetime]]:
        """Earliest and latest SEND time held (delivered alerts), across all species.

        This is the operational-coverage bound: a delivered alert proves the system
        was up at that instant, so the last send is the last point we can CONFIRM
        the system was operational. Days after it are status-unknown, not zero
        activity — even a backlog-flushed capture (dated earlier) proves the system
        delivered on its send day, which effective/capture time would miss."""
        row = self._conn.execute(
            "SELECT min(event_time_utc), max(event_time_utc) FROM detection_events "
            "WHERE event_time_utc IS NOT NULL"
        ).fetchone()
        if row is None:
            return None, None
        return _parse_dt(row[0]), _parse_dt(row[1])

    def delivery_timeline(
        self, *, start_utc: datetime, end_utc: datetime,
        binomial: Optional[str] = None,
    ) -> list[tuple[datetime, Optional[datetime]]]:
        """``(send_time, capture_time)`` ordered by send time.

        With ``binomial=None`` this spans EVERY species, which is the only basis on
        which a delivery outage can be judged: the mailbox stops for all species at
        once, so one species falling quiet is not an outage. Passing a binomial
        narrows it to that species' own stream — used to scope recovery counts and
        species-absence to the report's subject rather than borrowing the dataset's.
        """
        params: list = [start_utc.isoformat(), end_utc.isoformat()]
        species_clause = ""
        if binomial is not None:
            species_clause = "AND canonical_binomial = ? "
            params.append(binomial)
        rows = self._conn.execute(
            "SELECT event_time_utc, capture_time_utc FROM detection_events "
            "WHERE event_time_utc IS NOT NULL "
            f"AND {_EFFECTIVE_TIME} >= ? AND {_EFFECTIVE_TIME} <= ? "
            f"{species_clause}"
            "ORDER BY event_time_utc ASC",
            params,
        ).fetchall()
        return [(_parse_dt(r["event_time_utc"]), _parse_dt(r["capture_time_utc"]))
                for r in rows]

    def query_unmapped(self, *, start_utc: datetime, end_utc: datetime) -> list[PersistedEvent]:
        """Return events with a reserved ``unmapped:`` key (unmappable or
        label-less detections). Dated rows are bounded by the window; UNDATED
        rows are always included (a missing timestamp must not make them vanish
        too), so the report can disclose them rather than let them slip through
        the gap between the species query and the window filter.
        """
        rows = self._conn.execute(
            "SELECT * FROM detection_events "
            "WHERE canonical_binomial LIKE 'unmapped:%' "
            f"AND ({_EFFECTIVE_TIME} IS NULL "
            f"     OR ({_EFFECTIVE_TIME} >= ? AND {_EFFECTIVE_TIME} <= ?)) "
            f"ORDER BY {_EFFECTIVE_TIME} IS NULL, {_EFFECTIVE_TIME} ASC",
            (start_utc.isoformat(), end_utc.isoformat()),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def count_undated(self, binomial: str) -> int:
        """Count rows for a canonical key that have NO usable timestamp.

        These are excluded from windowed day-by-day figures (they cannot be
        placed on a day), so the report discloses them separately instead of
        letting them drop silently out of the window query.
        """
        row = self._conn.execute(
            "SELECT count(*) FROM detection_events "
            f"WHERE canonical_binomial = ? AND {_EFFECTIVE_TIME} IS NULL",
            (binomial,),
        ).fetchone()
        return int(row[0])

    def earliest_event_time(self, binomial: str) -> Optional[datetime]:
        """Earliest DATED (non-NULL event_time) row for a canonical key, or None
        when the key has no dated rows at all. Lets the report tell a
        period-over-period baseline that PREDATES our data ("no records that far
        back") apart from one that is merely empty ("0 events in that period") —
        two honest but distinct 'no baseline' reasons, never a divide-by-zero.
        """
        row = self._conn.execute(
            f"SELECT min({_EFFECTIVE_TIME}) FROM detection_events "
            f"WHERE canonical_binomial = ? AND {_EFFECTIVE_TIME} IS NOT NULL",
            (binomial,),
        ).fetchone()
        return _parse_dt(row[0]) if row and row[0] else None

    def species_counts(self) -> dict[str, int]:
        """``canonical_binomial`` -> total row count, across all detections.

        A read-only aggregate for UI annotation ("which classes the dataset
        actually contains") — not part of the report path.
        """
        rows = self._conn.execute(
            "SELECT canonical_binomial, count(*) FROM detection_events "
            "WHERE canonical_binomial IS NOT NULL GROUP BY canonical_binomial"
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def close(self) -> None:
        self._conn.close()

    # --------------------------------------------------------------- mapping
    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> PersistedEvent:
        return PersistedEvent(
            id=r["id"],
            source_type=r["source_type"],
            source_message_id=r["source_message_id"],
            ingested_at_utc=_parse_dt(r["ingested_at_utc"]),
            upstream_project=r["upstream_project"],
            upstream_label=r["upstream_label"],
            canonical_binomial=r["canonical_binomial"],
            canonical_binomial_is_derived=bool(r["canonical_binomial_is_derived"]),
            canonical_is_binomial=bool(r["canonical_is_binomial"]),
            display_common_name=r["display_common_name"],
            upstream_confidence=r["upstream_confidence"],
            upstream_confidence_note=r["upstream_confidence_note"],
            upstream_image_id=r["upstream_image_id"],
            upstream_image_id_resolvable=bool(r["upstream_image_id_resolvable"]),
            event_time_utc=_parse_dt(r["event_time_utc"]),
            event_time_is_capture_time=bool(r["event_time_is_capture_time"]),
            capture_time_utc=_parse_dt(r["capture_time_utc"]),
            capture_time_source=r["capture_time_source"] or "none",
            capture_time_tz_validated=bool(r["capture_time_tz_validated"]),
            capture_time_note=r["capture_time_note"],
            bioclip_ok=bool(r["bioclip_ok"]),
            bioclip_model=r["bioclip_model"],
            bioclip_topk=_loads(r["bioclip_topk_json"]),
            bioclip_error=r["bioclip_error"],
            bioclip_top1_kingdom=_col(r, "bioclip_top1_kingdom"),
            bioclip_top1_class=_col(r, "bioclip_top1_class"),
            bioclip_top1_order=_col(r, "bioclip_top1_order"),
            bioclip_top1_family=_col(r, "bioclip_top1_family"),
            bioclip_top1_genus=_col(r, "bioclip_top1_genus"),
            agreement_flag=r["agreement_flag"],
            agreement_rationale=r["agreement_rationale"],
            cross_check_status=_col(r, "cross_check_status") or "evaluable",
            resolution_basis=_col(r, "resolution_basis"),
            matched_rank=_col(r, "matched_rank"),
            taxonomic_distance=_col(r, "taxonomic_distance"),
            vlm_ok=bool(r["vlm_ok"]),
            vlm_model=r["vlm_model"],
            vlm_description=r["vlm_description"],
            vlm_error=r["vlm_error"],
            weather_category=r["weather_category"],
            weather_code_raw=r["weather_code_raw"],
            temperature_c=r["temperature_c"],
            precipitation_mm=r["precipitation_mm"],
            weather_match_source=r["weather_match_source"],
            weather_used_send_fallback=bool(r["weather_used_send_fallback"]),
            original_image_path=r["original_image_path"],
            boxed_image_path=r["boxed_image_path"],
            images_present=r["images_present"],
            field_provenance=_loads(r["provenance_json"]) or {},
            parse_warnings=_loads(r["parse_warnings_json"]) or [],
            created_at_utc=_parse_dt(r["created_at_utc"]),
            alias_table_sha256=_col(r, "alias_table_sha256"),
            source_auth_results_json=_col(r, "source_auth_results_json"),
            source_from=_col(r, "source_from"),
            source_return_path=_col(r, "source_return_path"),
            source_received_shape=_col(r, "source_received_shape"),
        )
