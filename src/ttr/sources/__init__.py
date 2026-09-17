"""Ingestion sources (Stage 1).

`DetectionSource` is the swappable ingestion seam; `EmailSource` is the first
concrete implementation. A future `DatabaseSource`/`WebhookSource` can slot in
with no downstream changes (plan §3, §5.1).
"""
