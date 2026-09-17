"""Ollama-backed LLMClient (§7).

Text generation over the local Ollama HTTP API, used by the report generator to
render prose. Images are optional (base64) so the same client could serve a
vision model. httpx is a core dependency; tests inject a mock-transport client.
"""

from __future__ import annotations

import base64
from typing import Optional

import httpx

from .base import LLMClient


class OllamaLLMClient(LLMClient):
    def __init__(
        self,
        model: str,
        endpoint: str = "http://localhost:11434",
        timeout_seconds: float = 60.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._model = model
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout_seconds
        self._client = client

    def generate(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        images: Optional[list[bytes]] = None,
    ) -> str:
        payload: dict = {"model": self._model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        if images:
            payload["images"] = [base64.b64encode(b).decode("ascii") for b in images]

        data = self._post(payload)
        return (data.get("response") or "").strip()

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
