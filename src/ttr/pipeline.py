"""Pipeline wiring: source -> enrich -> store (the ``run`` loop, §4/§6 Stage 5).

This is the wiring layer (not an agent), so it is the one place allowed to touch
sources, enrichment, and storage together. It is where species resolution is
INJECTED into persistence: every ingested event's ``upstream_label`` is resolved
to a canonical key via the alias map (or a reserved ``unmapped:`` key), so no row
is ever stored with a NULL binomial and then invisible to every query.

Degrade gracefully, never drop: the enrichers already return ``ok=False`` instead
of raising, and one event's unexpected failure is logged and skipped without
halting the loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .enrichment.agreement import compute_crosscheck
from .enrichment.base import DescriptionEnricher, TaxonomicEnricher
from .logging import get_logger
from .sources.base import DetectionEvent, DetectionSource
from .species.aliases import SpeciesAliasMap
from .species.taxonomy import load_target_taxonomy
from .storage.mapping import persisted_from_detection_event
from .storage.repository import DetectionRepository

logger = get_logger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class Pipeline:
    def __init__(
        self,
        *,
        source: DetectionSource,
        taxonomic: TaxonomicEnricher,
        description: DescriptionEnricher,
        repo: DetectionRepository,
        alias_map: SpeciesAliasMap,
        target_taxonomy=None,
        image_store_dir: str | Path,
        vlm_fallback_to_original: bool = False,
        agreement_use_topk: bool = False,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self._source = source
        self._taxonomic = taxonomic
        self._description = description
        self._repo = repo
        self._alias_map = alias_map
        # Higher ranks for the upstream targets, used ONLY to measure how far apart
        # a disagreement is. It can never change the verdict, which is species-level
        # (see enrichment.agreement); a None here costs the distance, not the flag.
        self._target_taxonomy = target_taxonomy if target_taxonomy is not None else load_target_taxonomy()
        self._image_store = Path(image_store_dir)
        self._vlm_fallback = vlm_fallback_to_original
        self._agreement_use_topk = agreement_use_topk
        # Optional progress observer (the ingest page's live view). A DISPLAY
        # concern only: it is a bare callable handed plain dicts, so no import
        # edge crosses a stage boundary, and it can never affect what is stored.
        self._on_event = on_event

    def run_once(self) -> int:
        """Poll the source once; enrich and store each new event. Returns the
        count stored. One event's failure is logged and skipped, never fatal."""
        stored = 0
        for ev in self._source.poll():
            try:
                self.process_event(ev)
                stored += 1
            except Exception as exc:   # belt-and-suspenders; enrichers don't raise
                logger.error("pipeline_event_failed",
                             extra={"message_id": ev.source_message_id, "error": repr(exc)})
                # A skipped event must be VISIBLE to an observer, not a silent gap.
                self._notify({"ok": False, "message_id": ev.source_message_id,
                              "upstream_label": ev.upstream_label, "error": repr(exc)})
        logger.info("pipeline_run_once_done", extra={"stored": stored})
        return stored

    def process_event(self, ev: DetectionEvent) -> int:
        original_path, boxed_path = self._persist_images(ev)
        original_bytes = self._role_bytes(ev, "original")
        boxed_bytes = self._role_bytes(ev, "boxed")

        boxed_fallback = False
        if not boxed_bytes and self._vlm_fallback and original_bytes:
            boxed_bytes, boxed_fallback = original_bytes, True

        taxo = self._taxonomic.classify(original_bytes)
        desc = self._description.describe(boxed_bytes)

        # Watch-item §8: log an unmapped BioCLIP label with the event id.
        if taxo.ok and taxo.topk and self._alias_map.binomial_for_bioclip(taxo.topk[0][0]) is None:
            logger.warning("unmapped_bioclip_label",
                           extra={"label": taxo.topk[0][0], "message_id": ev.source_message_id})

        check = compute_crosscheck(
            ev.upstream_label, taxo, self._alias_map, self._target_taxonomy,
            use_topk=self._agreement_use_topk,
        )
        flag, rationale = check.flag, check.rationale
        lineage = check.bioclip_lineage

        pe = persisted_from_detection_event(
            ev, alias_map=self._alias_map,
            original_image_path=original_path, boxed_image_path=boxed_path,
        )
        # Provenance for the species resolution just performed. Read off the map
        # that did the resolving, so the two cannot disagree.
        pe.alias_table_sha256 = getattr(self._alias_map, "content_sha256", None)
        pe.bioclip_ok = taxo.ok
        pe.bioclip_model = taxo.model_name
        pe.bioclip_topk = taxo.topk or None
        pe.bioclip_error = taxo.error
        pe.agreement_flag = flag
        pe.agreement_rationale = rationale
        pe.cross_check_status = check.status
        pe.resolution_basis = check.resolution_basis
        pe.matched_rank = check.matched_rank
        pe.taxonomic_distance = check.taxonomic_distance
        if lineage is not None:
            pe.bioclip_top1_kingdom = lineage.kingdom
            pe.bioclip_top1_class = lineage.class_name
            pe.bioclip_top1_order = lineage.order
            pe.bioclip_top1_family = lineage.family
            pe.bioclip_top1_genus = lineage.genus
        pe.vlm_ok = desc.ok
        pe.vlm_model = desc.model_name
        pe.vlm_description = desc.text
        pe.vlm_error = desc.error
        if boxed_fallback:
            pe.parse_warnings = list(pe.parse_warnings) + [
                "VLM described the ORIGINAL image (boxed absent); config fallback"
            ]

        event_id = self._repo.upsert_event(pe)
        if taxo.embedding:
            self._repo.store_embedding(event_id, taxo.embedding)
        # Every durable write is done, so — and only now — the source may retire
        # the message. Acknowledging at yield time meant an event that failed
        # anywhere above (a full image store, a locked database) was already
        # marked seen and never came round again: a silent drop, which §8
        # "degrade gracefully, never drop" forbids. Re-delivery is harmless
        # (UNIQUE source_message_id + ON CONFLICT DO NOTHING); a drop is not.
        self._source.mark_processed(ev.source_message_id)
        logger.info("pipeline_event_stored",
                    extra={"id": event_id, "key": pe.canonical_binomial,
                           "agreement": flag, "bioclip_ok": taxo.ok, "vlm_ok": desc.ok})
        self._notify({
            "ok": True,
            "event_id": event_id,
            "message_id": ev.source_message_id,
            "upstream_label": ev.upstream_label,
            "upstream_confidence": ev.upstream_best_confidence,
            "canonical_key": pe.canonical_binomial,
            "canonical_is_binomial": pe.canonical_is_binomial,
            "bioclip_ok": taxo.ok,
            "bioclip_top1": taxo.topk[0][0] if taxo.topk else None,
            "bioclip_score": taxo.topk[0][1] if taxo.topk else None,
            "bioclip_error": taxo.error,
            "agreement": flag,
            "agreement_rationale": rationale,
            "cross_check_status": check.status,
            "resolution_basis": check.resolution_basis,
            "taxonomic_distance": check.taxonomic_distance,
            "vlm_ok": desc.ok,
            "vlm_error": desc.error,
            # Paths are for the OBSERVER to turn into a thumbnail server-side; they
            # are stripped before anything reaches a browser.
            "original_image_path": original_path,
            "boxed_image_path": boxed_path,
            "parse_warnings": list(pe.parse_warnings or []),
        })
        return event_id

    def _notify(self, payload: dict) -> None:
        """Hand a small, JSON-shaped progress record to the optional observer.

        Degrade gracefully, never drop (§8): a progress sink is a display concern,
        so a failure in it must never affect ingestion. Every exception is
        swallowed and logged. Only plain scalars and lists cross this boundary —
        no domain object escapes, so the observer gains no coupling to
        sources/enrichment/storage.
        """
        if self._on_event is None:
            return
        try:
            self._on_event(payload)
        except Exception as exc:
            logger.warning("pipeline_progress_callback_failed", extra={"error": repr(exc)})

    # ------------------------------------------------------------------ images
    def _persist_images(self, ev: DetectionEvent) -> tuple[Optional[str], Optional[str]]:
        """Write attachment blobs into OUR image store; return STORE-RELATIVE paths.

        Relative to the project's image store, forward slashes — the same shape
        `ttr project import` rewrites an adopted corpus to, produced here so there
        is ONE shape and one place that decides it.

        Absolute paths resolve too, which is exactly why this went unnoticed: the
        eleven events of the first live multi-project ingest resolved perfectly
        while quietly costing the property the migration existed to establish.
        A project directory is meant to be movable — copied to another machine,
        or relocated by `--mode move` — and a row holding an absolute path breaks
        the moment it is. Relative is not a formatting preference; it is what
        makes the project self-contained.
        """
        original_path = boxed_path = None
        if not ev.images:
            return None, None
        dest_dir = self._image_store / _safe(ev.source_message_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for img in ev.images:
            dest = dest_dir / _safe(img.filename)
            dest.write_bytes(img.data)
            stored = dest.relative_to(self._image_store).as_posix()
            if img.role == "original":
                original_path = stored
            elif img.role == "boxed":
                boxed_path = stored
        return original_path, boxed_path

    @staticmethod
    def _role_bytes(ev: DetectionEvent, role: str) -> bytes:
        for img in ev.images:
            if img.role == role:
                return img.data
        return b""


def _safe(name: str) -> str:
    return _UNSAFE.sub("_", name).strip("_") or "unnamed"


# --------------------------------------------------------------- shared wiring
@dataclass
class PipelineBundle:
    """A built pipeline plus the resources it owns, so the caller can close them.

    The repository is created INSIDE whichever thread calls :func:`build_pipeline`,
    because sqlite connections are thread-bound (``storage.migrations.connect``
    uses ``sqlite3.connect``'s default ``check_same_thread=True``). A web request
    thread therefore cannot build a bundle for a background job to use — the job
    thread must build its own.
    """

    pipeline: "Pipeline"
    repo: "DetectionRepository"
    poll_seconds: int

    def close(self) -> None:
        self.repo.close()


def build_pipeline_for(ctx, *, alias_map=None, app_settings=None,
                       on_event: Callable[[dict], None] | None = None,
                       projects_in_scope: int | None = None,
                       ) -> PipelineBundle:
    """The production wiring, for ONE project.

    Everything per-deployment comes from ``ctx``: the database, the image store,
    the seen-store, the alias and taxonomy tables, the mailbox, and the poll
    interval. Everything machine-level keeps coming from ``Settings`` —
    BioCLIP's checkpoint and device, the detector's model and thresholds, the
    Ollama endpoint, the browser used for PDFs. That split is permanent: those
    describe the host, not the deployment, and duplicating them per project would
    invite two projects to disagree about `crop_iou_tau`, which is an analysis
    constant derived once in Phase 2a.

    The repository and the seen-store are built HERE, in the calling thread —
    see :class:`PipelineBundle`.

    ``projects_in_scope`` is how many projects an unscoped ``TTR_IMAP_PASSWORD``
    could be meant for (``credentials.resolve``). It has no usable default, on
    purpose. Every caller took a default of 1 until 2026-09-15, and that switched
    off the refusal the count exists for. Left unstated, the pipeline still
    builds, but refuses at login.
    """
    from .enrichment.factory import build_description_enricher, build_taxonomic_enricher
    from .sources.email_fetcher import EmailFetcher
    from .sources.email_source import EmailSource
    from .sources.seen_store import SeenStore
    from .storage.repository import DetectionRepository

    # FIRST, before a repository, a seen-store or a model is built: a project
    # with no inbox cannot be polled, and saying so is better than opening
    # everything and then failing inside the fetcher.
    ctx.require_mailbox()

    if app_settings is None:
        # `ctx.app` when the caller supplied one, else the process-level
        # settings. NOTE (transitional): `Settings` still declares the IMAP
        # fields as required, so constructing it needs *some* value for them
        # even though a project run no longer reads them. Those fields are
        # removed in a later stage, once a test proves nothing reads them.
        app_settings = ctx.app if ctx.app is not None else _process_settings()

    if alias_map is None:
        alias_map = ctx.load_alias_map()

    repo = DetectionRepository(ctx.db_path)
    # Built before the fetcher so the fetcher can consult it and skip DOWNLOADING
    # mail already handled — see EmailFetcher.fetch_unseen.
    seen = SeenStore(ctx.seen_store_path)
    mailbox = ctx.mailbox

    # A callable, not a resolved value: the credential is looked up at login
    # time and never held by the fetcher.
    def password():
        if projects_in_scope is None:
            from .projects.credentials import CredentialScopeNotStated

            raise CredentialScopeNotStated(
                f"the pipeline for project {ctx.id} was built without stating how "
                f"many projects are in scope for its credential, so no mailbox "
                f"password was resolved. This is a bug in the caller of "
                f"build_pipeline_for, not a configuration problem.")
        return ctx.imap_password(projects_in_scope=projects_in_scope)

    fetcher = EmailFetcher(
        host=mailbox.imap_host,
        user=mailbox.imap_user,
        password=password,
        folder=mailbox.imap_folder,
        use_ssl=mailbox.imap_use_ssl,
        lookback_days=mailbox.imap_lookback_days,
        is_known=seen.__contains__,
    )
    pipeline = Pipeline(
        source=EmailSource(fetcher, seen),
        taxonomic=build_taxonomic_enricher(app_settings),
        description=build_description_enricher(app_settings),
        repo=repo,
        alias_map=alias_map,
        target_taxonomy=ctx.load_target_taxonomy(),
        image_store_dir=ctx.image_store_dir,
        vlm_fallback_to_original=app_settings.vlm_fallback_to_original_on_missing_boxed,
        agreement_use_topk=app_settings.agreement_topk_join,
        on_event=on_event,
    )
    logger.info("pipeline_built",
                extra={"project": ctx.id, "db": str(ctx.db_path),
                       "image_store": str(ctx.image_store_dir)})
    return PipelineBundle(pipeline=pipeline, repo=repo,
                          poll_seconds=mailbox.poll_seconds)


def _process_settings():
    from .config import get_settings

    return get_settings()


def seen_store_path(storage) -> Path:
    """Where the idempotency key file lives, derived from the database's location.

    Defined ONCE so the CLI's `ingest`, the CLI's `run` and the web trigger cannot
    end up pointing at different files and re-ingesting each other's messages.
    Pointing a project at its own database therefore moves its seen-store with it,
    for free.

    The parameter is anything carrying a ``db_path`` — in practice a
    ``ProjectContext``'s narrow settings view. It was named ``settings`` while
    that was a ``Settings`` object; it is not one any more, and the old name made
    this read like the last place the application reached into global
    configuration for a per-deployment value.
    """
    return storage.db_path.parent / "seen_message_ids.txt"
