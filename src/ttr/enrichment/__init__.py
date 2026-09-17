"""Enrichment layer (Stage 3).

Two INDEPENDENT models: BioCLIP as a taxonomic cross-check on the original image
(Decision 1 — a second opinion that never overwrites the authoritative upstream
label), and a local Ollama VLM as a plain-English describer on the boxed image.
Both degrade gracefully: on any failure they return ``ok=False`` with an error
and never raise into the pipeline (guardrail: degrade gracefully, never drop).
"""
