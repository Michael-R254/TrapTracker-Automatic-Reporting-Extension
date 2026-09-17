"""BioCLIP's independence, asserted as a SIGNATURE rather than as a behaviour.

WHY THIS IS LOAD-BEARING, AND NOT AN INCIDENTAL DETAIL OF THE INTERFACE
----------------------------------------------------------------------
Decision 1 makes BioCLIP a cross-check, never a replacement: the upstream class
token stays authoritative and enrichment never mutates it. That decision is only
worth anything if BioCLIP's answer is arrived at *independently* — a second,
unprompted pair of eyes. A BioCLIP that had been told what TrapTracker thinks the
species is would not be corroborating the label; it would be agreeing with it, and
every `agree` in the corpus would be uninterpretable.

Nothing in the runtime enforces that. What enforces it is the shape of one method:

    TaxonomicEnricher.classify(self, original_image: bytes) -> TaxonomicRead

Pixels in, taxon out. There is no parameter through which a label, a hint, a prior,
or an upstream prediction could arrive, so the independence is structural rather
than a rule someone has to remember. `pipeline.py` consults the alias map only
*after* `classify` returns (`pipeline.py:97-104`), and the crop path hands over
image bytes alone (`crop/runner.py:115-126`).

This also underwrites a design decision beyond the enricher itself. Per-project
alias tables are safe *because* the alias table is a post-hoc join key that is
structurally downstream of BioCLIP — it can change which binomial an upstream token
maps to, but it cannot change what BioCLIP was shown. That argument collapses the
moment `classify` grows a second parameter.

So the failure mode this guards is not a crash. It is a plausible-looking widening
— `classify(image, hint=None)`, `classify(image, upstream_label=None)` — added in
good faith for some later feature, defaulting to None, harmless in the diff, and
quietly fatal to the meaning of every agreement figure the dissertation reports.
A test failure forces that change to be argued for out loud.

DO NOT relax these assertions to make a refactor pass. Widening `classify` is a
decision to re-open Decision 1, not a test-maintenance chore.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from ttr.enrichment.base import TaxonomicEnricher, TaxonomicRead
from ttr.enrichment.bioclip_enricher import BioClipEnricher


def _classify_implementations():
    """The abstract contract plus every concrete implementation of it.

    Checking the ABC alone would let a subclass override `classify` with a wider
    signature and pass; checking implementations alone would let the contract
    itself drift. Both are asserted.
    """
    subclasses = TaxonomicEnricher.__subclasses__()
    assert BioClipEnricher in subclasses, (
        "BioClipEnricher is no longer a TaxonomicEnricher — this test can no "
        "longer see the class it exists to guard")
    return [TaxonomicEnricher, *subclasses]


@pytest.mark.parametrize("cls", _classify_implementations(),
                         ids=lambda c: c.__name__)
def test_classify_takes_pixels_and_nothing_else(cls):
    """One positional parameter beyond self, annotated `bytes`.

    See this module's docstring: the absence of a second parameter is what makes
    BioCLIP's read independent of TrapTracker's label.
    """
    sig = inspect.signature(cls.classify)
    params = [p for name, p in sig.parameters.items() if name != "self"]

    assert len(params) == 1, (
        f"{cls.__name__}.classify takes {len(params)} parameters besides self: "
        f"{[p.name for p in params]}. BioCLIP must receive pixels and nothing "
        f"else — a second parameter is a channel through which TrapTracker's "
        f"label could reach it. See Decision 1.")

    (only,) = params
    assert only.kind in (inspect.Parameter.POSITIONAL_ONLY,
                         inspect.Parameter.POSITIONAL_OR_KEYWORD), (
        f"{cls.__name__}.classify's parameter is {only.kind}, not a plain "
        f"positional one")
    assert only.default is inspect.Parameter.empty, (
        f"{cls.__name__}.classify's image parameter has a default — the image is "
        f"mandatory; an optional one invites a call that passes something else")

    # *args / **kwargs would re-open the door this test exists to close.
    kinds = {p.kind for p in sig.parameters.values()}
    assert inspect.Parameter.VAR_POSITIONAL not in kinds, (
        f"{cls.__name__}.classify accepts *args — arbitrary extra arguments")
    assert inspect.Parameter.VAR_KEYWORD not in kinds, (
        f"{cls.__name__}.classify accepts **kwargs — arbitrary extra arguments")

    # Resolved, not compared as a string: `from __future__ import annotations`
    # makes every annotation in these modules a str, so `== "bytes"` would pass
    # for an unrelated symbol that happened to be spelled the same.
    hints = typing.get_type_hints(cls.classify)
    assert hints.get(only.name) is bytes, (
        f"{cls.__name__}.classify's image parameter is annotated "
        f"{hints.get(only.name)!r}, not bytes. Anything richer than bytes is a "
        f"container that could carry a label alongside the image.")
    assert hints.get("return") is TaxonomicRead, (
        f"{cls.__name__}.classify returns {hints.get('return')!r}, not TaxonomicRead")


def test_the_parameter_is_still_named_for_the_image():
    """A weak assertion deliberately kept separate from the strong ones above.

    The name carries no runtime meaning, so this is documentation that fails
    loudly rather than a correctness check — but a rename away from
    `original_image` is exactly the edit that would accompany a change of purpose,
    and it costs nothing to notice it.
    """
    sig = inspect.signature(TaxonomicEnricher.classify)
    names = [n for n in sig.parameters if n != "self"]
    assert names == ["original_image"], (
        f"the contract's parameter is now named {names} — if that reflects a "
        f"change of purpose rather than a rename, see Decision 1 first")
