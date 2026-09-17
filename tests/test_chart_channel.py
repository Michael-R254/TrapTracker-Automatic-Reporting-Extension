"""Charts reach the page by a route model output cannot take.

Every chart is replaced in the document by an opaque per-render token and held
aside; the real markup goes in at the very end. The web path puts it in AFTER
sanitising, so the charts are not governed by the allowlist that guards the
document — which is what lets the next stage forbid SVG in model-authored text
without taking the real charts with it.

Nothing about the rendered output changes at this stage. That is the claim these
tests exist to check, and they check it by BYTE COMPARISON against the same
report built the old way, on both paths, for all three report types. A stage that
is supposed to change nothing is exactly the kind that quietly changes something.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import markdown as _markdown
import nh3
import pytest

from ttr.agents.report import ChartSlots, ReportGeneratorAgent, _substitute
from ttr.agents.retrieval import RetrievalAgent
from ttr.agents.window import TimeWindow
from ttr.species.aliases import SpeciesAliasMap
from ttr.storage.repository import DetectionRepository
from ttr.web.app import (_ALLOWED_ATTRS, _ALLOWED_TAGS, _CHART_ATTRS, _CHART_TAGS,
                         _URL_SCHEMES)

from conftest import EXAMPLE_ALIAS_PATH, FakeLLM, make_persisted_event

UTC = timezone.utc
NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

#: (label, render method, generate method, args) for all three report types.
REPORT_TYPES = [
    ("single-species", "render", "generate", "species"),
    ("all-species", "render_all_species", "generate_all_species", "window"),
    ("bng-aligned", "render_bng_aligned", "generate_bng_aligned", "window"),
]


@pytest.fixture
def seeded(tmp_path):
    repo = DetectionRepository(tmp_path / "charts.db")
    for i in range(6):
        repo.upsert_event(make_persisted_event(
            f"<e{i}@x>", canonical_binomial="Vulpes vulpes",
            event_time=NOW - timedelta(days=i), display_common_name="red fox",
            bioclip_ok=True, vlm_ok=True,
            agreement_flag="agree" if i % 2 else "disagree",
            vlm_description="A fox at the feeder."))
    yield repo
    repo.close()


@pytest.fixture
def window():
    return TimeWindow.from_dates((NOW - timedelta(days=7)).date(), NOW.date())


def _agent(repo):
    return ReportGeneratorAgent(
        RetrievalAgent(repo, SpeciesAliasMap.from_yaml(EXAMPLE_ALIAS_PATH)),
        FakeLLM("A steady week."), low_confidence_threshold=0.5,
        include_crosscheck=True)


def _to_html(md: str) -> str:
    """Exactly what `_render_report_body` does to the document."""
    return nh3.clean(_markdown.markdown(md, extensions=["tables"]),
                     tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS,
                     url_schemes=_URL_SCHEMES)


def _to_html_pre_stage_3(md: str) -> str:
    """The document sanitised under the policy that stood BEFORE the narrowing.

    Stage 2 proved it changed nothing by rendering the old way — charts inline,
    whole document through one permissive sanitiser — and comparing. Stage 3
    removed SVG from the document policy, so that comparison can no longer be
    made with the current one: the old arrangement would now have its charts
    stripped, and the test would "pass" by both sides losing them.

    `_CHART_TAGS` IS the old document policy, unchanged and now applied only to
    generator output, so it reconstructs the old rendering exactly. The byte
    comparison therefore still means what it meant: what a reader sees has not
    moved across either stage.
    """
    return nh3.clean(_markdown.markdown(md, extensions=["tables"]),
                     tags=_CHART_TAGS, attributes=_CHART_ATTRS,
                     url_schemes=_URL_SCHEMES)


def _normalise(markup: str) -> str:
    """The chart policy the web path applies before injecting."""
    return nh3.clean(markup, tags=_CHART_TAGS, attributes=_CHART_ATTRS,
                     url_schemes=_URL_SCHEMES)


def _call(agent, method, kind, window):
    fn = getattr(agent, method)
    return fn("fox", window) if kind == "species" else fn(window)


# --------------------------------------------------------------------------- #
# The charts leave the document.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("label,render,generate,kind", REPORT_TYPES)
def test_the_document_carries_tokens_not_chart_markup(seeded, window, label,
                                                      render, generate, kind):
    rendered = _call(_agent(seeded), render, kind, window)

    assert rendered.charts, f"{label} produced no charts to hold"
    assert "<svg" not in rendered.markdown, "chart markup stayed in the document"
    for token in rendered.charts:
        assert token.startswith("ttrchart")
    assert all("<svg" in m or "<div" in m for m in rendered.charts.values())


def test_every_generator_hands_its_chart_over(seeded, window):
    """NINE generators, not the seven the stage was scoped around.

    `_counts_chart` (the day-by-day bars) and `_comparison_chart`
    (period-over-period) also emit SVG into the document and were not on the
    list. Both are held too: a chart left inline would survive this stage
    unnoticed and then vanish in the next one, when the document allowlist stops
    permitting svg — which is precisely the failure this channel exists to
    prevent, arriving by the back door.
    """
    agent = _agent(seeded)
    held = []
    for _label, render, _gen, kind in REPORT_TYPES:
        held.extend(_call(agent, render, kind, window).charts.values())

    joined = "".join(held)
    for marker in ["Daily alert events by species", "Daily monitoring status",
                   "Activity by hour of day", "Species composition of alert events"]:
        assert marker in joined, f"no chart held for {marker!r}"
    assert joined.count("<svg") >= 9, "fewer SVG blocks held than there are generators"


# --------------------------------------------------------------------------- #
# And come back byte-identically, on both paths.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("label,render,generate,kind", REPORT_TYPES)
def test_the_markdown_is_byte_identical_to_the_old_output(seeded, window, label,
                                                          render, generate, kind):
    """The CLI path. `generate_*` substitutes immediately, so this is exact."""
    rendered = _call(_agent(seeded), render, kind, window)
    direct = _call(_agent(seeded), generate, kind, window)

    assert rendered.as_markdown() == direct
    assert "ttrchart" not in direct, "a token escaped into the CLI document"


@pytest.mark.parametrize("label,render,generate,kind", REPORT_TYPES)
def test_the_html_is_byte_identical_to_the_old_output(seeded, window, label,
                                                      render, generate, kind):
    """The web path: sanitise the document, THEN put the charts in.

    Compared against the old arrangement — charts inline, whole document through
    the sanitiser — which is the only comparison that can show this stage changed
    nothing that reaches a browser.
    """
    rendered = _call(_agent(seeded), render, kind, window)
    new = rendered.substitute_into(_to_html(rendered.markdown), normalise=_normalise)
    old = _to_html_pre_stage_3(_call(_agent(seeded), generate, kind, window))

    assert new == old, f"{label} rendered differently through the chart channel"
    assert "ttrchart" not in new, "a token reached the response"


# --------------------------------------------------------------------------- #
# The nonce is what stops a model claiming a chart slot.
# --------------------------------------------------------------------------- #
def test_the_token_is_a_fresh_nonce_every_render(seeded, window):
    first = _agent(seeded).render("fox", window)
    second = _agent(seeded).render("fox", window)

    assert set(first.charts) != set(second.charts), (
        "tokens repeated across renders — a model that saw one could reuse it")


def test_a_model_emitted_placeholder_is_not_substituted(seeded, window):
    """The forgery attempt, end to end.

    A description carrying a plausible token — right prefix, right shape, wrong
    nonce — must render as inert text. A fixed sentinel would make this pass
    silently, which is exactly why the token is not one.
    """
    forged = "ttrchart" + "0" * 32 + "x0"
    seeded.upsert_event(make_persisted_event(
        "<forge@x>", canonical_binomial="Vulpes vulpes",
        event_time=NOW - timedelta(days=1), display_common_name="red fox",
        bioclip_ok=True, vlm_ok=True,
        vlm_description=f"A fox. {forged}"))

    rendered = _agent(seeded).render("fox", window)
    html = rendered.substitute_into(_to_html(rendered.markdown), normalise=_normalise)

    assert forged in html, "the forged token should survive as plain text"
    section = html.split("Unverified model descriptions")[1]
    assert "<svg" not in section, "a forged token claimed a chart slot"


def test_a_forged_token_is_not_substituted_even_with_the_right_index(seeded, window):
    """Only the nonce is secret, so only the nonce can be doing the work."""
    rendered = _agent(seeded).render("fox", window)
    real = next(iter(rendered.charts))
    forged = real[:8] + "f" * 32 + real[-2:]      # same shape, one nonce off

    assert forged != real
    out = _substitute(f"before {forged} after", rendered.charts, wrapper=True)
    assert out == f"before {forged} after", "a near-miss token was substituted"


# --------------------------------------------------------------------------- #
# Slot mechanics.
# --------------------------------------------------------------------------- #
def test_slots_hand_back_distinct_tokens_for_distinct_charts():
    slots = ChartSlots()
    a, b = slots.hold("<svg>a</svg>"), slots.hold("<svg>b</svg>")

    assert a != b
    assert slots.held == {a: "<svg>a</svg>", b: "<svg>b</svg>"}


def test_substitution_resolves_a_chart_held_inside_another():
    """A legend is held, then embedded in a chart that is itself held.

    One pass would leave the inner token showing. The fixed point is why it does
    not, and this is the case that requires it.
    """
    slots = ChartSlots()
    legend = slots.hold("<svg>legend</svg>")
    chart = slots.hold(f"<svg>chart {legend}</svg>")

    assert _substitute(chart, slots.held, wrapper=False) == "<svg>chart <svg>legend</svg></svg>"
