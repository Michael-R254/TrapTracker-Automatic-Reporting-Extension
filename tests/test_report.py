"""Stage 4 verification: the report renders day-by-day ALERT-EVENT counts, prints
the three unconditional honesty statements even when nothing is anomalous, flags
disagreements, and stays complete when the LLM fails."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from ttr.agents.report import ReportGeneratorAgent
from ttr.agents.retrieval import RetrievalAgent
from ttr.agents.window import TimeWindow

from conftest import FailingLLM, FakeLLM, make_persisted_event

UTC = timezone.utc


def _window():
    return TimeWindow(start_utc=datetime(2026, 7, 8, tzinfo=UTC),
                      end_utc=datetime(2026, 7, 14, tzinfo=UTC))


def _clean_fox(mid, day):
    """A fully-enriched, agreeing, high-confidence fox event (nothing anomalous)."""
    return make_persisted_event(
        mid, canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, day, 2, 0, tzinfo=UTC),
        display_common_name="red fox", upstream_confidence=0.92,
        bioclip_ok=True, vlm_ok=True, agreement_flag="agree",
        vlm_description="A red fox trots across a grassy clearing.")


def _report(repo, alias_map, llm, species="fox", window=None, include_crosscheck=True):
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), llm,
                                 low_confidence_threshold=0.5, include_crosscheck=include_crosscheck)
    return agent.generate(species, window or _window())


# --------------------------------------------------------------------------- #
# The three unconditional honesty statements — present even when clean.
# --------------------------------------------------------------------------- #
def test_honesty_statements_present_when_nothing_anomalous(repo, alias_map, fake_llm):
    repo.upsert_event(_clean_fox("<f1@x>", 9))
    repo.upsert_event(_clean_fox("<f2@x>", 9))
    md = _report(repo, alias_map, fake_llm)

    # Standing caveats are now tied to the concrete figures (2 events here).
    assert "2 alert events" in md                        # Decision 4, figure-tied
    assert "not 2 animals" in md
    assert "send time" in md.lower()                     # Decision 3
    # Decision 7 gate CLOSED for the NOMINAL path only — scope stated honestly.
    assert "validated field-for-field against a real captured alert email" in md.lower()
    assert "nominal" in md.lower()
    assert "reconstruction only" in md.lower()           # edge variants not over-claimed
    assert "always applies" not in md                    # legalese header dropped
    assert "None flagged" in md                          # genuinely nothing anomalous


def test_singular_lead_in_reads_naturally(repo, alias_map, fake_llm):
    repo.upsert_event(_clean_fox("<f1@x>", 9))               # exactly one dated event
    md = _report(repo, alias_map, fake_llm)
    assert "The **1 alert event** here is an alert-event count" in md
    assert "The **1 dated event** in the table above is" in md
    assert "Each of the **1" not in md                       # no clunky "Each of the 1 ..."


def test_standing_claims_are_verbatim_stable_across_reports(repo, alias_map, fake_llm):
    from ttr.agents.report import _COUNT_CLAIM, _DECISION7_CLAIM, _SEND_TIME_CLAIM

    repo.upsert_event(_clean_fox("<a@x>", 9))
    md1 = _report(repo, alias_map, fake_llm)
    repo.upsert_event(_clean_fox("<b@x>", 10))
    repo.upsert_event(_clean_fox("<c@x>", 11))
    md2 = _report(repo, alias_map, fake_llm)

    # Figures differ between the two reports...
    assert "1 alert event" in md1 and "3 alert events" in md2
    assert "always applies" not in md1                   # no legalese header
    # ...but the substantive claim wording is byte-identical in both (audit value).
    for claim in (_COUNT_CLAIM, _SEND_TIME_CLAIM, _DECISION7_CLAIM):
        assert claim in md1 and claim in md2


def test_day_by_day_counts(repo, alias_map, fake_llm):
    repo.upsert_event(_clean_fox("<f1@x>", 9))
    repo.upsert_event(_clean_fox("<f2@x>", 9))
    repo.upsert_event(_clean_fox("<f3@x>", 11))
    md = _report(repo, alias_map, fake_llm)

    assert "| 2026-07-09 | 2 |" in md
    assert "| 2026-07-11 | 1 |" in md
    assert "| 2026-07-10 | 0 |" in md                    # every day in window shown
    assert "**Alert events:** 3" in md
    assert "| **Total (dated)** | **3** |" in md         # table carries its own total


def test_day_by_day_chart_mirrors_the_table(repo, alias_map, fake_llm):
    # The chart is presentation of the SAME counts: one bar per non-zero day,
    # peak value in the aria-label, computed from the same figures as the table.
    repo.upsert_event(_clean_fox("<f1@x>", 9))
    repo.upsert_event(_clean_fox("<f2@x>", 9))
    repo.upsert_event(_clean_fox("<f3@x>", 11))
    md = _report(repo, alias_map, fake_llm)

    day_chart = md.split("## Day-by-day")[1].split("## ")[0]   # scope to THIS chart
    assert "<svg" in day_chart and 'role="img"' in day_chart   # inline SVG chart present
    assert day_chart.count('fill="#1b5e3f"') == 2              # two non-zero days -> two GREEN bars
    assert "peaking at 2" in day_chart                         # peak matches the busiest day (2)
    assert "not a trend line" in day_chart                     # discrete bars, no implied trend
    # Chart and table agree because both read the same by_day counts.
    assert "| 2026-07-09 | 2 |" in md


# --------------------------------------------------------------------------- #
# Change 2 — period-over-period comparison (deterministic; LLM never sees it).
# --------------------------------------------------------------------------- #
def test_comparison_no_baseline_when_prior_predates_earliest_row(repo, alias_map, fake_llm):
    # THE explicit case: the baseline period is entirely before the earliest
    # ingested row. Must render the 'no comparable prior period' statement — not a
    # blank section, and never a divide-by-zero.
    repo.upsert_event(_clean_fox("<f1@x>", 9))          # earliest fox row = 2026-07-09
    md = _report(repo, alias_map, fake_llm)             # window 2026-07-08..14; prior = 07-01..07

    assert "## Period-over-period comparison" in md                     # section present, not dropped
    assert "No comparable prior period exists in the data" in md
    assert "entirely before the earliest dated red fox record held (2026-07-09)" in md
    assert "so no change can be computed" in md
    assert "%" not in md.split("## Notable")[0].split("comparison")[1]  # no bogus percent, no div-by-zero


def test_comparison_computes_change_over_a_real_baseline(repo, alias_map, fake_llm):
    # 2 events in the prior period (07-01..07), 5 in this window (07-08..14).
    repo.upsert_event(_clean_fox("<p1@x>", 2))
    repo.upsert_event(_clean_fox("<p2@x>", 5))
    for i, day in enumerate((9, 9, 9, 11, 11)):
        repo.upsert_event(_clean_fox(f"<c{i}@x>", day))
    md = _report(repo, alias_map, fake_llm)

    assert "rose from 2 to 5" in md
    assert "+3, +150%" in md                            # deterministic delta + percent
    assert "1–7 Jul 2026" in md and "8–14 Jul 2026" in md   # bars labelled by real date range
    # The comparison section carries its own two-bar chart.
    assert "<svg" in md.split("## Period-over-period comparison")[1].split("## ")[0]
    # Reconciled: the comparison's current figure IS the table's dated total (5).
    assert "| **Total (dated)** | **5** |" in md
    assert "day-by-day table's dated total" in md


def test_comparison_falls_and_reconciles(repo, alias_map, fake_llm):
    for i in range(4):                                  # 4 in the prior period
        repo.upsert_event(_clean_fox(f"<p{i}@x>", 3))
    repo.upsert_event(_clean_fox("<c1@x>", 10))         # 1 in this window
    md = _report(repo, alias_map, fake_llm)
    assert "fell from 4 to 1" in md
    assert "-3, -75%" in md


def test_fourth_standing_statement_fires_with_and_without_baseline(repo, alias_map, fake_llm):
    from ttr.agents.report import _COMPARISON_CLAIM

    # No baseline: the fourth statement STILL fires (absence is information).
    repo.upsert_event(_clean_fox("<f1@x>", 9))
    md_no = _report(repo, alias_map, fake_llm)
    assert _COMPARISON_CLAIM in md_no
    assert "no prior equal-length period with data" in md_no

    # With a baseline: same verbatim claim, different lead-in.
    repo.upsert_event(_clean_fox("<p1@x>", 3))
    md_yes = _report(repo, alias_map, fake_llm)
    assert _COMPARISON_CLAIM in md_yes                  # byte-identical claim body (audit value)
    assert "1 prior equal-length period" in md_yes


def test_llm_never_receives_the_comparison_facts(repo, alias_map, fake_llm):
    # Strong enforcement: the model is not given the comparison, so it structurally
    # cannot narrate a cause. The deterministic sentence is the shown claim.
    repo.upsert_event(_clean_fox("<p1@x>", 3))          # prior baseline
    repo.upsert_event(_clean_fox("<c1@x>", 9))          # this window
    md = _report(repo, alias_map, fake_llm)

    assert "rose from 1 to 1" in md or "unchanged" in md  # comparison rendered deterministically
    assert fake_llm.calls                                 # the LLM did run (for the within-window summary)
    prompt = fake_llm.calls[0]["prompt"].lower()
    assert "previous equal-length period" not in prompt   # comparison facts withheld from the model
    assert "rose from" not in prompt and "fell from" not in prompt


def test_disagreement_is_flagged_by_binomial(repo, alias_map, fake_llm):
    repo.upsert_event(make_persisted_event(
        "<d@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 2, 0, tzinfo=UTC),
        bioclip_ok=True, vlm_ok=True, agreement_flag="disagree",
        taxonomic_distance="same_order",
        agreement_rationale="upstream 'VulpesVulpes' -> Vulpes vulpes, but BioCLIP -> Meles meles"))
    md = _report(repo, alias_map, fake_llm)

    assert "did not corroborate" in md
    assert "Meles meles" in md
    # The distance is what replaced the old 'indeterminate' bucket: a disagreement
    # now says how far off BioCLIP was, not merely that it differed.
    assert "same order, different family" in md


def test_disagreement_distance_breakdown_separates_near_miss_from_scene_content(
        repo, alias_map, fake_llm):
    """The presentation fix, asserted directly: a near miss inside the same genus
    and a read of the vegetation are both 'disagree', and the report must not let
    them collapse into one undifferentiated figure."""
    repo.upsert_event(make_persisted_event(
        "<near@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 2, 0, tzinfo=UTC),
        bioclip_ok=True, vlm_ok=True, agreement_flag="disagree",
        taxonomic_distance="same_genus",
        agreement_rationale="BioCLIP top-1 'Vulpes lagopus' is a different taxon (same_genus)"))
    repo.upsert_event(make_persisted_event(
        "<far@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 3, 0, tzinfo=UTC),
        bioclip_ok=True, vlm_ok=True, agreement_flag="disagree",
        taxonomic_distance="non_animal",
        agreement_rationale="BioCLIP top-1 'Poa pratensis' is a different taxon (non_animal)"))
    md = _report(repo, alias_map, fake_llm)

    assert "same genus, different species (1, 50% of disagreements)" in md
    # Names BioCLIP in the label itself: read on its own in a table, "not an
    # animal" says no animal was present, which is the opposite of the meaning.
    assert ("BioCLIP read a plant, fungus or other non-animal "
            "(1, 50% of disagreements)") in md
    assert "not an animal (1," not in md
    assert "Vulpes lagopus" in md and "Poa pratensis" in md
    assert "indeterminate" not in md.lower()


def test_not_evaluable_is_reported_as_excluded_not_as_a_disagreement(
        repo, alias_map, fake_llm):
    """Decision 2 under the two-state model: a class BioCLIP cannot represent is
    excluded from the denominator, never scored as a disagreement."""
    repo.upsert_event(make_persisted_event(
        "<p@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 2, 0, tzinfo=UTC),
        bioclip_ok=True, vlm_ok=True, agreement_flag="not_evaluable",
        taxonomic_distance="undetermined",
        agreement_rationale="class 'Person' is outside BioCLIP's taxonomy"))
    md = _report(repo, alias_map, fake_llm)

    assert "not comparable (1)" in md
    assert "NOT counted as a disagreement" in md
    assert "outside BioCLIP" in md


def test_crosscheck_can_be_suppressed_for_presentation(repo, alias_map, fake_llm):
    repo.upsert_event(make_persisted_event(
        "<d@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 2, 0, tzinfo=UTC),
        bioclip_ok=True, vlm_ok=True, agreement_flag="disagree",
        agreement_rationale="upstream -> Vulpes vulpes, but BioCLIP -> Meles meles"))

    shown = _report(repo, alias_map, fake_llm, include_crosscheck=True)
    assert "did not corroborate" in shown

    hidden = _report(repo, alias_map, fake_llm, include_crosscheck=False)
    # Suppressed from the rendering...
    assert "did not corroborate" not in hidden
    assert "BioCLIP -> Meles meles" not in hidden
    # ...but honestly disclosed as hidden, not falsely "none flagged".
    assert "cross-check display is off" in hidden.lower()
    assert "still stored" in hidden.lower()
    assert "None flagged" not in hidden


def test_low_confidence_and_failed_enrichment_flagged(repo, alias_map, fake_llm):
    # bioclip/vlm ran but errored => "failed" (a model tried and returned an error).
    repo.upsert_event(make_persisted_event(
        "<lc@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 2, 0, tzinfo=UTC),
        upstream_confidence=0.20,
        bioclip_ok=False, bioclip_error="cuda oom",
        vlm_ok=False, vlm_error="read timeout", agreement_flag="agree"))
    md = _report(repo, alias_map, fake_llm)

    assert "Low upstream confidence" in md
    assert "Enrichment failed" in md
    assert "BioCLIP on 1" in md and "VLM on 1" in md
    assert "Enrichment not run" not in md


def test_never_run_enrichment_is_worded_differently_from_failed(repo, alias_map, fake_llm):
    # bioclip/vlm not ok but NO error => never attempted (e.g. via `store`).
    repo.upsert_event(make_persisted_event(
        "<nr@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 2, 0, tzinfo=UTC),
        bioclip_ok=False, vlm_ok=False, agreement_flag="agree"))
    md = _report(repo, alias_map, fake_llm)

    assert "Enrichment not run" in md
    assert "not a model failure" in md
    assert "Enrichment failed" not in md


def test_undated_detections_are_disclosed_not_dropped(repo, alias_map, fake_llm):
    repo.upsert_event(_clean_fox("<f1@x>", 9))                    # dated
    repo.upsert_event(make_persisted_event(                       # undated fox
        "<u@x>", canonical_binomial="Vulpes vulpes", event_time=None,
        bioclip_ok=True, vlm_ok=True, agreement_flag="agree"))
    md = _report(repo, alias_map, fake_llm)

    # Reconciled: total = dated + undated (2 = 1 + 1), no self-contradiction.
    assert "**Alert events:** 2" in md
    assert "1 placed on days below, 1 without a usable timestamp" in md
    assert "1 of the 2" in md                                     # undated framed against total
    assert "| **Total (dated)** | **1** |" in md                 # table sums to the dated count


def test_figures_reconcile_dated_plus_undated_equals_total(repo, alias_map, fake_llm):
    # 2 dated + 1 undated: total 3, table sums to 2, undated discloses 1 of 3.
    repo.upsert_event(_clean_fox("<d1@x>", 9))
    repo.upsert_event(_clean_fox("<d2@x>", 11))
    repo.upsert_event(make_persisted_event(
        "<u1@x>", canonical_binomial="Vulpes vulpes", event_time=None,
        bioclip_ok=True, vlm_ok=True, agreement_flag="agree"))
    md = _report(repo, alias_map, fake_llm)

    header = int(re.search(r"\*\*Alert events:\*\* (\d+)", md).group(1))
    table_total = int(re.search(r"\*\*Total \(dated\)\*\* \| \*\*(\d+)\*\*", md).group(1))
    undated = int(re.search(r"(\d+) of the \d+", md).group(1))
    assert (header, table_total, undated) == (3, 2, 1)
    assert table_total + undated == header                        # the arithmetic adds up


def test_inconsistent_figures_withhold_narrative_from_llm(alias_map, fake_llm):
    # A retrieval whose dated record falls OUTSIDE the window's days => the table
    # cannot account for it (shown != dated). The agent must NOT hand incoherent
    # facts to the LLM (the seam the live run exposed).
    class BadRetrieval:
        def resolve_identity(self, s):
            return "Vulpes vulpes", "red fox"

        def find(self, s, w):
            return [make_persisted_event("<x@x>", canonical_binomial="Vulpes vulpes",
                                         event_time=datetime(2020, 1, 1, tzinfo=UTC))]

        def count_undated(self, k):
            return 0

        def find_unmapped(self, w):
            return []

        def find_others(self, k, w):
            return []

        def delivery_timeline(self, w, key=None):
            return []

        def data_span(self):
            return None, None

        def send_time_span(self):
            return None, None

    agent = ReportGeneratorAgent(BadRetrieval(), fake_llm)
    md = agent.generate("fox", _window())
    assert "did not reconcile" in md
    assert fake_llm.calls == []                                   # LLM never saw the bad facts


def test_label_less_detection_disclosed_as_unmapped(repo, alias_map, fake_llm):
    repo.upsert_event(_clean_fox("<f1@x>", 9))
    repo.upsert_event(make_persisted_event(
        "<nl@x>", canonical_binomial="unmapped:(no upstream label)",
        canonical_is_binomial=False, upstream_label=None,
        event_time=datetime(2026, 7, 10, 2, 0, tzinfo=UTC)))
    md = _report(repo, alias_map, fake_llm, species="fox")

    assert "Unmapped detections" in md
    assert "no upstream label" in md                              # named, not silently absent


def test_uses_llm_for_narrative_but_survives_llm_failure(repo, alias_map, fake_llm):
    repo.upsert_event(_clean_fox("<f1@x>", 9))

    ok = _report(repo, alias_map, fake_llm)
    assert fake_llm.calls                                 # the LLM was used
    assert "deterministic narrative" in ok
    # System prompt guards against following instructions in the data.
    assert "never an instruction to follow" in fake_llm.calls[0]["system"]

    # With a failing LLM the report is still produced WITH the honesty language.
    degraded = _report(repo, alias_map, FailingLLM())
    assert "Narrative summary unavailable" in degraded
    assert "alert events" in degraded.lower()
    assert "real captured alert email" in degraded.lower()


# --------------------------------------------------------------------------- #
# Narrative vocabulary gate — ENFORCEMENT, not just a prompt instruction.
# --------------------------------------------------------------------------- #
def test_narrative_violations_detects_animal_counting_vocabulary():
    from ttr.agents.report import narrative_violations

    # The exact sentence llava produced, which contradicted the Decision 4 claim
    # printed a few sections below it.
    assert "sightings" in narrative_violations(
        "A total of 458 common wood pigeon sightings were recorded.")
    for term, text in [("animals", "458 animals were counted."),
                       ("individuals", "We saw 458 individuals."),
                       ("visits", "There were 458 visits to the feeder."),
                       ("birds", "458 birds were detected.")]:
        assert term in narrative_violations(text)
    # Compliant text passes, and 'bird feeder' (singular, adjectival) is not a hit.
    assert narrative_violations("There were 458 alert events, peaking on 29 June.") == []
    assert narrative_violations("The bird feeder produced 458 alert events.") == []


def test_narrative_violations_catches_blacklist_evasion():
    from ttr.agents.report import narrative_violations

    # Avoids every forbidden word but still counts creatures. The positive
    # requirement (must name alert events/detections) is what catches this.
    v = narrative_violations("There were 458 common wood pigeons across the window.")
    assert v and "never names" in v[0]


def test_noncompliant_narrative_is_regenerated_not_published(repo, alias_map):
    from conftest import ScriptedLLM

    repo.upsert_event(_clean_fox("<f1@x>", 9))
    llm = ScriptedLLM("A total of 3 fox sightings were recorded.",      # rejected
                      "There were 3 alert events on 9 July.")            # accepted
    md = _report(repo, alias_map, llm)

    assert len(llm.calls) == 2                       # it regenerated
    assert "3 alert events on 9 July" in md          # the compliant text is published
    assert "sightings" not in md                     # the rejected text never appears
    assert "REJECTED for using: sightings" in llm.calls[1]["system"]   # told what to fix


def test_persistently_noncompliant_narrative_is_withheld_with_figures_intact(repo, alias_map):
    from conftest import ScriptedLLM

    repo.upsert_event(_clean_fox("<f1@x>", 9))
    llm = ScriptedLLM("458 fox sightings were recorded.")   # never complies
    md = _report(repo, alias_map, llm)

    assert len(llm.calls) == 3                       # bounded retries, then stop
    assert "Narrative summary withheld" in md
    assert "sightings" in md                         # the reason is named, not hidden
    # Withholding the prose costs nothing factual: figures and honesty language stand.
    assert "| 2026-07-09 | 1 |" in md
    assert "alert-event count, not an animal" in md
    assert "real captured alert email" in md.lower()


# --------------------------------------------------------------------------- #
# Notable observations — agent-COMPUTED highlights, no model in the claim path.
# --------------------------------------------------------------------------- #
def test_busiest_day_highlight_matches_the_day_by_day_table(repo, alias_map, fake_llm):
    for mid, day in [("<a@x>", 9), ("<b@x>", 9), ("<c@x>", 9), ("<d@x>", 11)]:
        repo.upsert_event(_clean_fox(mid, day))
    md = _report(repo, alias_map, fake_llm)

    # The highlight and the table are the same arithmetic over the same counts.
    assert "| 2026-07-09 | 3 |" in md
    assert "**Busiest day:** 9 Jul 2026 — 3 alert events." in md
    assert "**Quietest active day:** 11 Jul 2026 — 1 alert event." in md
    # Events, never animals — same discipline as everywhere else.
    assert "3 animals" not in md


def test_non_target_species_highlight_matches_the_other_rows(repo, alias_map, fake_llm):
    repo.upsert_event(_clean_fox("<f@x>", 9))
    for mid, day in [("<b1@x>", 10), ("<b2@x>", 12)]:
        repo.upsert_event(make_persisted_event(
            mid, canonical_binomial="Meles meles",
            event_time=datetime(2026, 7, day, 2, 0, tzinfo=UTC),
            display_common_name="european badger", bioclip_ok=True, vlm_ok=True))
    repo.upsert_event(make_persisted_event(
        "<c1@x>", canonical_binomial="Bos taurus",
        event_time=datetime(2026, 7, 13, 2, 0, tzinfo=UTC),
        display_common_name="cattle", bioclip_ok=True, vlm_ok=True))
    md = _report(repo, alias_map, fake_llm, species="fox")

    assert "**Non-target activity:** 3 alert events from 2 other species" in md
    assert "**european badger** (*Meles meles*) — 2 alert events on 10, 12 Jul 2026" in md
    assert "**cattle** (*Bos taurus*) — 1 alert event on 13 Jul 2026" in md
    # The target species is not listed as its own non-target activity.
    assert "Vulpes vulpes*) — " not in md


def test_zero_days_beyond_the_data_are_not_called_quiet_days(repo, alias_map, fake_llm):
    # A window wider than the dataset must not inflate the zero-day count with
    # days the data never covered — that is absence of DATA, not of animals.
    repo.upsert_event(_clean_fox("<f@x>", 9))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate("fox", TimeWindow.from_dates(date(2026, 7, 6), date(2026, 7, 12)))

    assert "they do not all mean the same thing" in md
    assert "outside the data's coverage" in md
    assert "days with no DATA, not days with no detections" in md
    assert "2026-07-09 to 2026-07-09" in md            # the span actually held


def test_zero_day_inside_a_delivery_silence_is_linked_to_the_gap_block(repo, alias_map, fake_llm):
    """A zero-day that an outage explains must not read as a quiet camera — the
    same screen would otherwise imply both 'no animals' and 'outage' for it."""
    repo.upsert_event(_clean_fox("<pre@x>", 8))                       # delivered 8 Jul
    for i in range(3):                                                # flush on 11 Jul
        repo.upsert_event(make_persisted_event(
            f"<flush{i}@x>", canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, 11, 12, 0, i, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, 11, 9, i, tzinfo=UTC),
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate("fox", TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 11)))

    # 9 and 10 July are empty, but a proven silence spans them.
    assert "lost to a delivery silence" in md
    assert "listed under 'System downtime' below" in md
    assert "says nothing about what was in front of the camera" in md


def test_zero_day_with_nothing_delivered_at_all_is_undetermined(repo, alias_map, fake_llm):
    """An empty day with no proven outage is NOT automatically a confirmed absence.

    If nothing of any species arrived either, a real absence and undetected
    downtime are indistinguishable — claiming the system was 'demonstrably
    delivering' would assert uptime that was never observed.
    """
    for mid, day in [("<a@x>", 8), ("<b@x>", 9), ("<c@x>", 11)]:
        repo.upsert_event(make_persisted_event(
            mid, canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, day, 12, 0, 33, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, day, 12, 0, tzinfo=UTC),
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate("fox", TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 11)))

    assert "1 undetermined" in md
    assert "nothing of ANY species arrived either" in md
    assert "with the system confirmed delivering" not in md   # no unearned uptime claim


def test_zero_day_is_confirmed_absent_when_other_species_arrived_that_day(repo, alias_map, fake_llm):
    # Same shape, but another species IS delivered on the empty day — now the
    # system is confirmed up and the absence is a real observation.
    for mid, day in [("<a@x>", 8), ("<c@x>", 11)]:
        repo.upsert_event(make_persisted_event(
            mid, canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, day, 12, 0, 33, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, day, 12, 0, tzinfo=UTC),
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    for i in range(6):                                   # badgers all through 9-10 Jul
        ts = datetime(2026, 7, 9, 6, tzinfo=UTC) + timedelta(hours=6 * i)
        repo.upsert_event(make_persisted_event(
            f"<b{i}@x>", canonical_binomial="Meles meles",
            event_time=ts + timedelta(seconds=33), capture_time_utc=ts,
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="european badger", bioclip_ok=True, vlm_ok=True))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate("fox", TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 11)))

    assert "with the system confirmed delivering" in md
    assert "only these support 'none seen that day'" in md
    assert "undetermined" not in md


def test_zero_day_and_coverage_block_share_one_classification(repo, alias_map, fake_llm):
    """The two sections must draw the SAME distinctions from the SAME computation,
    so a period cannot be 'confirmed absent' in one and 'no data' in the other."""
    from ttr.agents.report import ReportGeneratorAgent as R

    gaps = [(datetime(2026, 7, 9, 0, tzinfo=UTC), datetime(2026, 7, 10, 0, tzinfo=UTC), 86400, 5)]
    day_a = datetime(2026, 7, 9, 0, tzinfo=UTC)
    day_b = datetime(2026, 7, 9, 23, 59, 59, tzinfo=UTC)
    # Wholly inside an outage, nothing else delivered => no_data, both sides.
    assert R._classify_interval(day_a, day_b, gaps, [])[0] == "no_data"
    # Outside any outage, other species arriving => absent.
    free_a, free_b = datetime(2026, 7, 12, tzinfo=UTC), datetime(2026, 7, 13, tzinfo=UTC)
    assert R._classify_interval(free_a, free_b, gaps,
                                [datetime(2026, 7, 12, 9, tzinfo=UTC)])[0] == "absent"
    # Outside any outage, nothing delivered => undetermined, never "absent".
    assert R._classify_interval(free_a, free_b, gaps, [])[0] == "unconfirmed"
    # Straddling: material time on both sides => mixed, not rounded to either.
    kind, down_h, up_h, _n = R._classify_interval(
        datetime(2026, 7, 8, tzinfo=UTC), datetime(2026, 7, 12, tzinfo=UTC), gaps,
        [datetime(2026, 7, 11, 9, tzinfo=UTC)])
    assert kind == "mixed" and round(down_h) == 24 and round(up_h) == 72


def test_no_non_target_activity_renders_an_explicit_negative(repo, alias_map, fake_llm):
    # Uncertainty-as-absence guard: a computed highlight must not vanish when its
    # answer is zero — it must say zero.
    repo.upsert_event(_clean_fox("<f@x>", 9))
    md = _report(repo, alias_map, fake_llm)

    assert "**Non-target activity:** none" in md
    assert "no detections of any other mapped species fell in this window" in md


def test_outage_is_reported_as_a_data_gap_not_an_activity_gap(repo, alias_map, fake_llm):
    # Captures continue through a delivery silence and arrive afterwards — the
    # signature that proves an outage rather than a quiet period.
    repo.upsert_event(_clean_fox("<pre@x>", 9))                       # sent 9 Jul 02:00
    for i in range(4):
        repo.upsert_event(make_persisted_event(
            f"<flush{i}@x>", canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, 11, 12, 0, i, tzinfo=UTC),   # all delivered later
            capture_time_utc=datetime(2026, 7, 10, 8 + i, 0, tzinfo=UTC),  # captured mid-silence
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    md = _report(repo, alias_map, fake_llm)

    assert "**System downtime — no data, not an absence of animals:**" in md
    assert "no alert was delivered for ANY species while the camera kept capturing" in md
    assert "unknown and unknowable" in md          # never claims the gap was empty
    assert "| Silence (UTC) | Duration | red fox events recovered afterwards |" in md
    # Only the genuinely back-dated events count as evidence, not the alert that
    # reopened the channel (whose own capture also falls inside the silence).
    assert "| 3 |" in md


def test_species_absent_while_system_confirmed_up_is_not_called_no_data(repo, alias_map, fake_llm):
    """THE discriminating case, which the real dataset cannot produce: a species
    goes quiet for days while OTHER species keep arriving.

    The system is demonstrably working, so this is a real absence of the species —
    it must NOT be labelled 'no data (system down)', which would turn a genuine
    ecological observation into a shrug about instrumentation.
    """
    # Target species: present on 8 Jul, then nothing for the rest of the window.
    repo.upsert_event(make_persisted_event(
        "<fox@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 8, 9, 0, 33, tzinfo=UTC),
        capture_time_utc=datetime(2026, 7, 8, 9, 0, tzinfo=UTC),
        capture_time_source="filename_block_d", capture_time_tz_validated=True,
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    # Another species keeps being delivered every few hours throughout — proof the
    # camera and the mailbox were both alive the whole time.
    for i in range(30):
        ts = datetime(2026, 7, 8, 12, tzinfo=UTC) + timedelta(hours=2 * i)
        repo.upsert_event(make_persisted_event(
            f"<badger{i}@x>", canonical_binomial="Meles meles",
            event_time=ts + timedelta(seconds=33), capture_time_utc=ts,
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="european badger", bioclip_ok=True, vlm_ok=True))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate("fox", TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 10)))

    assert "red fox absent — system confirmed operational" in md
    assert "these are real absences, not missing data" in md
    assert "the system was demonstrably working" in md
    # It must NOT be excused as downtime, and no outage should be claimed at all.
    assert "presence unverifiable" not in md
    assert "**System downtime:** none detected" in md


def test_system_downtime_recovery_counts_are_species_scoped(repo, alias_map, fake_llm):
    """A dataset-wide recovery figure beside a filtered report is a misattribution:
    the outage delayed OTHER species' events, none of the reported one's."""
    repo.upsert_event(make_persisted_event(          # target: before the silence only
        "<fox@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 8, 9, 0, 33, tzinfo=UTC),
        capture_time_utc=datetime(2026, 7, 8, 9, 0, tzinfo=UTC),
        capture_time_source="filename_block_d", capture_time_tz_validated=True,
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    for i in range(5):                               # a NON-target backlog flush
        repo.upsert_event(make_persisted_event(
            f"<badger{i}@x>", canonical_binomial="Meles meles",
            event_time=datetime(2026, 7, 10, 12, 0, i, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, 9, 8 + i, 0, tzinfo=UTC),
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="european badger", bioclip_ok=True, vlm_ok=True))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate("fox", TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 10)))

    # The silence is real and shown, but scoped: zero RED FOX events were recovered.
    assert "| Silence (UTC) | Duration | red fox events recovered afterwards |" in md
    assert "| 0 |" in md
    assert "No red fox events at all were recovered from these silences" in md
    assert "none of the delayed traffic was red fox" in md
    assert "never that any were present" in md
    assert "| 4 |" not in md and "| 5 |" not in md   # the all-species figure never leaks


def test_quiet_night_is_not_reported_as_an_outage(repo, alias_map, fake_llm):
    """A long delivery silence with NO back-dated captures is not evidence of an
    outage — otherwise every night in a garden would be reported as one.

    This is the false positive that a naive 'captures inside the silence' test
    produces: under normal operation an alert is sent ~33s after its capture, so
    EVERY event's own capture falls inside the silence preceding its own send.
    Only events delivered later than the alert that reopened the channel count.
    """
    # Two events a day apart, each delivered promptly after its own capture.
    for mid, day in [("<n1@x>", 9), ("<n2@x>", 11)]:
        repo.upsert_event(make_persisted_event(
            mid, canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, day, 8, 0, 33, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, day, 8, 0, 0, tzinfo=UTC),   # 33s earlier
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    md = _report(repo, alias_map, fake_llm)

    assert "**System downtime:** none detected" in md
    assert "distinguishes an outage from a genuinely quiet period" in md
    assert "Silence (UTC)" not in md              # no gap table at all


def test_vlm_descriptions_are_demoted_below_the_computed_highlights(repo, alias_map, fake_llm):
    repo.upsert_event(make_persisted_event(
        "<v@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 2, 0, tzinfo=UTC),
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True,
        vlm_description="A fox and a cat near the feeder at 12:47pm."))
    md = _report(repo, alias_map, fake_llm)

    # Kept as evidence, clearly labelled as asserting nothing...
    assert "## Unverified model descriptions (not observations)" in md
    assert "asserts nothing" in md
    assert "fabricates on-image text" in md            # the existing warning survives
    # ...and positioned BELOW the computed highlights.
    assert md.index("## Notable observations") < md.index("## Unverified model descriptions")


def test_highlights_disclose_excluded_undated_and_unmapped_rows(repo, alias_map, fake_llm):
    repo.upsert_event(_clean_fox("<f@x>", 9))
    repo.upsert_event(make_persisted_event(                      # undated target row
        "<u@x>", canonical_binomial="Vulpes vulpes", event_time=None,
        bioclip_ok=True, vlm_ok=True))
    repo.upsert_event(make_persisted_event(                      # unmapped row in window
        "<um@x>", canonical_binomial="unmapped:GallusGallus", canonical_is_binomial=False,
        upstream_label="GallusGallus", event_time=datetime(2026, 7, 10, 2, 0, tzinfo=UTC)))
    md = _report(repo, alias_map, fake_llm)

    assert "_Scope:" in md
    assert "could not be placed on a day" in md
    assert "NOT counted in the non-target list" in md


# --------------------------------------------------------------------------- #
# All-species summary (generate_all_species) — sibling of the single-species path.
# --------------------------------------------------------------------------- #
def _all_species_window():
    return TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 12))


def _seed_multi_species(repo):
    """Fox, badger, cattle, plus a nonbio and an unmapped row — all dated in-window."""
    for mid, day in [("<f1@x>", 9), ("<f2@x>", 9), ("<f3@x>", 11)]:
        repo.upsert_event(_clean_fox(mid, day))
    for mid, day in [("<b1@x>", 9), ("<b2@x>", 10)]:
        repo.upsert_event(make_persisted_event(
            mid, canonical_binomial="Meles meles",
            event_time=datetime(2026, 7, day, 3, 0, tzinfo=UTC),
            display_common_name="european badger", bioclip_ok=True, vlm_ok=True))
    repo.upsert_event(make_persisted_event(
        "<c1@x>", canonical_binomial="Bos taurus",
        event_time=datetime(2026, 7, 12, 3, 0, tzinfo=UTC),
        display_common_name="cattle", bioclip_ok=True, vlm_ok=True))
    repo.upsert_event(make_persisted_event(          # non-biological class
        "<p1@x>", canonical_binomial="nonbio:Person", canonical_is_binomial=False,
        event_time=datetime(2026, 7, 10, 3, 0, tzinfo=UTC)))
    repo.upsert_event(make_persisted_event(          # unmapped token
        "<u1@x>", canonical_binomial="unmapped:GallusGallus", canonical_is_binomial=False,
        upstream_label="GallusGallus", event_time=datetime(2026, 7, 10, 4, 0, tzinfo=UTC)))


def test_what_this_supports_line_present_and_derived(repo, alias_map, fake_llm):
    _seed_multi_species(repo)                             # fox dominant (3), 4 others
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    allm = agent.generate_all_species(_all_species_window())
    fox = agent.generate("fox", _all_species_window())

    assert "**What this supports:**" in allm and "**What this supports:**" in fox
    # Names the dominant species and its active-day count; scales honestly (small
    # fixture -> not "strong ... sustained").
    supp = [l for l in allm.splitlines() if "What this supports" in l][0]
    assert "red fox" in supp
    assert "days with data" in supp
    assert "speciess" not in supp                         # no broken pluralisation
    assert "4 other species also detected (unconfirmed)" in supp   # k derived
    # Single-species report has no "other species" clause (none in its own set).
    supp_fox = [l for l in fox.splitlines() if "What this supports" in l][0]
    assert "other species" not in supp_fox


def test_outage_reach_is_quantified_and_matches_a_hand_check(repo, alias_map, fake_llm):
    # Prompt daily events on 8, 9, 10 Jul, then a proven outage: two captures on
    # 12 Jul delivered late (the second after the silence ends, so it's 'proven').
    # The gap runs 10 Jul 12:00 -> 12 Jul 10:00, touching local dates 10, 11, 12.
    for d in (8, 9, 10):
        repo.upsert_event(_both_clocks_fox(f"<d{d}@x>", d))
    for i in range(2):                                    # captured 12 Jul, flushed 12 Jul 10:00
        repo.upsert_event(make_persisted_event(
            f"<f{i}@x>", canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, 12, 10, 0, i, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, 12, 6 + i, 0, tzinfo=UTC),
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    md = _report(repo, alias_map, fake_llm,
                 window=TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 13)))

    # Hand check: active days = {8,9,10,12} = 4; outage touches {10,11,12}; so 2 of
    # 4 active days (10 and 12) are touched, carrying 1+2 = 3 of 5 events (60%).
    assert "Trend caution" in md
    assert "2 of 4 active days are touched by a proven outage" in md
    assert "those days carry 3 of 5 events (60%)" in md


def test_time_of_day_section_on_both_reports_counts_sum_to_N(repo, alias_map, fake_llm):
    # Two events at distinct local hours (08:00 and 20:00 UTC = 09:00 and 21:00 BST).
    for mid, hh in [("<a@x>", 8), ("<b@x>", 20)]:
        repo.upsert_event(make_persisted_event(
            mid, canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, 9, hh, 0, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, 9, hh, 0, tzinfo=UTC),
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    for md in (agent.generate("fox", _all_species_window()),
               agent.generate_all_species(_all_species_window())):
        assert "## Time-of-day distribution" in md                 # on both report types
        sec = md.split("## Time-of-day distribution")[1].split("## ")[0]
        assert "<svg" in sec                                       # the histogram
        buckets = [int(m) for m in re.findall(r"\| \d\d:00–\d\d:59 \| (\d+) \|", sec)]
        assert sum(buckets) == 2                                   # buckets sum to N
        assert "| **Total** | **2** |" in sec
        assert "diurnal species and daytime-only capture" in sec   # the honest read
        assert "not a behavioural rate" in sec                     # caveat


def test_non_target_review_lists_non_dominant_events_all_species_only(repo, alias_map, fake_llm):
    _seed_multi_species(repo)     # fox(3) dominant; badger(2), cattle(1), Person(1), unmapped(1)
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    allm = agent.generate_all_species(_all_species_window())
    fox = agent.generate("fox", _all_species_window())

    assert "## Non-target detections — for review" in allm
    assert "## Non-target detections" not in fox                   # all-species ONLY
    sec = allm.split("## Non-target detections — for review")[1].split("## ")[0]
    # The verify-before-acting line, non-hedging.
    assert "must be checked against the actual images" in sec
    assert "Do not act on the automated label alone" in sec
    # Lists per-event local timestamps for the non-dominant species; NOT the fox.
    assert "european badger" in sec and "cattle" in sec
    assert "(local)" in sec
    # One line per non-dominant event: badger 2 + cattle 1 + Person 1 + unmapped 1 = 5.
    assert sec.count("(local)") == 5
    assert "red fox" not in sec                                    # the dominant is excluded
    _seed_multi_species(repo)                             # 8 events across 5 keys
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    window = _all_species_window()
    allm = agent.generate_all_species(window)
    fox = agent.generate("fox", window)

    # Present in all-species, ABSENT from single-species.
    assert "## Species composition of alert events" in allm
    assert "## Species composition" not in fox

    sec = allm.split("## Species composition of alert events")[1].split("## Cross-check")[0]
    # Counts (data rows only; the Total row is bolded and won't match) sum to N=8.
    counts = [int(m) for m in re.findall(r"\| [^|]+ \| (\d+) \| [\d.]+% \|", sec)]
    assert sum(counts) == 8
    assert "| **Total (N)** | **8** | 100% |" in allm
    assert "<svg" in sec and ("<path" in sec or "<circle" in sec)   # the pie

    # NO diversity index and NO 'biodiversity' site claim.
    low = allm.lower()
    for banned in ("biodiversity", "shannon", "simpson", "diversity index",
                   "evenness", "species richness"):
        assert banned not in low, banned

    # All three required caveats.
    assert "not of animals or abundance" in allm                    # events, not animals
    assert "automated and mostly unconfirmed" in allm               # provisional IDs
    assert "PROVISIONAL" in allm
    assert "single location, feeder-facing" in allm                 # single-point measure


def test_species_composition_shares_are_percentages_of_N(repo, alias_map, fake_llm):
    _seed_multi_species(repo)
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate_all_species(_all_species_window())
    # 3 of 8 = 37.5%, 2 of 8 = 25.0%, 1 of 8 = 12.5% — recomputed against N, not hardcoded.
    assert "| red fox (*Vulpes vulpes*) | 3 | 37.5% |" in md
    assert "| european badger (*Meles meles*) | 2 | 25.0% |" in md


def test_downtime_dates_section_names_dates_and_frames_captured_not_absent(repo, alias_map, fake_llm):
    _seed_outage_flush(repo)                             # a proven outage 8→11 Jul
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    window = TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 12))

    for md in (agent.generate("fox", window), agent.generate_all_species(window)):
        assert "## System downtime — dates affected" in md          # on BOTH report types
        sec = md.split("## System downtime — dates affected")[1]
        # The captured-not-absent framing, verbatim.
        assert "kept capturing" in sec
        assert "absence of delivery, not of data or animals" in sec
        assert "delivered late" in sec
        assert "not missing and not zero" in sec
        # Names the affected LOCAL dates the outage touches.
        assert "8, 9, 10, 11 Jul 2026" in sec


def test_all_species_total_equals_sum_of_per_species_totals(repo, alias_map, fake_llm):
    _seed_multi_species(repo)
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    window = _all_species_window()

    md = agent.generate_all_species(window)
    # 3 fox + 2 badger + 1 cattle + 1 nonbio + 1 unmapped = 8, all counted.
    assert "**Alert events:** 8" in md
    assert "| **Total (dated)** | **8** |" in md
    assert "| **Total** | **8** | |" in md          # species breakdown total

    # The all-species total equals the sum of the mapped per-species queries PLUS
    # the reserved keys the single-species path would exclude.
    retr = RetrievalAgent(repo, alias_map)
    per = sum(len(retr.find(sp, window)) for sp in ("fox", "badger", "cattle"))
    assert per == 6                                  # mapped species only
    assert len(retr.find_all(window)) == 8          # + nonbio + unmapped


def test_all_species_breakdown_accounts_for_every_event(repo, alias_map, fake_llm):
    _seed_multi_species(repo)
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate_all_species(_all_species_window())

    # Every key present, reserved ones included and labelled as what they are.
    assert "red fox (*Vulpes vulpes*) | 3 | 2 |" in md
    assert "european badger (*Meles meles*) | 2 | 2 |" in md
    assert "cattle (*Bos taurus*) | 1 | 1 |" in md
    assert "Person (non-biological class) | 1 | 1 |" in md
    assert "GallusGallus (unmapped token) | 1 | 1 |" in md
    # The deliberate cross-view difference is stated unconditionally.
    assert "counts **every** key" in md
    assert "single-species report **excludes** them" in md
    assert "This window contains 2 such events" in md   # nonbio + unmapped


def test_all_species_breakdown_note_present_even_with_no_reserved_keys(repo, alias_map, fake_llm):
    # Reconciliation methodology must be stated even when it doesn't bite, so a
    # reader comparing views isn't left guessing.
    repo.upsert_event(_clean_fox("<f1@x>", 9))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate_all_species(_all_species_window())
    assert "counts **every** key" in md
    assert "This window contains none, so every row here is a mapped species" in md


def test_all_species_outage_is_downtime_not_species_absence(repo, alias_map, fake_llm):
    # A backlog flush across the window: captures during a silence, delivered after.
    repo.upsert_event(_clean_fox("<pre@x>", 8))
    for i in range(4):
        repo.upsert_event(make_persisted_event(
            f"<flush{i}@x>", canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, 11, 12, 0, i, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, 10, 8 + i, 0, tzinfo=UTC),
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate_all_species(_all_species_window())

    assert "## System downtime" in md
    assert "proven system downtime" in md
    assert "All-species events recovered afterwards" in md
    # The collapse is explicit, and the per-species distinction is NOT claimed.
    assert "either proven downtime" in md and "or undetermined" in md
    assert "absent while the system was confirmed operational' cannot apply" in md
    assert "system confirmed operational" not in md.replace(
        "'absent while the system was confirmed operational' cannot apply", "")


def test_all_species_crosscheck_summary_states_the_outcome(repo, alias_map, fake_llm):
    # 1 agree + 2 not-confirmed => "not confirmed on 2 of 3 (67%)".
    repo.upsert_event(_clean_fox("<ok@x>", 9))                       # agreement_flag="agree"
    repo.upsert_event(make_persisted_event(
        "<d@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 4, 0, tzinfo=UTC), agreement_flag="disagree",
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    repo.upsert_event(make_persisted_event(
        "<i@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 10, 4, 0, tzinfo=UTC), agreement_flag="not_evaluable",
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate_all_species(_all_species_window())

    assert "## Cross-check summary" in md
    assert "did not corroborate 2 of 3 detections (67%)" in md
    # One real disagreement and one that could not be compared at all — counted
    # apart, so the binary denominator stays honest.
    assert "1 disagree, 1 not comparable" in md
    assert "excluded from the agree/disagree denominator" in md
    assert "per-detection rationales are in the individual per-species reports" in md


def test_all_species_crosscheck_summary_is_not_silent_when_clean(repo, alias_map, fake_llm):
    # Uncertainty-as-absence guard: a clean cross-check still states it, so absence
    # of a flag is never read as absence of a check.
    repo.upsert_event(_clean_fox("<ok@x>", 9))                       # agree
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate_all_species(_all_species_window())
    assert "## Cross-check summary" in md
    assert "corroborated the upstream label on all 1 detection" in md


def test_all_species_crosscheck_summary_honours_the_toggle(repo, alias_map, fake_llm):
    repo.upsert_event(make_persisted_event(
        "<d@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 4, 0, tzinfo=UTC), agreement_flag="disagree",
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm,
                                 include_crosscheck=False)
    md = agent.generate_all_species(_all_species_window())
    # Hidden, but disclosed as hidden — never a silent vanish.
    assert "cross-check display is off" in md.lower()
    assert "still stored" in md
    assert "Agreement not confirmed" not in md


def test_all_species_carries_the_unconditional_honesty_statements(repo, alias_map, fake_llm):
    _seed_multi_species(repo)
    agent = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    md = agent.generate_all_species(_all_species_window())
    assert "alert-event count, not an animal" in md          # Decision 4
    assert "CAMERA CAPTURE TIME" in md                        # Decision 3, amended
    assert "real captured alert email" in md.lower()         # Decision 7
    assert "## Period-over-period comparison" in md           # Change 2, scoped


def _both_clocks_fox(mid, day):
    """A dated fox event carrying BOTH a capture and a send time (send 33s later)."""
    return make_persisted_event(
        mid, canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, day, 12, 0, 33, tzinfo=UTC),
        capture_time_utc=datetime(2026, 7, day, 12, 0, tzinfo=UTC),
        capture_time_source="filename_block_d", capture_time_tz_validated=True,
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True)


def test_honesty_send_time_figures_use_this_reports_own_N(repo, alias_map, fake_llm):
    # 3 fox + 2 badger. The single-species fox honesty section must quote 3 (its own
    # dated N), the all-species must quote 5 — never a hardcoded 475 or another
    # scope's total. This is the regression guard for the hardcoded-475 bug.
    for i in range(3):
        repo.upsert_event(_both_clocks_fox(f"<f{i}@x>", 9))
    for i in range(2):
        repo.upsert_event(make_persisted_event(
            f"<b{i}@x>", canonical_binomial="Meles meles",
            event_time=datetime(2026, 7, 10, 12, 0, 33, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="european badger", bioclip_ok=True, vlm_ok=True))
    agent = _agent_all = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm)
    window = TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 12))

    fox = agent.generate("fox", window)
    allsp = agent.generate_all_species(window)

    # The section recomputes against each report's own event set.
    assert "In this report's 3 dated events carrying both a capture and a send time" in fox
    assert "In this report's 5 dated events carrying both a capture and a send time" in allsp
    # The stale hardcoded figures never appear in either.
    for md in (fox, allsp):
        assert "475" not in md
        assert "24 of 475" not in md
        assert "% were delivered within 60s of capture" in md   # figure present, per-report


def test_honesty_send_time_percentages_recompute_against_denominator(repo, alias_map, fake_llm):
    # 4 fox all sent within 60s (33s) => 100.0% within; none mis-dated.
    for i in range(4):
        repo.upsert_event(_both_clocks_fox(f"<f{i}@x>", 9))
    md = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm).generate(
        "fox", TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 12)))
    assert "In this report's 4 dated events" in md
    assert "100.0% were delivered within 60s of capture" in md
    assert "0 (0.0%) fall on a different calendar day" in md


# --------------------------------------------------------------------------- #
# External-review fixes: trend/outage caveat, no-data tail, monitoring effort.
# --------------------------------------------------------------------------- #
def _seed_outage_flush(repo):
    """A backlog flush that _proven_gaps() recognises as an outage: captures during
    a silence, all delivered after it ended."""
    repo.upsert_event(_clean_fox("<pre@x>", 8))                       # delivered 8 Jul
    for i in range(4):
        repo.upsert_event(make_persisted_event(
            f"<flush{i}@x>", canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, 11, 12, 0, i, tzinfo=UTC),   # all delivered 11 Jul
            capture_time_utc=datetime(2026, 7, 10, 8 + i, 0, tzinfo=UTC),  # captured 10 Jul
            capture_time_source="filename_block_d", capture_time_tz_validated=True,
            display_common_name="red fox", bioclip_ok=True, vlm_ok=True))


def test_trend_language_is_caveated_and_suppressed_when_outage_in_window(repo, alias_map, fake_llm):
    _seed_outage_flush(repo)
    md = _report(repo, alias_map, fake_llm,
                 window=TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 12)))

    # Item 1: the outage and the trend claim are connected in the SAME section.
    assert "Trend caution" in md
    assert "distorts the day-by-day shape" in md
    assert "must not be read as animal behaviour" in md
    # The LLM is given the confound, NOT a direction to describe.
    facts = fake_llm.calls[0]["prompt"]
    assert "DO NOT describe any rise, decline, or trend" in facts
    assert "Overall trend across the window: declining" not in facts
    assert "Overall trend across the window: rising" not in facts
    assert "Do NOT describe any trend" in fake_llm.calls[0]["system"]


def test_no_trend_caveat_and_a_normal_trend_when_no_outage(repo, alias_map, fake_llm):
    for d in (8, 9, 10, 11):
        repo.upsert_event(_clean_fox(f"<x{d}@x>", d))
    md = _report(repo, alias_map, fake_llm,
                 window=TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 11)))
    assert "Trend caution" not in md
    assert "Overall trend across the window:" in fake_llm.calls[0]["prompt"]


def test_tail_beyond_coverage_shows_no_data_not_zero(repo, alias_map, fake_llm):
    # Events on 9 and 12; window runs to 14. 10-11 are in-coverage zeros, 13-14
    # fall after the last delivery, so they are unresolved status, not zero.
    repo.upsert_event(_clean_fox("<a@x>", 9))
    repo.upsert_event(_clean_fox("<b@x>", 12))
    md = _report(repo, alias_map, fake_llm,
                 window=TimeWindow.from_dates(date(2026, 7, 9), date(2026, 7, 14)))

    assert "| 2026-07-10 | 0 |" in md                     # in-coverage: a true zero
    assert "| 2026-07-13 | no data |" in md               # beyond last delivery
    assert "| 2026-07-14 | no data |" in md
    assert "| 2026-07-14 | 0 |" not in md                 # never a bare zero at the tail
    assert "monitoring status" in md and "not zero activity" in md
    # The chart marks those columns too (band + label), not empty bars.
    assert 'fill="#e6eae6"' in md and ">no data</text>" in md


def test_operational_coverage_uses_send_time_not_capture(repo, alias_map, fake_llm):
    # A backlog-flushed capture (10 Jul) delivered 12 Jul proves the system was up
    # on 12 Jul — so 12 Jul is a true zero-capture day, NOT 'no data', even though
    # the last CAPTURE was 10 Jul.
    repo.upsert_event(make_persisted_event(
        "<flush@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),         # delivered 12 Jul
        capture_time_utc=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),    # captured 10 Jul
        capture_time_source="filename_block_d", capture_time_tz_validated=True,
        display_common_name="red fox"))
    md = _report(repo, alias_map, fake_llm,
                 window=TimeWindow.from_dates(date(2026, 7, 10), date(2026, 7, 13)))
    assert "| 2026-07-10 | 1 |" in md                     # the capture day
    assert "| 2026-07-12 | 0 |" in md                     # delivery day: confirmed up, zero
    assert "| 2026-07-12 | no data |" not in md
    assert "| 2026-07-13 | no data |" in md               # after last delivery


def test_monitoring_effort_states_uptime_and_declines_a_rate(repo, alias_map, fake_llm):
    repo.upsert_event(_clean_fox("<a@x>", 9))
    repo.upsert_event(_clean_fox("<b@x>", 11))
    md = _report(repo, alias_map, fake_llm,
                 window=TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 12)))

    assert "## Monitoring effort" in md
    assert "confirmed operational for" in md and "%)" in md
    assert "raw over this effort" in md
    assert "events-per-hour rate would not be a sound figure" in md
    assert "not turned into a rate here" in md


def test_monitoring_effort_coverage_is_detection_span_not_calendar_span(repo, alias_map, fake_llm):
    # First detection 08 Jul 20:00, last 10 Jul 04:00 -> a 32h SPAN, but it touches
    # 3 calendar days (72h). Coverage must be the 32h span, named by its timestamps,
    # not the calendar range.
    repo.upsert_event(make_persisted_event(
        "<a@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 8, 20, 0, tzinfo=UTC),
        capture_time_utc=datetime(2026, 7, 8, 19, 59, 27, tzinfo=UTC),
        capture_time_source="filename_block_d", capture_time_tz_validated=True,
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    repo.upsert_event(make_persisted_event(
        "<b@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 10, 4, 0, tzinfo=UTC),
        capture_time_utc=datetime(2026, 7, 10, 3, 59, 27, tzinfo=UTC),
        capture_time_source="filename_block_d", capture_time_tz_validated=True,
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    md = ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm).generate(
        "fox", TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 10)))

    eff = md.split("## Monitoring effort")[1].split("##")[0]
    # Coverage is the 32h detection span, named by timestamps and labelled a span.
    assert "from the first to the last detection delivered" in eff
    assert "2026-07-08 20:00 → 2026-07-10 04:00 UTC" in eff
    assert "span of **32.0h**" in eff
    assert "not the calendar range those dates touch" in eff
    # The uptime denominator is the 32h span, NOT the 72h calendar range those days
    # touch (72h here is the window figure, a different number).
    assert "confirmed operational for 32.0 of 32.0 hours (100%)" in eff
    assert "of 72.0 hours" not in eff and "of 72 hours" not in eff


def test_monitoring_effort_reports_downtime_reducing_uptime(repo, alias_map, fake_llm):
    _seed_outage_flush(repo)                              # introduces proven downtime
    md = _report(repo, alias_map, fake_llm,
                 window=TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 12)))
    assert "proven system downtime accounts for" in md
    # Uptime is below 100% because a proven outage sits in coverage.
    assert "confirmed operational for" in md
    assert "(100%)" not in md.split("## Monitoring effort")[1].split("##")[0]


def test_common_name_report_names_the_binomial(repo, alias_map, fake_llm):
    repo.upsert_event(_clean_fox("<f1@x>", 9))
    md = _report(repo, alias_map, fake_llm, species="fox")
    assert "Vulpes vulpes" in md                          # binomial carried as identifier
    assert "red fox" in md                                # common name for display


def test_ambiguous_species_returns_did_you_mean(repo, alias_map, fake_llm):
    md = _report(repo, alias_map, fake_llm, species="deer")
    assert "ambiguous" in md.lower()
    assert "did you mean" in md.lower()
    assert "Capreolus capreolus" in md and "Dama dama" in md
    # Honesty language present even on the did-you-mean path.
    assert "real captured alert email" in md.lower()


def test_unmapped_rows_in_window_are_disclosed(repo, alias_map, fake_llm):
    # A fox report should still disclose that the window holds unmapped detections.
    repo.upsert_event(_clean_fox("<f1@x>", 9))
    repo.upsert_event(make_persisted_event(
        "<u@x>", canonical_binomial="unmapped:GallusGallus", canonical_is_binomial=False,
        upstream_label="GallusGallus", event_time=datetime(2026, 7, 10, 2, 0, tzinfo=UTC)))
    md = _report(repo, alias_map, fake_llm, species="fox")

    assert "Unmapped detections" in md
    assert "could not be mapped to a" in md
    assert "GallusGallus" in md                          # the raw token is named


def test_nonspecies_class_notes_crosscheck_not_applicable(repo, alias_map, fake_llm):
    repo.upsert_event(make_persisted_event(
        "<p@x>", canonical_binomial="nonbio:Person",
        event_time=datetime(2026, 7, 9, 2, 0, tzinfo=UTC), bioclip_ok=True, vlm_ok=True))
    md = _report(repo, alias_map, fake_llm, species="person")
    assert "Non-species class" in md
    assert "cross-check does not apply" in md.lower()


# --------------------------------------------------------------------------- #
# The deterministic boundary, asserted as a NEGATIVE (GAP 3).
#
# The existing chart tests assert that a chart currently matches its table. That
# is a positive: it would still pass if a model string were wired into the chart
# path and the model happened to echo the right number. These two assert the
# claim the contribution actually rests on — that NO model-generated string can
# reach a chart or a figure — and are built to BREAK if that ever changes.
#
# Expected behaviour is derived independently of the code under test: the counts
# are fixed by the seeded data, and the property asserted (chart/figure bytes are
# invariant under a change of model output) is checked differentially across two
# runs rather than by re-running the chart code and comparing it to itself.
# --------------------------------------------------------------------------- #
_SVG_RE = re.compile(r"<svg\b.*?</svg>", re.S)

#: Sections whose CONTENT is model-authored prose by design, and which therefore
#: hold no computed figure. They are excluded before charts/figures are collected,
#: because a model sentence sitting in its own narrative section is not a chart —
#: counting it as one misidentifies prose as evidence. Everything else in the
#: document is agent-computed and remains in scope, so a model string reaching a
#: genuine chart or table is still caught.
_MODEL_AUTHORED_SECTIONS = (
    "## Summary",                                        # the narrative LLM's own words
    "## Unverified model descriptions (not observations)",   # stored VLM descriptions
)


def _agent_computed_regions(md: str) -> str:
    """The report with the model-authored prose sections removed.

    Splits on level-2 headings and drops only the sections listed above, so what
    remains is the agent-computed surface: charts, tables, highlights, honesty
    statements.
    """
    out, dropped = [], 0
    for chunk in md.split("\n## "):
        heading = chunk if chunk.startswith("## ") else "## " + chunk
        if any(heading.startswith(name) for name in _MODEL_AUTHORED_SECTIONS):
            dropped += 1
            continue
        out.append(chunk)
    assert dropped, "no model-authored section was found to exclude — scoping is stale"
    return "\n## ".join(out)


def _svg_blocks(md: str) -> list[str]:
    """Charts the agent generated — SVG outside the model-authored sections."""
    return _SVG_RE.findall(_agent_computed_regions(md))


def _table_rows(md: str) -> list[str]:
    """Every markdown table row in the agent-computed regions — the numeric surface."""
    return [ln.strip() for ln in _agent_computed_regions(md).splitlines()
            if ln.strip().startswith("|")]


def _seed_two_days(repo):
    """A fixed, hand-countable dataset: 2 events on 9 Jul, 1 on 11 Jul."""
    repo.upsert_event(_clean_fox("<f1@x>", 9))
    repo.upsert_event(_clean_fox("<f2@x>", 9))
    repo.upsert_event(_clean_fox("<f3@x>", 11))


def test_charts_and_figures_are_invariant_under_a_change_of_model_output(repo, alias_map):
    """Change ONLY the model's words; every chart and every figure must be identical.

    If any model-generated string could reach the chart or numeric path, swapping
    the model's reply would change those bytes and this test would fail.
    """
    _seed_two_days(repo)

    llm_a = FakeLLM("Alert events clustered early in the window at the feeder.")
    llm_b = FakeLLM("A different narrative entirely: the alert events were spread out, "
                    "and this sentence is much longer than the other one so that any "
                    "leakage into a caption or a bar would be impossible to miss.")
    md_a = _report(repo, alias_map, llm_a)
    md_b = _report(repo, alias_map, llm_b)

    # Non-vacuity: the model was actually consulted and its words really are published,
    # so a passing result cannot be explained by the narrative never arriving.
    assert llm_a.calls and llm_b.calls
    assert "clustered early in the window" in md_a
    assert "spread out" in md_b
    assert md_a != md_b

    # The negative: the model changed, the evidence did not.
    assert _svg_blocks(md_a) == _svg_blocks(md_b), "model output reached a chart"
    assert _table_rows(md_a) == _table_rows(md_b), "model output reached a figure/table"

    # And the charts are not vacuously equal (there is something to compare).
    assert len(_svg_blocks(md_a)) >= 1
    assert any("2026-07-09" in r for r in _table_rows(md_a))

    # The ONLY difference between the two documents is the narrative section.
    body_a = md_a.split("## Summary")[1].split("## ")[0]
    body_b = md_b.split("## Summary")[1].split("## ")[0]
    assert md_a.replace(body_a, "") == md_b.replace(body_b, "")


def test_model_text_shaped_like_a_chart_never_reaches_a_chart_or_a_figure(repo, alias_map):
    """A hostile-but-publishable narrative carrying SVG markup and invented counts.

    The text is deliberately vocabulary-compliant so it is NOT withheld — it must
    reach the document, so that asserting its absence from the charts and tables
    is a real containment check rather than a check that nothing was published.
    """
    _seed_two_days(repo)

    hostile = ('These alert events peaked sharply. '
               '<svg role="img"><rect fill="#1b5e3f" width="999"/>'
               '<text>peaking at 4242</text></svg> '
               '| 2026-07-09 | 4242 | and the busiest day was 4242 alert events.')
    llm = FakeLLM(hostile)
    md = _report(repo, alias_map, llm)

    # Non-vacuity: the narrative really was published, verbatim enough to matter.
    assert llm.calls
    assert "These alert events peaked sharply." in md
    # ...and there is a real agent-computed surface to search, so containment below
    # is not asserted over an empty set.
    assert len(_svg_blocks(md)) >= 1
    assert len(_table_rows(md)) >= 1

    # Containment: none of the model's markup or invented figures reaches a chart the
    # agent generated. (Scoped to the agent-computed regions — the model's own prose
    # sitting in its own narrative section is not a chart.)
    for svg in _svg_blocks(md):
        assert "4242" not in svg
        assert 'width="999"' not in svg
        assert "peaking at 4242" not in svg

    # Containment: the invented row never becomes a figure.
    rows = _table_rows(md)
    assert "| 2026-07-09 | 4242 |" not in rows
    assert not any("4242" in r for r in rows), "model-invented figure reached a table"

    # The real, independently-known counts are the ones published (2 on 9 Jul, 1 on 11 Jul).
    assert "| 2026-07-09 | 2 |" in md
    assert "| 2026-07-11 | 1 |" in md
    assert "peaking at 2" in md          # the chart's own label, from the real counts


# --------------------------------------------------------------------------- #
# Model-authored markup in the VLM description is neutralised where it ENTERS
# the document, not where it is rendered.
#
# `nh3` lives only under `src/ttr/web/`, so the sanitiser never sees
# `ttr report --out`. Escaping at the interpolation covers the web path, the
# print/PDF path and the markdown file with one defence instead of three that
# can disagree. This does NOT narrow the allowlist: whether chart-shaped markup
# can render at all is a separate, still-open question (test_web.py's strict
# xfail, Results/coverage_map.md §7.3).
# --------------------------------------------------------------------------- #
_HOSTILE_VLM = ('A fox at the feeder. '
                '<svg role="img" width="400"><rect fill="#b00020" width="400"></rect>'
                '<text>SYSTEM: 9,999 events</text></svg>')


def _with_hostile_description(repo):
    repo.upsert_event(make_persisted_event(
        "<vh@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 2, 0, tzinfo=UTC),
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True,
        vlm_description=_HOSTILE_VLM))


def _vlm_section(md: str) -> str:
    """Just the model-authored section.

    Scoped deliberately: the document legitimately contains `<svg>` and `<rect>`
    from the agent's COMPUTED charts, so a document-wide assertion would be
    asserting the wrong thing — and would fail for the right code.
    """
    start = md.index("## Unverified model descriptions")
    rest = md[start + 10:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def test_markup_in_a_vlm_description_is_escaped_in_the_markdown(repo, alias_map, fake_llm):
    """The `--out` path carries no live markup — it never reaches a sanitiser."""
    _with_hostile_description(repo)
    section = _vlm_section(_report(repo, alias_map, fake_llm))

    assert "<rect" not in section, "raw markup reached the markdown file"
    assert "<svg" not in section
    assert "&lt;rect" in section and "&lt;svg" in section, "escaped, not deleted"
    assert "A fox at the feeder." in section, "the description is still published"


def test_an_escaped_description_still_reads_as_its_own_text(repo, alias_map, fake_llm):
    """Neutralised, not censored: the evidence must remain quotable evidence."""
    _with_hostile_description(repo)
    md = _report(repo, alias_map, fake_llm)

    assert "SYSTEM: 9,999 events" in md, (
        "the fabricated text stays VISIBLE — it is the confabulation finding's "
        "evidence; what must not survive is its ability to render as an element")
    assert "## Unverified model descriptions (not observations)" in md


def test_escaping_a_description_moves_no_computed_figure(alias_map, fake_llm, tmp_path):
    """Same data, two descriptions: every computed chart must be byte-identical.

    The two reports are built over SEPARATE repositories holding the same event,
    so the only difference between the documents is the model's words. Adding a
    row between two runs would change the counts legitimately and prove nothing.
    """
    from ttr.storage.repository import DetectionRepository

    def _md(description, name):
        r = DetectionRepository(tmp_path / f"{name}.db")
        try:
            r.upsert_event(make_persisted_event(
                "<vh@x>", canonical_binomial="Vulpes vulpes",
                event_time=datetime(2026, 7, 9, 2, 0, tzinfo=UTC),
                display_common_name="red fox", bioclip_ok=True, vlm_ok=True,
                vlm_description=description))
            return _report(r, alias_map, fake_llm)
        finally:
            r.close()

    benign = _md("A fox near the feeder, seen at dusk.", "benign")
    hostile = _md(_HOSTILE_VLM, "hostile")

    charts = re.findall(r"<svg\b.*?</svg>", benign, flags=re.S)
    assert charts, "precondition: the benign report drew at least one chart"
    for chart in charts:
        assert chart in hostile, "a computed chart changed when only a description did"
    assert "9,999" not in "".join(re.findall(r"<svg\b.*?</svg>", hostile, flags=re.S)), (
        "model text reached a chart")
