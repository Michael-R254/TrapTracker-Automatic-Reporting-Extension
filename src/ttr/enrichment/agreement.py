"""`compute_crosscheck` — the BioCLIP cross-check verdict (§5.2, Decision 2).

**Two-state, species-level, deterministic.** The upstream raw class token is
authoritative and is read, never mutated (Decision 1). BioCLIP's top-1 either
resolves to the same scientific binomial or it does not:

    agree     — BioCLIP's top-1 IS the upstream species (or a subspecies of it)
    disagree  — it is not, for ANY reason, including a prediction outside the
                alias table

There is no third verdict and no looser rank. A genus- or family-level near miss is
still a disagreement: "corroborated" has to keep meaning "same species" or it cannot
be defended. The old ``indeterminate`` state is gone — it dominated the corpus (617
of 787) and read as a mystery bucket rather than as a finding.

**What replaced it.** Every disagreement now carries a measured
``taxonomic_distance`` (same genus / same family / ... / not an animal) and a
``resolution_basis`` naming how the verdict was reached. That turns the former
mystery bucket into an evidenced sub-classification of *why* each disagreement
occurred, without weakening what ``agree`` means.

**Not-evaluable is a denominator, not a verdict.** Three situations make a
comparison physically impossible rather than negative: a class outside BioCLIP's
Tree of Life (Person/Car/CalibrationPole — Decision 2), a BioCLIP run that failed or
returned nothing, and a missing or unmappable upstream label. Scoring those as
``disagree`` would assert a taxonomic conflict that was never tested. They get
``status='not_evaluable'`` and are reported as an explicitly excluded denominator,
so agree + disagree + not_evaluable always reconstructs the full corpus.

This module is pure — no I/O, no logging, no LLM. The Watch-item requirement to log
unmappable BioCLIP labels with the event id (§8) is satisfied by the caller
(pipeline), which has the event id; the rationale returned here names the label so
that log stays precise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from ..species.aliases import SpeciesAliasMap
from ..species.taxonomy import Lineage, TargetTaxonomy, taxonomic_distance
from .base import TaxonomicRead

#: The headline verdict. ``not_evaluable`` is a STATUS carried in the same column so
#: a row can never be silently dropped from a count; it is not a third opinion about
#: the species, and the binary is reported over evaluable rows only.
Flag = Literal["agree", "disagree", "not_evaluable"]

Status = Literal["evaluable", "not_evaluable"]

#: How the verdict was reached. The first two are the only bases that yield
#: ``agree``; the rest all yield ``disagree`` or ``not_evaluable``.
RESOLUTION_BASES = (
    "exact_species_match",          # BioCLIP top-1 binomial == upstream binomial
    "subspecies_collapsed_match",   # top-1 was a trinomial that collapses to it
    "topk_species_match",           # OPT-IN top-k join only: matched below rank 1
    "no_taxonomic_match",           # resolved on both sides; different species
    "unresolved_prediction",        # top-1 carried no usable binomial at all
    "outside_bioclip_taxonomy",     # Person/Car/CalibrationPole (not_evaluable)
    "bioclip_unavailable",          # failed / returned nothing (not_evaluable)
    "upstream_label_unusable",      # missing or unmappable upstream token (not_evaluable)
)


@dataclass(frozen=True)
class CrossCheck:
    """One event's cross-check outcome: headline verdict plus its audit trail."""

    flag: Flag
    status: Status
    resolution_basis: str
    matched_rank: str                       # "species" | "none"
    taxonomic_distance: str                 # see species.taxonomy.DISTANCES
    rationale: str
    bioclip_top1: Optional[str] = None
    bioclip_lineage: Optional[Lineage] = None

    @property
    def is_agree(self) -> bool:
        return self.flag == "agree"

    @property
    def is_evaluable(self) -> bool:
        return self.status == "evaluable"


def _not_evaluable(basis: str, rationale: str, *, top1: Optional[str] = None,
                   lineage: Optional[Lineage] = None) -> CrossCheck:
    return CrossCheck(
        flag="not_evaluable", status="not_evaluable", resolution_basis=basis,
        matched_rank="none", taxonomic_distance="undetermined", rationale=rationale,
        bioclip_top1=top1, bioclip_lineage=lineage,
    )


def _top1_lineage(taxo: TaxonomicRead) -> Optional[Lineage]:
    """The prediction's own ranks, as BioCLIP reported them.

    Present on rows enriched after the hierarchy was captured; ``None`` on rows
    stored before that, whose distance is therefore ``undetermined`` until the
    backfill runs. Deliberately NOT inferred from the binomial — an inferred rank
    would be indistinguishable in the audit from one the model actually returned.
    """
    lineages = getattr(taxo, "topk_lineages", None)
    if not lineages:
        return None
    head = lineages[0]
    if head is None:
        return None
    return head if isinstance(head, Lineage) else Lineage.from_mapping(head)


