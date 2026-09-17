"""Real-BioCLIP smoke test — DESELECTED by default (plan §6 Stage 3).

Runs only under ``pytest -m models`` (requires the [enrich] extra and downloads
weights on first run). CI without weights still passes because the default
``addopts`` excludes the ``models`` marker. Proves the real classifier returns a
binomial + embedding on the fixture image, and that agreement joins on it.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.models

FIXTURE = "tests/fixtures/images/20260714_021301842_IMG_0091.jpg"


def test_real_bioclip_classifies_and_embeds(alias_map):
    from pathlib import Path

    from ttr.enrichment.agreement import compute_agreement
    from ttr.enrichment.bioclip_enricher import BioClipEnricher

    data = Path(FIXTURE).read_bytes()
    read = BioClipEnricher(model_name="hf-hub:imageomics/bioclip", topk=5).classify(data)

    assert read.ok is True, read.error
    assert read.topk and " " in read.topk[0][0]        # a scientific binomial
    assert read.embedding and len(read.embedding) > 0  # a real embedding vector

    # Agreement is computable against the real top-1 (flag depends on the image).
    flag, _ = compute_agreement("VulpesVulpes", read, alias_map)
    assert flag in {"agree", "disagree", "not_evaluable"}
