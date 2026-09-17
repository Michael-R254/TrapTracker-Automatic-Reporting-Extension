"""Build a project from a published evaluation extract.

An extract is the image-free, identifier-free CSV this project publishes so its
claims can be reproduced without redistributing camera imagery or transport
metadata (`docs/evaluation/data/README.md`). It is the artefact a reviewer cites,
and until now there was no way to open one in the application that produced it --
the extract was a file you read, not a project you could explore.

**This is a loader, not a second ingest path.** It writes rows that were already
enriched, exactly as the CSV records them. Nothing here calls BioCLIP, the VLM or
a mailbox, and nothing re-decides a verdict: `agreement_flag`, `taxonomic_distance`
and the rest are copied across as stored values. A loader that recomputed them
would produce a project that disagreed with the evidence file it was built from.

**What the rows deliberately lack.** The extract carries no Message-IDs, no image
paths and no addresses, so:

* `source_message_id` is synthesised as ``<extract-N@invalid>``. `.invalid` is
  reserved by RFC 2606 and can never resolve, which is the point: the id has to
  be unique for the upsert and must never look like a real one.
* `images_present` is ``"none"`` and both image paths stay NULL. The project's
  image store is created and stays empty.
* `event_time_utc` is alert-**send** time (Decision 3). The extract has no capture
  time, so `capture_time_utc` stays NULL and the row is flagged accordingly --
  the same shape a real row takes when the filename could not be decoded.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Optional

from ..logging import get_logger
from .errors import ProjectError

logger = get_logger(__name__)

#: Columns the loader requires. A CSV missing any of them is refused by name
#: rather than half-loaded and left to fail later on a NULL.
REQUIRED_COLUMNS = (
    "idx", "upstream_label", "canonical_binomial", "event_time_utc",
)

#: Columns copied straight through when present. Absent ones stay NULL rather
#: than being defaulted to something plausible.
_OPTIONAL_TEXT = (
    "agreement_flag", "agreement_rationale", "cross_check_status",
    "resolution_basis", "matched_rank", "taxonomic_distance",
    "bioclip_top1_kingdom", "bioclip_top1_class", "bioclip_top1_order",
    "bioclip_top1_family", "bioclip_top1_genus", "vlm_description",
)


def _parse_time(value: str, row_no: int) -> dt.datetime:
    raw = (value or "").strip()
    if not raw:
        raise ProjectError(f"row {row_no}: event_time_utc is empty")
    try:
        stamp = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectError(f"row {row_no}: event_time_utc {raw!r} is not ISO-8601") from exc
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def read_extract(csv_path: Path) -> list[dict]:
    """Parse and validate an extract, or raise with the reason."""
    if not csv_path.is_file():
        raise ProjectError(f"no such extract: {csv_path}")
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ProjectError(f"{csv_path}: no rows")
    missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing:
        raise ProjectError(
            f"{csv_path}: missing required column(s): {', '.join(missing)}")
    return rows


def load_into(ctx, rows: list[dict], *, ingested_at: Optional[dt.datetime] = None) -> int:
    """Write `rows` into the project's database. Returns the number stored."""
    from ..storage.repository import PersistedEvent

    stamp = ingested_at or dt.datetime.now(dt.timezone.utc)
    repo = ctx.open_repo()
    stored = 0
    try:
        for n, row in enumerate(rows, 1):
            when = _parse_time(row.get("event_time_utc", ""), n)
            try:
                topk = json.loads(row.get("bioclip_topk_json") or "[]")
            except (TypeError, ValueError):
                topk = []

            def text(col: str) -> Optional[str]:
                v = (row.get(col) or "").strip()
                return v or None

            confidence = None
            raw_conf = (row.get("upstream_confidence") or "").strip()
            if raw_conf:
                try:
                    confidence = float(raw_conf)
                except ValueError:
                    confidence = None

            binomial = text("canonical_binomial")
            repo.upsert_event(PersistedEvent(
                source_type="extract",
                # RFC 2606 reserved: unique for the upsert, never resolvable, and
                # visibly not a real Message-ID to anyone who reads one.
                source_message_id=f"<extract-{row.get('idx') or n}@invalid>",
                ingested_at_utc=stamp,
                upstream_label=text("upstream_label"),
                upstream_confidence=confidence,
                canonical_binomial=binomial,
                canonical_is_binomial=bool(binomial)
                                      and not binomial.startswith("nonbio:"),
                event_time_utc=when,
                # The extract has no capture time, so the effective key is send
                # time and the row says so rather than implying otherwise.
                event_time_is_capture_time=False,
                bioclip_ok=bool(topk),
                bioclip_topk=topk,
                images_present="none",
                field_provenance={"loaded_from_extract": {
                    "present": True, "source": "csv"}},
                **{c: text(c) for c in _OPTIONAL_TEXT},
            ))
            stored += 1
    finally:
        repo.close()
    logger.info("extract_loaded", extra={"rows": stored})
    return stored


def create_from_extract(csv_path: Path, name: str, *,
                        alias_table: Optional[Path] = None,
                        site_name: Optional[str] = None,
                        latitude: Optional[float] = None,
                        longitude: Optional[float] = None,
                        root: Optional[Path] = None):
    """Create a mailbox-less project and load an extract into it.

    Returns ``(manifest, project_dir, ctx, stored)``. Everything that can be
    checked is checked before the project exists, and a failure after that
    removes the half-built project, so a refusal leaves nothing to delete by hand.
    """
    from .paths import projects_root
    from .registry import Registry
    from .service import context_for, create_project, delete_project, set_site

    rows = read_extract(Path(csv_path))
    if (latitude is None) != (longitude is None):
        raise ProjectError("give both --latitude and --longitude, or neither -- a "
                           "half-set coordinate pair is not a location")
    if alias_table is not None and not Path(alias_table).is_file():
        raise ProjectError(f"no such alias table: {alias_table}")

    root = root or projects_root()
    manifest, project_dir = create_project(name, root=root)
    try:
        # The alias table belongs in the project BEFORE the rows are resolved
        # against it, so the stored `alias_table_sha256` names the table actually
        # in force rather than the bundled fallback.
        if alias_table is not None:
            import shutil

            shutil.copy2(alias_table, project_dir / "species_aliases.yaml")
        if site_name or latitude is not None:
            set_site(manifest.id, name=site_name or None,
                     latitude=latitude, longitude=longitude, root=root)
        ctx = context_for(Registry.load(root).get(manifest.id), root)
        stored = load_into(ctx, rows)
    except BaseException:
        delete_project(manifest.id, root=root, force=True)
        raise
    return manifest, project_dir, ctx, stored
