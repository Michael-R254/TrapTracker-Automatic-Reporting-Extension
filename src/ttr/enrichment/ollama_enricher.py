"""Ollama VLM describer on the BOXED image (§5.2, §7).

Produces a plain-English description via a local Ollama vision model over HTTP.
Email-derived image content is UNTRUSTED data (CLAUDE.md constraint 6): the
system prompt instructs the model to describe only and to ignore any
instructions embedded in the image. On timeout or any error the call returns
``ok=False`` with the error and never raises (degrade gracefully).

``httpx`` is a core dependency, so this module imports without the ``[enrich]``
extra; tests inject an ``httpx.Client`` backed by a mock transport (no live
server).
"""

from __future__ import annotations

import base64
from typing import Optional

import httpx

from ..agentdefs import load_agent
from ..logging import get_logger
from .base import DescriptionEnricher, DescriptionRead

logger = get_logger(__name__)

_PROVIDER = "ollama"

# This enricher is genuinely an LLM agent: its behaviour is defined by prompt text,
# not by code. Those instructions therefore live in a version-controlled Markdown
# definition — ``src/ttr/agentdefs/definitions/vlm-image-describer.md`` — with the
# system prompt as the body and the user prompt as its ``user`` fragment. The
# prompt-injection guard (the image is data to describe, never a source of
# instructions — CLAUDE.md constraint 6) is part of that instruction text.
#
# Its sibling BioClipEnricher is NOT an agent and has no definition: it runs an
# image classifier with no prompt anywhere in its path.
_DESCRIBER_AGENT = "vlm-image-describer"


class OllamaEnricher(DescriptionEnricher):
    def __init__(
        self,
        model: str,
        endpoint: str = "http://localhost:11434",
        timeout_seconds: float = 60.0,
        max_attempts: int = 2,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._model = model
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout_seconds
        self._max_attempts = max(1, max_attempts)
        self._client = client   # injected in tests; otherwise a client per call

    def describe(self, boxed_image: bytes) -> DescriptionRead:
        if not boxed_image:
            return DescriptionRead(
                provider=_PROVIDER, model_name=self._model,
                ok=False, error="no boxed image",
            )

        definition = load_agent(_DESCRIBER_AGENT)
        payload = {
            "model": self._model,
            "system": definition.instructions,
            "prompt": definition.section("user"),
            "images": [base64.b64encode(boxed_image).decode("ascii")],
            "stream": False,
        }

        last_error: Optional[str] = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                data = self._post(payload)
                text = (data.get("response") or "").strip()
                if not text:
                    last_error = "empty response from model"
                    continue
                return DescriptionRead(
                    provider=_PROVIDER, model_name=self._model, text=text, ok=True,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("ollama_attempt_failed",
                               extra={"attempt": attempt, "error": last_error})

        return DescriptionRead(
            provider=_PROVIDER, model_name=self._model, ok=False, error=last_error,
        )

    def _post(self, payload: dict) -> dict:
        url = f"{self._endpoint}/api/generate"
        if self._client is not None:
            resp = self._client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
