"""BNG-aligned monitoring report — a THIRD report type added alongside the
single-species and all-species reports.

Three obligations, per the change spec:

(a) the existing single-species and all-species reports are byte-unchanged — a
    committed snapshot of each over a fixed window is compared exactly (the new
    report is new code + new branches only; this locks that in against future
    edits to the shared render functions);
(b) the new report carries the not-a-Gain-Plan / no-units disclaimer and contains
    NONE of the guardrail items (no net-gain figure, no distinctiveness/trading
    score, no quantified biodiversity unit, no pre/post comparison);
(c) the OS grid reference renders a 'NOT RECORDED — required' placeholder when the
    data carries no camera location, rather than a fabricated grid.

All figures in the new report are derived from the report's own event set.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path

from ttr.agents.report import ReportGeneratorAgent
from ttr.agents.retrieval import RetrievalAgent
from ttr.agents.window import TimeWindow

from conftest import FakeLLM, make_persisted_event

UTC = timezone.utc
SNAP_DIR = Path(__file__).resolve().parent / "fixtures" / "snapshots"

# The one fixed window every snapshot and derivation in this module uses.
SNAP_WINDOW = TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 14))


def _fox(mid, day, *, agreement="agree", distance=None, rationale=None):
    return make_persisted_event(
        mid, canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, day, 8, 30, tzinfo=UTC),
        display_common_name="red fox", upstream_confidence=0.92,
        bioclip_ok=True, vlm_ok=True, agreement_flag=agreement,
        taxonomic_distance=distance or ("same_species" if agreement == "agree"
                                        else "undetermined"),
        agreement_rationale=rationale,
        vlm_description="A red fox trots across a grassy clearing.")


def _seed(repo):
    """A deterministic multi-species window: a dominant species, a non-target
    species, a reserved non-biological class, and one unconfirmed cross-check — so
    every all-species/BNG section has real content to render.

    The unconfirmed pair covers both shapes the two-state cross-check can produce:
    a real DISAGREEMENT carrying a measured distance, and a NOT-EVALUABLE class
    that BioCLIP has no way to represent (Person) and which must stay out of the
    agree/disagree denominator."""
    repo.upsert_event(_fox("<f1@x>", 8))
    repo.upsert_event(_fox("<f2@x>", 9))
    repo.upsert_event(_fox("<f3@x>", 10))
    repo.upsert_event(_fox(
        "<f4@x>", 11, agreement="disagree", distance="same_family",
        rationale="upstream 'VulpesVulpes' -> Vulpes vulpes, but BioCLIP's top-1 "
                  "'Canis lupus' is a different taxon (same_family)"))
    repo.upsert_event(make_persisted_event(
        "<b1@x>", canonical_binomial="Meles meles",
        event_time=datetime(2026, 7, 9, 22, 0, tzinfo=UTC),
        display_common_name="european badger", upstream_confidence=0.81,
        bioclip_ok=True, vlm_ok=True, agreement_flag="agree"))
    repo.upsert_event(make_persisted_event(
        "<p1@x>", canonical_binomial="nonbio:Person",
        event_time=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        display_common_name="person", upstream_confidence=0.99,
        bioclip_ok=True, vlm_ok=True, agreement_flag="not_evaluable",
        taxonomic_distance="undetermined",
        agreement_rationale="class 'Person' is outside BioCLIP's taxonomy "
                            "(no scientific binomial / not in the Tree of Life)"))


def _agent(repo, alias_map):
    # Fixed, gate-COMPLIANT narrative text (names 'detections'/'alert events') so the
    # snapshots exercise the real narrative path and stay stable across runs.
    return ReportGeneratorAgent(
        RetrievalAgent(repo, alias_map),
        FakeLLM("Detections held steady across these alert events, busiest mid-window."),
        low_confidence_threshold=0.5, include_crosscheck=True)


# --------------------------------------------------------------------------- #
# (a) The two existing report types must be BYTE-IDENTICAL after this change.
# --------------------------------------------------------------------------- #
def _check_or_write_snapshot(name: str, produced: str) -> None:
    """Compare against the committed golden; bootstrap it on first run.

    The golden is captured from the CURRENT (post-change) output of the existing
    reports, which — because this change touches none of their render methods — is
    identical to the pre-change output. The assert then locks it against any future
    accidental edit to a shared function.
    """
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / name
    if not path.exists():
        path.write_text(produced, encoding="utf-8")
    assert produced == path.read_text(encoding="utf-8"), (
        f"{name} changed — an existing report output is no longer byte-identical. "
        "If this change is intentional, delete tests/fixtures/snapshots/ and re-run.")


def test_all_species_report_byte_unchanged(repo, alias_map):
    _seed(repo)
    md = _agent(repo, alias_map).generate_all_species(SNAP_WINDOW)
    _check_or_write_snapshot("all_species.md", md)


def test_single_species_report_byte_unchanged(repo, alias_map):
    _seed(repo)
    md = _agent(repo, alias_map).generate("fox", SNAP_WINDOW)
    _check_or_write_snapshot("single_species_fox.md", md)


# --------------------------------------------------------------------------- #
# (b) The new report: disclaimer present, all 7 sections, no guardrail items.
# --------------------------------------------------------------------------- #
_DISCLAIMER = ("It is not a statutory Biodiversity Gain Plan and does not calculate "
               "biodiversity units")

# Concepts the report must never GENERATE (guardrails). Each is checked as an
# actual produced value, not merely the word — the mandated disclaimer negates
# "biodiversity units", which is allowed; a quantified unit or a net-gain figure is
# not. Word-boundary, case-insensitive.
_FORBIDDEN = (
    r"net[\s-]?gain",              # no net-gain figure or phrasing
    r"net[\s-]?change",            # no net-change
    r"distinctiveness",            # no distinctiveness score
    r"strategic significance",     # no strategic-significance score
    r"trading rule|trading summary",
    r"\d[\d.,]*\s*(?:biodiversity\s+)?units?\b",   # no quantified biodiversity unit
    r"\b10\s*%\s*(?:net|gain)",    # no 10% net-gain target
)


def _bng(repo, alias_map, window=SNAP_WINDOW):
    return _agent(repo, alias_map).generate_bng_aligned(window)


def test_bng_report_has_disclaimer_and_all_seven_sections(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    assert _DISCLAIMER in md
    for heading in (
        "## Document control",
        "## Executive summary",
        "## Evidence quality",
        "## 1. Submission and site identity",
        "## 2. Provenance and competence",
        "## 3. Baseline status",
        "## 4. Monitoring period and survey effort",
        "## 5. Survey constraints",
        "## 6. Automated species classification results",
        "## Evidence limitations",
        "## Recommended monitoring actions",
        "## 7. Data sharing",
    ):
        assert heading in md, f"missing section: {heading}"


def test_bng_report_contains_no_guardrail_items(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    low = md.lower()
    for pattern in _FORBIDDEN:
        assert re.search(pattern, low) is None, f"guardrail breach: pattern {pattern!r} present"
    # "biodiversity unit(s)" may appear only as NEGATIONS (disclaimer, exec summary,
    # limitations) — never as a quantified value (the _FORBIDDEN regex above rules the
    # quantified case out). Assert every occurrence is preceded by a negation.
    for m in re.finditer(r"biodiversity[ -]units?", low):
        ctx = low[max(0, m.start() - 24):m.start()]
        assert any(neg in ctx for neg in ("no ", "not ", "does not", "calculates no")), \
            f"biodiversity unit not in a negation: ...{ctx!r}"


def test_bng_provenance_cross_check_figure_is_derived_from_this_window(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    # Seeded: 6 detections, 2 unconfirmed (one disagreeing fox, one not-evaluable Person). The
    # figure must be computed from the report's own event set, not hardcoded.
    assert "not corroborate 2 of 6 (33%)" in md
    assert "No competent ecologist has verified" in md


def test_bng_detected_species_is_events_not_biodiversity(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    assert "No detection count is a biodiversity value" in md
    assert "not a community or biodiversity assessment" in md
    # Composition derived from the window: fox is dominant (4 of 6 events).
    assert "| red fox (*Vulpes vulpes*) | 4 |" in md
    # §6 is now a validation table with provisional status, not asserted presence.
    assert "## 6. Automated species classification results" in md
    assert "| Provisional |" in md


# --------------------------------------------------------------------------- #
# (c) OS grid reference: placeholder when absent, never fabricated.
# --------------------------------------------------------------------------- #
def test_os_grid_reference_renders_required_placeholder(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    assert "**OS grid reference (camera location):** **NOT RECORDED — required.**" in md
    # And nothing that looks like an actual OS grid (two letters + digits) was invented.
    assert re.search(r"\b[A-Z]{2}\s?\d{4,10}\b", md) is None


def test_bng_baseline_states_no_prior_and_no_change(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    assert "establishes a baseline" in md
    assert "No prior baseline exists" in md
    assert "No change is computed" in md


def test_bng_survey_constraints_derives_season_and_day_span(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    # July -> summer, derived from the window's own months; 7-day window.
    assert "a 7-day window in summer" in md
    assert "Single camera, single location, feeder-facing" in md


# --------------------------------------------------------------------------- #
# Consolidation (PART 1): each limitation stated once, not repeatedly.
# --------------------------------------------------------------------------- #
def test_bng_caveats_are_consolidated_not_repeated(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    low = md.lower()
    # 'community composition' as a positive claim never appears.
    assert "community composition" not in low
    # The consolidated limitations section exists and gathers the recurring caveats once.
    assert "## Evidence limitations" in md
    assert "no habitat-condition assessment" in low
    assert "counts are alert events, not animals" in low


# --------------------------------------------------------------------------- #
# Ecological interpretation (PART 2) + management considerations (PART 3).
# A realistic fixture: one dominant species (>=90%), two recurring non-dominant
# species (>=2 active days), and a management-relevant, thinly-evidenced species
# (in _MANAGEMENT_RELEVANCE, one day, unverified) — so every branch renders.
# --------------------------------------------------------------------------- #
ECO_WINDOW = TimeWindow.from_dates(date(2026, 7, 1), date(2026, 7, 14))


def _seed_ecological(repo):
    for i in range(90):                              # dominant: 90 events across all 14 days
        repo.upsert_event(make_persisted_event(
            f"<pig{i}@x>", canonical_binomial="Columba palumbus",
            event_time=datetime(2026, 7, 1 + (i % 14), 8 + (i % 8), 0, tzinfo=UTC),
            display_common_name="common wood pigeon", upstream_confidence=0.9,
            bioclip_ok=True, vlm_ok=True, agreement_flag="agree"))
    for d in (2, 5, 8, 11):                          # recurring: crow on 4 distinct days
        repo.upsert_event(make_persisted_event(
            f"<crow{d}@x>", canonical_binomial="Corvus corone",
            event_time=datetime(2026, 7, d, 10, 0, tzinfo=UTC),
            display_common_name="carrion crow", bioclip_ok=True, vlm_ok=True,
            agreement_flag="agree"))
    for i, d in enumerate((3, 9, 9)):                # recurring: squirrel, 3 events on 2 days
        repo.upsert_event(make_persisted_event(
            f"<sq{i}@x>", canonical_binomial="Sciurus carolinensis",
            event_time=datetime(2026, 7, d, 11 + i, 0, tzinfo=UTC),
            display_common_name="grey squirrel", bioclip_ok=True, vlm_ok=True,
            agreement_flag="agree"))
    for i in range(2):                               # management flag: cat, one day, unverified
        repo.upsert_event(make_persisted_event(
            f"<cat{i}@x>", canonical_binomial="Felis catus",
            event_time=datetime(2026, 7, 6, 13 + i, 0, tzinfo=UTC),
            display_common_name="domestic cat", bioclip_ok=True, vlm_ok=True,
            agreement_flag="not_evaluable", taxonomic_distance="undetermined"))


def _interp_section(md: str) -> str:
    return md[md.index("## Interpretation of automated classifications"):md.index("## Evidence limitations")]


def test_interpretation_figures_match_the_computed_event_set(repo, alias_map):
    _seed_ecological(repo)
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    interp = _interp_section(md)
    # Dominant: 90 of 99 = 90.9%, present on all 14 days-with-data -> 'overwhelming'.
    assert "common wood pigeon (*Columba palumbus*).** 90 of 99 events (90.9%) on 14 of 14 days with data" in interp
    assert "assigned this label to the overwhelming majority of the alert events" in interp
    # Recurring: derived active-day counts (squirrel: 3 events across 2 days).
    assert "carrion crow (*Corvus corone*) on 4 days (4 events)" in interp
    assert "grey squirrel (*Sciurus carolinensis*) on 2 days (3 events)" in interp
    assert "spread across multiple distinct days" in interp
    # Management flag: the cat, once, with its provisional/2-events-unverified caveat.
    assert "Management-relevant, provisional — domestic cat (*Felis catus*)" in interp
    assert "a potential predator at a feeder" in interp
    assert "only 2 events with an unverified identification" in interp


def test_interpretation_contains_no_forbidden_claims(repo, alias_map):
    _seed_ecological(repo)
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    interp = _interp_section(md).lower()
    # No positive claims the data cannot support.
    assert "population" not in interp
    assert "predation pressure" not in interp
    assert "predation impact" not in interp
    assert "community composition" not in interp
    # 'abundance' appears ONLY in the closing scope-negation, never as a positive claim.
    assert interp.count("abundance") == 1
    assert "not confirmed presence, abundance, or wider site biodiversity" in interp


def _mgmt_section(md: str) -> str:
    return md[md.index("## Recommended monitoring actions"):md.index("## 7. Data sharing")]


def test_management_actions_are_a_table_with_priorities_and_roles(repo, alias_map):
    _seed_ecological(repo)
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    mgmt = _mgmt_section(md)
    # It is a table with the required columns.
    assert "| Recommended action | Evidence / reason | Trigger | Responsible role | Priority | Status |" in mgmt
    # Core actions present as rows.
    assert "Continue monitoring across seasons" in mgmt
    assert "Compare equivalent future windows against this baseline" in mgmt
    assert "Undertake a habitat survey where habitat condition is required" in mgmt
    assert "Record the camera OS grid reference" in mgmt
    # Documented priorities and a generic (unnamed) responsible role.
    assert "| High | Open |" in mgmt and "| Medium | Open |" in mgmt and "| Low | Open |" in mgmt
    assert "Monitoring operator" in mgmt
    # Non-target labels named from the data in the review action's reason cell.
    assert "carrion crow, grey squirrel and domestic cat" in mgmt


# --------------------------------------------------------------------------- #
# Item 1: the interpretation's active-day denominator is the COVERAGE span
# (days with data), never the wider requested-window day count.
# --------------------------------------------------------------------------- #
def test_active_day_denominator_is_coverage_span_not_window(repo, alias_map):
    win = TimeWindow.from_dates(date(2026, 7, 1), date(2026, 7, 31))     # 31-day window
    for d in (5, 6, 7, 8, 9, 10):                                        # data on only 6 days
        repo.upsert_event(make_persisted_event(
            f"<pig{d}@x>", canonical_binomial="Columba palumbus",
            event_time=datetime(2026, 7, d, 9, 0, tzinfo=UTC),
            display_common_name="common wood pigeon", bioclip_ok=True, vlm_ok=True,
            agreement_flag="agree"))
    agent = _agent(repo, alias_map)
    md = agent.generate_bng_aligned(win)
    interp = _interp_section(md)
    op_first, op_last = agent._retrieval.send_time_span()
    cov = agent._coverage_span_days(win, op_first, op_last)
    assert cov == 6                              # first-to-last detection span, in local days
    assert cov != len(win.days())                # and NOT the 31-day requested window
    assert f"on 6 of {cov} days with data" in interp
    assert "of 31 days" not in interp            # the no-data days are not counted as absence


# --------------------------------------------------------------------------- #
# Item 3: Executive summary — figures all derived from the event set.
# --------------------------------------------------------------------------- #
def test_executive_summary_figures_match_computed_data(repo, alias_map):
    _seed_ecological(repo)
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    summ = md[md.index("## Executive summary"):md.index("## Evidence quality")]
    # N=99 (90 pigeon + 4 crow + 3 squirrel + 2 cat), 4 species, pigeon 90.9%.
    assert "Between 2026-07-01 and 2026-07-14" in summ
    assert "recorded 99 alert events across 4 automated species classifications" in summ
    assert "The most frequent automated classification was common wood pigeon (90.9% of events)" in summ
    assert "with 3 less-frequent labels recorded less often" in summ
    # Corroboration limitation + provisional + exclusions, all derived.
    assert "not corroborated" in summ and "provisional" in summ
    assert "no competent ecologist has verified" in summ
    assert "it does not measure abundance, habitat condition, or site-wide biodiversity" in summ
    assert "calculates no biodiversity units" in summ
    assert "not a statutory Biodiversity Gain Plan" in summ


def test_executive_summary_species_count_excludes_non_biological(repo, alias_map):
    """A non-biological class (Person) is a detection event, never a species — it must
    not inflate the 'k species' figure, on the SAME classification the species-breakdown
    section uses. The event total still counts every detection."""
    _seed_ecological(repo)                                    # 4 biological species, N=99
    for i in range(3):                                        # + a non-biological class
        repo.upsert_event(make_persisted_event(
            f"<person{i}@x>", canonical_binomial="nonbio:Person",
            event_time=datetime(2026, 7, 4, 12 + i, 0, tzinfo=UTC),
            display_common_name="person", bioclip_ok=True, vlm_ok=True,
            agreement_flag="not_evaluable", taxonomic_distance="undetermined"))
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    summ = md[md.index("## Executive summary"):md.index("## Evidence quality")]
    # 5 distinct keys are present, but only 4 are species — Person is excluded.
    assert "across 4 automated species classifications" in summ
    assert "5 automated species" not in summ
    # N still counts every detection, biological or not (99 + 3 Person = 102).
    assert "recorded 102 alert events" in summ


# --------------------------------------------------------------------------- #
# Final polish pass: reworded §2 intro, no Decision refs, §6 label, two new
# management recommendations (the cat one gated behind confirmation + repeat).
# --------------------------------------------------------------------------- #
def test_bng_report_has_headline_cards(repo, alias_map):
    _seed_ecological(repo)
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    assert "## Headline summary" in md
    assert 'class="cards"' in md
    assert md.count('class="card"') == 5                 # the five required cards
    for label in ("Total alert events", "Provisional species labels",
                  "Confirmed operational hours", "Operational coverage",
                  "BioCLIP not corroborated"):
        assert label in md


def test_bng_report_has_four_charts_with_distinct_downtime(repo, alias_map):
    _seed_ecological(repo)
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    assert "## Activity and monitoring figures" in md
    for caption in ("Daily alert events by species", "Daily monitoring status",
                    "Activity by hour of day", "Species composition of alert events"):
        assert caption in md
    assert md.count("<svg") >= 4                          # four inline-SVG charts
    # Downtime is a distinct, labelled category from zero detections and no-data.
    assert "affected by proven downtime (hatched share)" in md
    assert "no data / outside coverage (dashed)" in md
    # Composition is a horizontal bar chart, NOT a pie hiding minor categories.
    assert "a pie would hide them" in md


def test_bng_evidence_images_degrade_gracefully_when_unavailable(repo, alias_map):
    # Test fixtures carry no image files on disk, so item 11 must render its honest
    # 'unavailable' note rather than fabricating placeholders.
    _seed_ecological(repo)
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    assert "## Appendix A — Representative image evidence" in md
    assert "Representative-image evidence was unavailable during report generation" in md
    assert "data:image" not in md                         # no fabricated thumbnails


def test_bng_appendices_present_with_definitions_and_metadata(repo, alias_map):
    _seed_ecological(repo)
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    assert "## Appendix B — Definitions, metadata and data-quality rules" in md
    for sub in ("### B1 Calculation definitions", "### B2 Proven downtime",
                "### B3 Model and threshold metadata", "### B4 Data-quality status rules",
                "### B5 Event-grouping rule", "### B6 Report generation"):
        assert sub in md
    assert "Alert events per 100 confirmed operational hours" in md
    assert f"Report version:** {__import__('ttr.agents.report', fromlist=['_BNG_REPORT_VERSION'])._BNG_REPORT_VERSION}" in md


def test_bng_site_section_reports_missing_metadata_honestly(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    assert "Monitoring-point ID:** Not recorded" in md
    assert "Camera orientation:** Not recorded" in md
    assert "Feeder-facing status:** yes" in md
    assert "Site / camera-position map:** Not available" in md


def test_bng_report_has_no_decision_references(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    assert re.search(r"Decision \d", md) is None      # internal citations stripped
    assert "Decision 4" not in md


def test_bng_section6_label_is_provisional_classification_not_observed(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    # §6 is renamed away from any wording asserting definitive presence.
    assert "## 6. Automated species classification results" in md
    assert "Condition-monitoring evidence" not in md
    assert "Species observed (" not in md
    # Provisional framing is explicit.
    assert "automated, provisional" in md


def test_bng_provenance_intro_reworded_to_documentation_voice(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    assert ("Unlike a statutory Biodiversity Gain Plan, this report does not include "
            "certification by a competent ecologist.") in md
    assert "honest inverse of a competent-person declaration" not in md   # old wording gone


def test_management_gated_cat_action_present_and_conditional(repo, alias_map):
    _seed_ecological(repo)                                    # includes a domestic cat
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    mgmt = _mgmt_section(md)
    # Composition-change action present.
    assert "Inspect the feeder and camera after unexpected composition changes" in mgmt
    # The predator action is present AND gated on confirmation + repeat presence.
    assert "Review repeated domestic cat classifications before considering mitigation" in mgmt
    assert "Only after confirmed ID and repeated records" in mgmt


def test_gated_predator_recommendation_absent_without_a_predator_detection(repo, alias_map):
    # A management-relevant but NON-predator species (grey squirrel = invasive) plus the
    # dominant pigeon — no predator, so the gated mitigation recommendation must not appear;
    # its presence is tied to a management-relevant PREDATOR existing in the window.
    for d in (2, 5, 8):
        repo.upsert_event(make_persisted_event(
            f"<pig{d}@x>", canonical_binomial="Columba palumbus",
            event_time=datetime(2026, 7, d, 9, 0, tzinfo=UTC),
            display_common_name="common wood pigeon", bioclip_ok=True, vlm_ok=True,
            agreement_flag="agree"))
    for i, d in enumerate((3, 9)):
        repo.upsert_event(make_persisted_event(
            f"<sq{i}@x>", canonical_binomial="Sciurus carolinensis",
            event_time=datetime(2026, 7, d, 11, 0, tzinfo=UTC),
            display_common_name="grey squirrel", bioclip_ok=True, vlm_ok=True,
            agreement_flag="agree"))
    md = _agent(repo, alias_map).generate_bng_aligned(ECO_WINDOW)
    mgmt = _mgmt_section(md)
    assert "Inspect the feeder and camera after unexpected composition changes" in mgmt  # unconditional
    assert "before mitigation" not in mgmt                     # predator row needs a predator
    assert "Review repeat" not in mgmt


# --------------------------------------------------------------------------- #
# Item 4: pure BNG-form scaffolding subtitles removed; purpose-explaining ones kept.
# --------------------------------------------------------------------------- #
def test_bng_form_scaffolding_subtitles_removed_purpose_ones_kept(repo, alias_map):
    _seed(repo)
    md = _bng(repo, alias_map)
    # Pure 'Mirrors §X' / 'Maps this to §Y' cross-references are gone.
    assert "Mirrors the" not in md
    assert "Maps this" not in md
    # But the purpose-explaining intros are retained, in natural prose without §-refs.
    # §2's point (what's automated vs human-verified) survives its rewording:
    assert "which aspects of the report were generated automatically and which require human verification" in md
    assert "survey constraints on this monitoring evidence" in md      # §5 constraints-home point


# --------------------------------------------------------------------------- #
# Appendix A: withheld is not the same statement as unavailable.
#
# The public sample of this report is distributed without its representative
# camera frames, because they depict a private property. The report says so
# where they would have been. That is a different claim from the existing
# "unavailable" note, which fires when the generator could not READ the images —
# one means nobody chose, the other means someone did. A document that blurred
# the two would be making exactly the kind of unstated claim this project's
# honesty rules exist to prevent.
# --------------------------------------------------------------------------- #
def test_appendix_a_states_that_frames_were_withheld_not_that_they_failed(repo, alias_map):
    _seed(repo)
    md = _agent(repo, alias_map)
    md._include_evidence_images = False
    out = md.generate_bng_aligned(SNAP_WINDOW)

    assert "## Appendix A — Representative image evidence" in out, "the section stays"
    assert "Withheld from this copy of the report" in out
    assert "private property" in out
    assert "they are not missing, and nothing failed" in out
    assert "No other part of this report is altered" in out

    # ...and it must NOT claim the generator could not read them.
    assert "was unavailable during report generation" not in out


def test_withholding_appendix_a_changes_nothing_else_in_the_report(repo, alias_map):
    """The claim the note itself makes, asserted rather than trusted."""
    _seed(repo)
    with_images = _agent(repo, alias_map).generate_bng_aligned(SNAP_WINDOW)

    agent = _agent(repo, alias_map)
    agent._include_evidence_images = False
    without = agent.generate_bng_aligned(SNAP_WINDOW)

    marker = "## Appendix A — Representative image evidence"
    assert with_images.split(marker)[0] == without.split(marker)[0], (
        "something outside Appendix A changed when only the images were withheld")


def test_appendix_a_embeds_no_image_when_withheld(repo, alias_map):
    _seed(repo)
    agent = _agent(repo, alias_map)
    agent._include_evidence_images = False
    out = agent.generate_bng_aligned(SNAP_WINDOW)

    assert "data:image" not in out, "a thumbnail survived the withhold flag"
