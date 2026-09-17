"""The independent object detector (Phase 2c, §1).

Independent of TrapTracker by construction: it runs on the CLEAN frame, sees no
label, and its own class names are recorded but never filtered on. That
independence is the whole point — a crop sourced from TrapTracker's own
localisation would let BioCLIP inherit TrapTracker's localisation errors, and
those would then be recorded as species disagreements (Decision 1).

Default is RT-DETR (``PekingU/rtdetr_r50vd_coco_o365``, Apache-2.0) through
Hugging Face transformers (Apache-2.0). This was chosen over MegaDetector after
a licensing audit: MegaDetector's code is MIT and MIT/Apache weight variants
exist, but every shipped inference path installs an AGPL-3.0 dependency
(``megadetector`` requires ``ultralytics-yolov5``; ``PytorchWildlife`` requires
``ultralytics`` and ``yolov5``), and this project serves a web UI — the one
configuration AGPL §13 is written to reach. See ``LICENSING_REVIEW.md`` §4.

Phase 2a benchmarked it against the 835 Phase-1-verified rectangles: 97.0% recall
at IoU 0.5, median matched IoU 0.887, and no degradation on small boxes — the
smallest decile (21-41px shorter side) was found at 93.5%.

Heavy imports are deferred to call time; a failure returns ``ok=False`` and never
raises into the pipeline (Constraint 4).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..logging import get_logger
from .base import DetectorBox, DetectorRead

logger = get_logger(__name__)

_PROVIDER = "rtdetr"

#: Phase 2a operating point. Holds 97.0% recall while cutting candidates per
#: frame from 26.5 to 4.9; at this setting every event yielded at least one
#: candidate. Raising it to 0.50 halves candidates again but costs 7.5 points of
#: recall and leaves events with nothing at all.
DEFAULT_SCORE_THRESHOLD = 0.30


class ObjectDetector(ABC):
    """Proposes candidate boxes on a clean frame. Never sees a label."""

    @abstractmethod
    def detect(self, clean_image: bytes) -> DetectorRead:
        ...


class RtDetrDetector(ObjectDetector):
    def __init__(self, model_name: str, device: str = "cpu",
                 score_threshold: float = DEFAULT_SCORE_THRESHOLD,
                 max_candidates: int = 100) -> None:
        self._model_name = model_name
        self._device = device
        self._threshold = score_threshold
        self._max = max_candidates
        self._proc = None
        self._model = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        from transformers import AutoImageProcessor, RTDetrForObjectDetection

        self._proc = AutoImageProcessor.from_pretrained(self._model_name)
        self._model = RTDetrForObjectDetection.from_pretrained(self._model_name).eval()
        logger.info("detector_model_loaded",
                    extra={"model": self._model_name, "device": self._device})

    def detect(self, clean_image: bytes) -> DetectorRead:
        if not clean_image:
            return DetectorRead(provider=_PROVIDER, model_name=self._model_name,
                                ok=False, error="no clean image")
        try:
            import io

            import torch
            from PIL import Image

            image = Image.open(io.BytesIO(clean_image)).convert("RGB")
            self._ensure_model()
            inputs = self._proc(images=image, return_tensors="pt")
            with torch.no_grad():
                outputs = self._model(**inputs)
            sizes = torch.tensor([[image.height, image.width]])
            result = self._proc.post_process_object_detection(
                outputs, threshold=self._threshold, target_sizes=sizes)[0]

            id2label = self._model.config.id2label
            cands = [
                DetectorBox(box=(int(b[0]), int(b[1]), int(b[2]), int(b[3])),
                            score=float(s),
                            label=str(id2label.get(int(l), l)))
                for s, l, b in zip(result["scores"].tolist(),
                                   result["labels"].tolist(),
                                   result["boxes"].tolist())
            ]
            cands.sort(key=lambda c: -c.score)
            return DetectorRead(provider=_PROVIDER, model_name=self._model_name,
                                candidates=cands[:self._max], ok=True)
        except Exception as exc:                      # never raise into the pipeline
            logger.warning("detector_failed", extra={"error": repr(exc)})
            return DetectorRead(provider=_PROVIDER, model_name=self._model_name,
                                ok=False, error=f"{type(exc).__name__}: {exc}")
