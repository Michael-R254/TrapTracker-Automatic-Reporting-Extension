PRAGMA journal_mode=WAL;   -- agents read concurrently while ingestion writes (brief §3)

CREATE TABLE IF NOT EXISTS detection_events (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    -- provenance / identity
    source_type                 TEXT NOT NULL,          -- "email"
    source_message_id           TEXT NOT NULL UNIQUE,   -- idempotency key
    ingested_at_utc             TEXT NOT NULL,          -- ISO-8601
    -- parsed upstream fields
    upstream_project            TEXT,
    upstream_label              TEXT,                   -- raw class token, AUTHORITATIVE, never overwritten
    -- species identity (Decision 5): binomial is canonical; both derived from upstream_label via the alias table
    canonical_binomial          TEXT,                   -- DERIVED via alias table; the query/join key (indexed below).
                                                        --   Holds a scientific binomial, OR a reserved 'nonbio:<Token>'
                                                        --   key for classes without one (Person/Car/CalibrationPole).
    canonical_binomial_is_derived INTEGER NOT NULL DEFAULT 1,
    canonical_is_binomial       INTEGER NOT NULL DEFAULT 1,  -- 0 for reserved non-binomial keys (no species-hood claimed)
    display_common_name         TEXT,                   -- DERIVED convenience for readable reports; nullable
    upstream_confidence         REAL,
    upstream_confidence_note    TEXT DEFAULT '2dp_rounded_best_per_label',
    upstream_image_id           INTEGER,
    upstream_image_id_resolvable INTEGER NOT NULL DEFAULT 0,  -- FK into inaccessible DB
    event_time_utc              TEXT,                   -- indexed below. ALERT-SEND time (Decision 3).
                                                        --   NEVER overwritten by capture time: the divergence
                                                        --   between the two is itself evidence.
    event_time_is_capture_time  INTEGER NOT NULL DEFAULT 0,   -- it is SEND time
    -- capture time (Decision 3, amended): decoded from the attachment FILENAME's
    -- Reolink block D (Europe/London local -> UTC via zoneinfo). Stored ALONGSIDE
    -- event_time_utc, never in place of it. Preferred for day assignment when present.
    capture_time_utc            TEXT,                   -- indexed below; NULL when undecodable
    capture_time_source         TEXT NOT NULL DEFAULT 'none',  -- filename_block_d | send_time_fallback | none
    capture_time_tz_validated   INTEGER NOT NULL DEFAULT 0,    -- 1 only inside the validated BST window
    capture_time_note           TEXT,                   -- why it degraded / why the tz is unvalidated
    -- enrichment: taxonomic cross-check (BioCLIP)
    bioclip_ok                  INTEGER NOT NULL DEFAULT 0,
    bioclip_model               TEXT,
    bioclip_topk_json           TEXT,                   -- [[taxon, score], ...]
    bioclip_embedding_json      TEXT,                   -- v1 storage shape ONLY; repository-internal (Fix b) — nothing outside the DAL may assume JSON-in-SQLite
    bioclip_error               TEXT,
    -- Full taxonomic hierarchy of the top-1 candidate, as BioCLIP reported it.
    -- Stored so a disagreement can be MEASURED (one genus away, or another kingdom)
    -- rather than merely recorded. Never inferred from the binomial: NULL here means
    -- the row predates hierarchy capture, and its distance is 'undetermined'.
    bioclip_top1_kingdom        TEXT,
    bioclip_top1_class          TEXT,
    bioclip_top1_order          TEXT,
    bioclip_top1_family         TEXT,
    bioclip_top1_genus          TEXT,
    -- enrichment: cross-check verdict (Decision 2, species-level and two-state)
    agreement_flag              TEXT,                   -- agree | disagree | not_evaluable
    agreement_rationale         TEXT,
    -- Whether a comparison was POSSIBLE at all. 'not_evaluable' covers a class
    -- outside BioCLIP's Tree of Life (Person/Car/CalibrationPole), a failed BioCLIP
    -- run, and a missing/unmappable upstream label: scoring those as 'disagree'
    -- would assert a taxonomic conflict that was never tested. The binary is
    -- reported over 'evaluable' rows, with these as an explicit excluded
    -- denominator. Deliberately redundant with agreement_flag so no count can
    -- silently drop a row.
    cross_check_status          TEXT NOT NULL DEFAULT 'evaluable',  -- evaluable | not_evaluable
    -- Audit trail for the verdict. resolution_basis names HOW it was reached;
    -- taxonomic_distance says how far apart the two identifications are. These are
    -- what replaced the old 'indeterminate' bucket: the same rows, now carrying
    -- evidence for WHY they disagree instead of being an unexplained pile.
    resolution_basis            TEXT,                   -- see enrichment.agreement.RESOLUTION_BASES
    matched_rank                TEXT,                   -- species | none
    taxonomic_distance          TEXT,                   -- see species.taxonomy.DISTANCES
    -- enrichment: description (VLM)
    vlm_ok                      INTEGER NOT NULL DEFAULT 0,
    vlm_model                   TEXT,
    vlm_description             TEXT,
    vlm_error                   TEXT,
    -- images (OUR paths, in OUR image_store)
    original_image_path         TEXT,
    boxed_image_path            TEXT,
    images_present              TEXT NOT NULL,          -- both | original_only | boxed_only | none
    -- enrichment: weather correlation (Open-Meteo archive, matched to capture_time)
    -- Weather at the CAMERA's location, not the animal's; additive enrichment that
    -- never strengthens or weakens any existing claim. Raw code kept so the
    -- categorisation (weather_codes.py) is reversible and reviewable.
    weather_category            TEXT,                   -- sunny|cloudy|rainy|unknown|unavailable
    weather_code_raw            INTEGER,                -- WMO code as returned
    temperature_c               REAL,
    precipitation_mm            REAL,
    weather_match_source        TEXT,                   -- how it was matched, or why unavailable
    weather_used_send_fallback  INTEGER NOT NULL DEFAULT 0,  -- 1 = matched to send time (no capture time)
    -- crop path (Phase 2c). PARALLEL to the bioclip_* columns above, never a
    -- replacement: agreement.py keeps reading the full-frame read, so filling
    -- these changes no corroboration verdict. NULL means "not computed yet",
    -- which is visible rather than a silently defaulted status.
    crop_status                 TEXT,                   -- the six-value vocabulary; see crop.base
    crop_basis                  TEXT,                   -- HOW that status was reached
    crop_error                  TEXT,
    crop_box_json               TEXT,                   -- final crop rect on the CLEAN frame
    crop_pad_frac               REAL,                   -- padding ACTUALLY applied (less at a frame edge)
    -- the independent detector that produced the box (never TrapTracker's)
    detector_provider           TEXT,
    detector_model              TEXT,
    detector_box_json           TEXT,                   -- chosen box before padding
    detector_score              REAL,
    detector_candidate_count    INTEGER,                -- the ambiguity denominator
    detector_error              TEXT,
    -- TrapTracker's recovered rectangle: COMPARISON ONLY, never crop geometry
    tt_box_json                 TEXT,
    tt_box_recovered            INTEGER,
    tt_box_self_check           TEXT,                   -- pass | fail; only 'pass' corroborates
    tt_box_clipped              INTEGER,
    tt_banner_ocr_label         TEXT,
    tt_banner_ocr_confidence    REAL,                   -- set only when the digits were actually ranked (conf_broke_tie)
    tt_banner_ocr_score         REAL,
    tt_banner_basis             TEXT,                   -- how the banner was identified, or why not
    tt_label_mismatch           INTEGER,                -- banner disagrees with upstream_label
    frame_detection_count       INTEGER,                -- rendered detections in the frame
    -- do the two agree on WHERE the animal is (never on WHAT it is)
    localisation_iou            REAL,
    localisation_agreement      TEXT,                   -- agree | disagree | not_comparable
    -- BioCLIP re-run on the crop, stored ALONGSIDE the full-frame read
    bioclip_crop_ok             INTEGER,
    bioclip_crop_model          TEXT,
    bioclip_crop_topk_json      TEXT,
    bioclip_crop_error          TEXT,
    bioclip_crop_top1_kingdom   TEXT,
    bioclip_crop_top1_class     TEXT,
    bioclip_crop_top1_order     TEXT,
    bioclip_crop_top1_family    TEXT,
    bioclip_crop_top1_genus     TEXT,
    -- the cropped-read cross-check: a PARALLEL audit trail, never the published
    -- verdict. Mirrors the six unsuffixed columns above one-for-one so the two
    -- reads can be crosstabbed directly. Phase 3 Option B reports the comparison
    -- as a finding; agreement_flag stays the full-frame verdict.
    agreement_flag_crop         TEXT,                   -- agree | disagree | not_evaluable
    agreement_rationale_crop    TEXT,
    cross_check_status_crop     TEXT,
    resolution_basis_crop       TEXT,
    matched_rank_crop           TEXT,
    taxonomic_distance_crop     TEXT,
    -- honesty layer
    provenance_json             TEXT NOT NULL,          -- serialized field_provenance
    parse_warnings_json         TEXT,
    created_at_utc              TEXT NOT NULL,
    -- WHICH species table resolved canonical_binomial. Species resolution happens
    -- at WRITE time and is stored, so the table in force at ingest is baked into
    -- the row; with per-project tables, "which one" is a real question. NULL on
    -- rows written before this was recorded — true, and better than a guess.
    -- Also listed in migrations._ADDED_COLUMNS: this clause serves a FRESH
    -- database, that list serves an existing one, and both are needed.
    alias_table_sha256          TEXT,
    -- Sender provenance: what the delivery envelope said, recorded verbatim and
    -- never judged here. source_auth_results_json is EVERY Authentication-Results
    -- header as a JSON array; NULL means the header was ABSENT, which a pass/fail
    -- column could not tell apart from present-and-failing. Also listed in
    -- migrations._ADDED_COLUMNS: this clause serves a FRESH database, that list
    -- serves an existing one, and both are needed.
    source_auth_results_json    TEXT,
    source_from                 TEXT,
    source_return_path          TEXT,
    source_received_shape       TEXT
);

-- Per-date cache of Open-Meteo hourly responses. Historical data does not change,
-- so a cached date is never re-fetched. Keyed by (rounded lat/lon, date).
CREATE TABLE IF NOT EXISTS weather_cache (
    latitude    TEXT NOT NULL,          -- stored as text to avoid float-key drift
    longitude   TEXT NOT NULL,
    day         TEXT NOT NULL,          -- ISO date
    hourly_json TEXT NOT NULL,          -- [{hour_utc, weather_code, temperature_c, precipitation_mm}, ...]
    fetched_at_utc TEXT NOT NULL,
    PRIMARY KEY (latitude, longitude, day)
);

-- Which PROJECT owns this database. One row, or none at all on a database that
-- predates the project layer. Absent is a real, distinguishable state: a fresh
-- database is stamped silently, a legacy one is adopted with a notice, and a
-- database stamped for a DIFFERENT project is refused. Nothing is back-filled.
CREATE TABLE IF NOT EXISTS project_meta (
    project_id     TEXT NOT NULL,       -- the owning project's UUID
    schema_version INTEGER NOT NULL,
    created_utc    TEXT NOT NULL,
    -- One row, enforced rather than assumed: a second owner is not a state this
    -- code has any sensible reading of.
    singleton      INTEGER NOT NULL DEFAULT 1 CHECK (singleton = 1),
    PRIMARY KEY (singleton)
);

CREATE INDEX IF NOT EXISTS idx_de_binomial      ON detection_events (canonical_binomial);
CREATE INDEX IF NOT EXISTS idx_de_event_time    ON detection_events (event_time_utc);
CREATE INDEX IF NOT EXISTS idx_de_binomial_time ON detection_events (canonical_binomial, event_time_utc);
CREATE INDEX IF NOT EXISTS idx_de_label         ON detection_events (upstream_label);  -- kept for provenance/audit queries
-- Windowed species queries now filter on the EFFECTIVE time
-- (COALESCE(capture_time_utc, event_time_utc)); index both legs of that choice.
CREATE INDEX IF NOT EXISTS idx_de_capture_time  ON detection_events (capture_time_utc);
CREATE INDEX IF NOT EXISTS idx_de_binomial_capture
    ON detection_events (canonical_binomial, capture_time_utc);
