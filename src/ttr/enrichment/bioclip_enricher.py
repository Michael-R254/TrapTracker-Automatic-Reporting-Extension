"""BioCLIP taxonomic cross-check on the ORIGINAL image (Decision 1, §5.2).

Verified against the installed pybioclip 2.1.5 API:
  - ``from bioclip import Rank`` / ``from bioclip.predict import TreeOfLifeClassifier``
  - ``TreeOfLifeClassifier(model_str=..., device=...)``
  - ``predict(image, Rank.SPECIES, k=...) -> list[dict]`` with keys
    ``species`` (scientific binomial, e.g. "Vulpes vulpes"), ``common_name``, ``score``
  - ``create_image_features_for_image(image, normalize=True) -> torch.Tensor`` (embedding)

Heavy imports (torch, PIL, bioclip) are deferred to call time so this module
imports without the ``[enrich]`` extra, and any failure — missing image, missing
weights, inference error — returns ``ok=False`` and never raises (degrade
gracefully). The authoritative upstream label is never seen or touched here.
"""

from __future__ import annotations

import io
from typing import Optional

from ..logging import get_logger
from .base import TaxonomicEnricher, TaxonomicRead

logger = get_logger(__name__)

_PROVIDER = "bioclip"


class BioClipEnricher(TaxonomicEnricher):
    def __init__(self, model_name: str, device: str = "cpu", topk: int = 5) -> None:
        self._model_name = model_name
        self._device = device
        self._topk = topk
        self._classifier = None            # lazily constructed on first use
        self._rank = None

    def _ensure_model(self) -> None:
        if self._classifier is not None:
            return
        # NOTE: if the configured checkpoint (default 'hf-hub:imageomics/bioclip',
        # §7) fails to load under pybioclip 2.x — which ships 'bioclip-2' — that is
        # a config change (BIOCLIP_MODEL), not a code change. Surfaced at smoke time.
        from bioclip import Rank
        from bioclip.predict import TreeOfLifeClassifier

        self._rank = Rank.SPECIES
        self._classifier = TreeOfLifeClassifier(
            model_str=self._model_name, device=self._device
        )
        logger.info("bioclip_model_loaded", extra={"model": self._model_name, "device": self._device})

    def classify(self, original_image: bytes) -> TaxonomicRead:
        if not original_image:
            return TaxonomicRead(
                provider=_PROVIDER, model_name=self._model_name,
                ok=False, error="no original image",
            )
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(original_image)).convert("RGB")
            self._ensure_model()

            # pybioclip's predict() accepts str | List[str] | List[Image] — a
            # bare PIL Image is NOT accepted (it gets iterated), so pass a list.
            preds = self._classifier.predict([image], self._rank, k=self._topk)
            topk, lineages = self._to_topk(preds)
            embedding = self._embedding(image)
            return TaxonomicRead(
                provider=_PROVIDER, model_name=self._model_name,
                topk=topk, topk_lineages=lineages, embedding=embedding, ok=True,
            )
        except Exception as exc:  # never raise into the pipeline
            logger.warning("bioclip_failed", extra={"error": repr(exc)})
            return TaxonomicRead(
                provider=_PROVIDER, model_name=self._model_name,
                ok=False, error=f"{type(exc).__name__}: {exc}",
            )

    #: Ranks pybioclip returns alongside the binomial. `predict(..., Rank.SPECIES)`
    #: builds each row from `create_classification_dict`, which populates every rank
    #: down to the requested one — the binomial alone was only ever a projection of
    #: a full lineage the model already had.
    _RANK_KEYS = ("kingdom", "phylum", "class", "order", "family",
                  "genus", "species_epithet")

    @classmethod
    def _to_topk(cls, preds) -> tuple[list[tuple[str, float]], list[dict]]:
        """``(topk, lineages)`` — positionally aligned, one lineage per candidate.

        The lineage is taken verbatim from the model's own output rather than looked
        up: BioCLIP's placement of a taxon is the placement the comparison must use,
        and an inferred rank would be indistinguishable in the audit from a reported
        one. Ranks the model omits are dropped rather than filled with a blank, so
        an absent rank stays visibly absent.
        """
        # predict() returns a list of dicts for a single image; be tolerant of a
        # dict-keyed-by-image shape too.
        rows = preds if isinstance(preds, list) else next(iter(preds.values()))
        out: list[tuple[str, float]] = []
        lineages: list[dict] = []
        for row in rows:
            species = row.get("species") or row.get("scientific_name")
            score = row.get("score")
            if species is None or score is None:
                continue
            out.append((str(species), float(score)))
            lineages.append({k: str(row[k]) for k in cls._RANK_KEYS
                             if row.get(k) not in (None, "")})
        return out, lineages

    def _embedding(self, image) -> Optional[list[float]]:
        try:
            tensor = self._classifier.create_image_features_for_image(image, normalize=True)
            flat = tensor.detach().cpu().reshape(-1).tolist()
            return [float(x) for x in flat]
        except Exception as exc:
            # Embedding is best-effort; top-k can still be authoritative.
            logger.warning("bioclip_embedding_failed", extra={"error": repr(exc)})
            return None