def compute_crosscheck(
    upstream_label: Optional[str],           # raw upstream token, authoritative, untouched
    taxo: TaxonomicRead,
    alias_map: SpeciesAliasMap,
    target_taxonomy: Optional[TargetTaxonomy] = None,
    *,
    use_topk: bool = False,
) -> CrossCheck:
    """Return the two-state verdict plus its audit fields. Pure and deterministic.

    The default join is **top-1**: BioCLIP's single best guess against the upstream
    label. ``use_topk=True`` is a LAXER join, retained only to measure both
    strategies (docs/evaluation/bioclip_crosscheck_findings.md) and never the
    default — a correct species buried deep in the top-k at a tiny score flipping to
    'agree' is a threshold change, not a better check. It loosens WHERE in the
    ranking a match may be found; it never loosens the species-level rank at which a
    match counts, and a top-k match is recorded under its own resolution basis with
    the rank and score in the rationale so it can never pass for a top-1 agreement.
    """
    if not upstream_label:
        return _not_evaluable(
            "upstream_label_unusable", "no upstream label to compare against")
    if not taxo.ok:
        return _not_evaluable(
            "bioclip_unavailable",
            f"BioCLIP did not produce a result ({taxo.error or 'unknown error'})")
    if not taxo.topk:
        return _not_evaluable("bioclip_unavailable", "BioCLIP returned no candidate taxa")

    canonical_key = alias_map.canonical_key_for_token(upstream_label)
    if canonical_key is None:
        return _not_evaluable(
            "upstream_label_unusable",
            f"upstream token '{upstream_label}' is not mappable to a binomial "
            f"(alias-table coverage limitation)")
    # Person/Car/CalibrationPole are outside BioCLIP's Tree of Life. Comparing them
    # would manufacture a permanent false 'disagree' about a species BioCLIP was
    # never able to name (Decision 2), so the comparison is declared impossible.
    if not alias_map.is_bioclip_comparable(upstream_label):
        return _not_evaluable(
            "outside_bioclip_taxonomy",
            f"class '{upstream_label}' is outside BioCLIP's taxonomy "
            f"(no scientific binomial / not in the Tree of Life)")

    upstream_binomial = alias_map.binomial_for_token(upstream_label)
    if upstream_binomial is None:
        # Comparable but non-binomial should be unreachable; guard defensively.
        return _not_evaluable(
            "upstream_label_unusable",
            f"class '{upstream_label}' has no scientific binomial to compare")

    bioclip_label = taxo.topk[0][0]
    lineage = _top1_lineage(taxo)
    target = target_taxonomy.lineage_for(upstream_binomial) if target_taxonomy else None
    distance = taxonomic_distance(target, lineage)

    if use_topk:
        for rank, (label, score) in enumerate(taxo.topk[1:], start=2):
            if alias_map.binomial_for_bioclip(label) == upstream_binomial:
                return CrossCheck(
                    flag="agree", status="evaluable",
                    resolution_basis="topk_species_match", matched_rank="species",
                    taxonomic_distance="same_species",
                    rationale=(f"upstream {upstream_binomial} found in BioCLIP "
                               f"top-{len(taxo.topk)} at rank {rank}, score {score:.4f} "
                               f"[top-k join — laxer than top-1]"),
                    bioclip_top1=bioclip_label, bioclip_lineage=lineage,
                )

    bioclip_binomial = alias_map.binomial_for_bioclip(bioclip_label)
    if bioclip_binomial == upstream_binomial:
        # A trinomial that collapsed to the target (e.g. 'Columba livia domestica')
        # is still the same species; the basis records which route was taken so the
        # two are auditable apart.
        exact = len(bioclip_label.split()) == 2
        return CrossCheck(
            flag="agree", status="evaluable",
            resolution_basis="exact_species_match" if exact else "subspecies_collapsed_match",
            matched_rank="species", taxonomic_distance="same_species",
            rationale=f"both resolve to {upstream_binomial}",
            bioclip_top1=bioclip_label, bioclip_lineage=lineage,
        )

    if bioclip_binomial is None:
        # Outside the 26-species alias table. This is a DISAGREEMENT, not a gap in
        # the check: BioCLIP named something, and it was not the upstream species.
        # The distance says how far off it was, which is the reportable finding.
        return CrossCheck(
            flag="disagree", status="evaluable",
            resolution_basis="unresolved_prediction", matched_rank="none",
            taxonomic_distance=distance,
            rationale=(f"upstream '{upstream_label}' -> {upstream_binomial}, but BioCLIP's "
                       f"top-1 '{bioclip_label}' is a different taxon ({distance})"),
            bioclip_top1=bioclip_label, bioclip_lineage=lineage,
        )

    return CrossCheck(
        flag="disagree", status="evaluable",
        resolution_basis="no_taxonomic_match", matched_rank="none",
        taxonomic_distance=distance,
        rationale=(f"upstream '{upstream_label}' -> {upstream_binomial}, "
                   f"but BioCLIP '{bioclip_label}' -> {bioclip_binomial} ({distance})"),
        bioclip_top1=bioclip_label, bioclip_lineage=lineage,
    )


def compute_agreement(
    upstream_label: Optional[str],
    taxo: TaxonomicRead,
    alias_map: SpeciesAliasMap,
    target_taxonomy: Optional[TargetTaxonomy] = None,
    *,
    use_topk: bool = False,
) -> tuple[Flag, str]:
    """``(flag, rationale)`` — the narrow view, for callers that only need the
    verdict (the ``bioclip`` CLI command). Delegates to :func:`compute_crosscheck`
    so there is exactly one implementation of the decision."""
    result = compute_crosscheck(upstream_label, taxo, alias_map, target_taxonomy,
                                use_topk=use_topk)
    return result.flag, result.rationale
