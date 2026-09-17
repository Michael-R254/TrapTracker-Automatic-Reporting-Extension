"""LLMClient ABC — text generation with optional images (§4)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class LLMClient(ABC):
    @abstractmethod
    def generate(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        images: Optional[list[bytes]] = None,
    ) -> str:
        """Return the model's text response. Raises on transport/model failure;
        callers that must not fail (the report) catch and degrade."""
        ...
