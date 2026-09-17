"""BioCLIP's own label table, read locally — for BACKFILL only.

Rows enriched before the pipeline started capturing BioCLIP's full hierarchy stored
only the binomial, so their `taxonomic_distance` cannot be measured from what is in
the database. This module recovers the missing ranks from the exact table BioCLIP
classifies over, so a backfilled lineage is identical to what a re-run would have
produced — not an inference from the binomial.

**Nothing here is vendored.** The table is fetched through the same
``hf_hub_download`` call ``pybioclip`` itself makes, into the user's own Hugging Face
cache, and this repository redistributes no copy of it. It
is therefore an OPTIONAL import: only ``recompute-crosscheck --backfill-lineage``
needs it, the live pipeline never does (its lineages come straight off each
prediction), and the test suite never does. Heavy imports are deferred to call time
so importing this module costs nothing.
"""

from __future__ import annotations

from typing import Optional

from ..logging import get_logger
from ..species.taxonomy import Lineage

logger = get_logger(__name__)

_RANKS = ("kingdom", "phylum", "class", "order", "family", "genus", "species_epithet")
_DATAFILE = "embeddings/txt_emb_species.json"


def _agreed_ranks(a: Lineage, b: Lineage) -> Lineage:
    """The ranks two lineages for the same binomial agree on; the rest blanked.

    Keeps what the source table is unanimous about and drops what it is not, so a
    merged entry never asserts a placement the table contradicts elsewhere.
    """
    left, right = a.as_mapping(), b.as_mapping()
    return Lineage.from_mapping(
        {rank: value for rank, value in left.items() if value and right.get(rank) == value}
    )


class TolLineageIndex:
    """Binomial -> :class:`Lineage`, from BioCLIP's Tree-of-Life label table.

    Keyed on the binomial rather than the genus on purpose: 1,849 genus names in
    that table are homonyms across kingdoms (``Ficus`` is both a fig and a sea
    snail), so a genus-keyed lookup would place a fifth of the observed predictions
    in the wrong kingdom.

    A binomial can still carry more than one lineage, almost always because the
    table mixes competing higher-rank schemes for the same organism (``Apus apus``
    appears under both Apodiformes and Caprimulgiformes; ``Poa pratensis`` under
    both Liliopsida and Magnoliopsida). Those are merged rank by rank: a rank every
    candidate agrees on is kept, a rank they disagree on is dropped. The result
    asserts only what the table is unanimous about, and since an absent rank never
    counts as a match, the distance is computed from the agreed ranks instead of
    collapsing to ``undetermined``. A genuine cross-kingdom homonym disagrees at
    every rank, so it empties out and correctly reports ``undetermined``.
    """

    def __init__(self, by_binomial: dict[str, Lineage], merged: set[str]) -> None:
        self._by_binomial = by_binomial
        self._merged = merged

    def __len__(self) -> int:
        return len(self._by_binomial)

    @property
    def merged_count(self) -> int:
        """Binomials whose conflicting lineages were reduced to their agreed ranks."""
        return len(self._merged)

    def lineage_for(self, taxon: Optional[str]) -> Optional[Lineage]:
        """Look up a BioCLIP label.

        Tries the label verbatim first — some table entries carry an author string
        inside the epithet (``Cortinarius firmus Fr.``), and truncating before
        trying the whole thing would miss them. Only then falls back to the first
        two words for a real trinomial (``Columba livia domestica`` ->
        ``Columba livia``), matching the alias table's own subspecies handling so
        both sides of a comparison collapse alike.
        """
        if not taxon:
            return None
        hit = self._by_binomial.get(" ".join(taxon.split()).casefold())
        if hit is not None:
            return hit
        parts = taxon.split()
        if len(parts) > 2:
            return self._by_binomial.get(" ".join(parts[:2]).casefold())
        return None

    @classmethod
    def load(cls, model_str: str) -> "TolLineageIndex":
        """Download-or-reuse the cached label table for ``model_str`` and index it.

        Raises ``RuntimeError`` with an actionable message when pybioclip is absent
        or the model has no Tree-of-Life table, rather than surfacing an import
        error from three layers down.
        """
        import json

        try:
            from bioclip._constants import HF_DATAFILE_REPO_TYPE, TOL_MODELS
            from huggingface_hub import hf_hub_download
        except ImportError as exc:                      # pragma: no cover - env-dependent
            raise RuntimeError(
                "lineage backfill needs the optional enrichment extra: "
                'pip install -e ".[enrich]"'
            ) from exc

        repo_id = TOL_MODELS.get(model_str)
        if repo_id is None:
            raise RuntimeError(
                f"no Tree-of-Life label table for BioCLIP model '{model_str}'; "
                f"known models: {', '.join(sorted(TOL_MODELS))}"
            )

        path = hf_hub_download(repo_id=repo_id, filename=_DATAFILE,
                               repo_type=HF_DATAFILE_REPO_TYPE)
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)

        by_binomial: dict[str, Lineage] = {}
        merged: set[str] = set()
        for names, _common in rows:
            body = dict(zip(_RANKS, names))
            genus, epithet = body.get("genus"), body.get("species_epithet")
            if not (genus and epithet):
                continue
            key = f"{genus} {epithet}".casefold()
            lineage = Lineage.from_mapping(body)
            existing = by_binomial.get(key)
            if existing is None:
                by_binomial[key] = lineage
            elif existing != lineage:
                by_binomial[key] = _agreed_ranks(existing, lineage)
                merged.add(key)

        logger.info("tol_lineage_index_loaded",
                    extra={"repo": repo_id, "binomials": len(by_binomial),
                           "merged_conflicts": len(merged)})
        return cls(by_binomial, merged)
