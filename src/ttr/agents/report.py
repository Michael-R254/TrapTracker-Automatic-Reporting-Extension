"""`ReportGeneratorAgent` — consumes RetrievalAgent output only (§5.4).

Design: the factual content (day-by-day ALERT-EVENT counts) and the three
unconditional honesty statements are computed DETERMINISTICALLY here — never
delegated to the LLM. The LLM is used only to add an optional narrative summary
from those facts; if it fails or is a fake, the report is still complete and the
honesty language still present. VLM descriptions are treated as untrusted data
(CLAUDE.md §6): they are quoted, and the summary prompt forbids following any
instruction embedded in them.
"""

from __future__ import annotations

import math
import re
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html import escape as _html_escape
from typing import Optional


def _x(value) -> str:
    """HTML-escape a value for safe inclusion in generated HTML/SVG markup."""
    return _html_escape(str(value), quote=True)


#: Bound on the substitution fixed point. A legend is held inside a chart that is
#: itself held, so a single pass is not enough; more than a couple of levels means
#: a cycle, and looping forever on a report is worse than failing on one.
_SUBSTITUTION_PASSES = 8


class ChartSlots:
    """Trusted, agent-computed markup held OUT of the document while it is built.

    Design record: docs/render-safety.md §2, which explains why the nonce makes
    forgery impossible rather than merely hard - the timing argument below is the
    part that is not obvious from the code.

    Each chart is replaced in the document by an opaque token and kept here, so
    the document carries no chart markup at all until the last step. That is what
    lets the web path insert the charts AFTER sanitising: a narrowed allowlist can
    then forbid SVG outright without taking the real charts with it.

    THE TOKEN IS A PER-RENDER NONCE, and that is the security property. A model
    writing a plausible-looking token into its narrative, or into an image
    description, cannot claim a chart slot: it cannot know the nonce. That is 128
    bits from `secrets`, fresh for every render, and the model's text is already
    fixed by the time it exists. A constant sentinel would be forgeable by anyone
    who had read this file.

    Tokens are deliberately alphanumeric — nothing markdown styles, nothing nh3
    escapes — so a token survives both untouched and lands exactly where the chart
    it stands for would have been.
    """

    _PREFIX = "ttrchart"

    def __init__(self) -> None:
        self._nonce = secrets.token_hex(16)
        self._held: dict[str, str] = {}

    def hold(self, markup: str) -> str:
        """Take custody of chart markup; return the token that stands for it."""
        token = f"{self._PREFIX}{self._nonce}x{len(self._held)}"
        self._held[token] = markup
        return token

    @property
    def held(self) -> dict:
        return dict(self._held)


def _substitute(text: str, held: dict, *, wrapper: bool) -> str:
    """Put the held charts back. Bounded fixed point — see `_SUBSTITUTION_PASSES`.

    ``wrapper`` is the HTML path. Markdown wraps a bare token in ``<p>``, exactly
    as it wraps a bare ``<svg>`` today, so an inline chart keeps that paragraph and
    reads identically. A chart whose markup is its own ``<div>`` block was never
    wrapped, so its enclosing ``<p>`` is taken away with it — otherwise the block
    would return nested one layer deeper than it started.
    """
    for _ in range(_SUBSTITUTION_PASSES):
        before = text
        for token, markup in held.items():
            if wrapper and markup.lstrip().startswith("<div"):
                # Trailing newline restores the blank line markdown leaves around a
                # raw HTML block but not between two <p> elements. Without it the
                # charts are semantically identical and byte-different, and a
                # byte-comparison against the old output is the only check that
                # can prove this stage changed nothing.
                text = text.replace(f"<p>{token}</p>", markup + "\n")
            text = text.replace(token, markup)
        if text == before:
            break
    return text


@dataclass(frozen=True)
class RenderedReport:
    """A report document plus the charts held out of it.

    ``generate_*`` stay the plain-string entry points and substitute immediately,
    which is all the CLI needs. The web path uses ``render_*`` so it can sanitise
    between the two halves.
    """

    markdown: str
    charts: dict

    def as_markdown(self) -> str:
        """The document with its charts inline — byte-identical to the old output."""
        return _substitute(self.markdown, self.charts, wrapper=False)

    def substitute_into(self, html: str, normalise=None) -> str:
        """Insert the charts into already-SANITISED html.

        ``normalise`` lets the caller put each chart through a policy of its OWN
        before it goes in. The web path uses it, and the reason is the point of
        the whole arrangement: the charts must not be governed by the allowlist
        that guards the DOCUMENT, because the next stage narrows that allowlist
        to forbid SVG outright and would take the real charts with it. A separate
        chart policy keeps trusted markup trusted without leaving it unchecked.

        The agent stays out of that decision — it owns no sanitiser and imports
        none. It hands over the markup and the caller applies its own policy,
        which is the same layering rule the rest of the stages follow.
        """
        charts = self.charts
        if normalise is not None:
            charts = {token: normalise(markup) for token, markup in charts.items()}
        return _substitute(html, charts, wrapper=True)


def _as_plain_text(value) -> str:
    """Neutralise markup in MODEL-authored text, where it enters the document.

    Escaped and DISPLAYED here, whereas narrative prose is deleted outright by the
    document sanitiser. That asymmetry is deliberate and is explained in
    docs/render-safety.md §3: a description is a quotation, and deleting part of a
    quotation falsifies it. Do not "fix" one path to match the other.

    The VLM describes an image that arrived as an email attachment, so its output
    is email-derived and untrusted under CLAUDE.md §6 — data to quote, never
    markup to render. It has no legitimate need for any: the section that carries
    it presents it as EVIDENCE, in quotation marks, from a source the surrounding
    text explicitly calls unreliable.

    Escaped HERE, at the interpolation, rather than at render — and that choice is
    most of the value. `nh3` exists only under `src/ttr/web/`, so the sanitiser
    never sees `ttr report --out`, and model markup was travelling verbatim into a
    markdown file that becomes live HTML in any renderer passing HTML through. One
    escape upstream of the split covers the web path, the print/PDF path and the
    file, rather than three defences that can disagree.

    `quote=False` because this is text content inside a quoted line, not an
    attribute value; `_x` above is the attribute-safe variant for agent-generated
    SVG. Neither narrows the allowlist, and the computed charts are untouched —
    whether chart-shaped markup can render AT ALL remains open, and remains a
    strict xfail in `test_web.py`.
    """
    return _html_escape(str(value or ""), quote=False)

from ..agentdefs import load_agent
from ..logging import get_logger
from ..species.aliases import AmbiguousSpecies, is_binomial_key
from ..species.taxonomy import DISTANCE_LABELS, DISTANCES
from ..storage.repository import PersistedEvent
from .retrieval import RetrievalAgent
from .window import TimeWindow

logger = get_logger(__name__)

# The three standing caveats (CLAUDE.md) appear in EVERY report. Each substantive
# CLAIM below is verbatim-stable across reports — the audit value is in the claim
# being identical — and is tied to a concrete figure at render time so it reads as
# "this report's N events", not abstract boilerplate. Only the injected figures vary.
_COUNT_CLAIM = (
    "one alert event is one (alert rule x image) match at best-per-label; two animals in "
    "one frame register as a single event, and other species in frame, sub-threshold "
    "detections, and repeat visits are not represented (Decision 4)"
)
# FIGURE-FREE by design (like _COUNT_CLAIM). The empirical divergence figures
# (within-60s %, mis-dated count/%) are computed PER REPORT from its own dated
# events and injected at render time — never hardcoded, so they can never quote
# another scope's N. (A prior bug baked "475"/"24" from the one-time validation
# study into this constant, so a 458-event report still printed 475.)
_SEND_TIME_CLAIM = (
    "placed by CAMERA CAPTURE TIME, decoded from the image filename and converted from "
    "Europe/London local time to UTC (Decision 3, amended: capture time is the preferred "
    "temporal key where available; alert send time is still stored, never overwritten). "
    "Capture time was validated against the timestamp burned into the image pixels. "
    "Where no capture time could be decoded the alert send time is used instead and the "
    "row is flagged. Send time is a poor proxy for capture time — usually close, but "
    "delivery is not guaranteed: an outage or backlog flushes a queue of older captures "
    "under one send timestamp, mis-dating events by whole days through delivery latency "
    "rather than midnight-boundary rounding"
)
# Scope note on the timezone conversion — a known limitation, not a hedge.
_CAPTURE_TZ_CLAIM = (
    "the Europe/London -> UTC conversion uses real DST rules (zoneinfo), not a fixed "
    "offset, but it is VALIDATED ONLY WITHIN BST: the evaluated dataset "
    "(2026-06-28..2026-07-16) predates the October DST change, so autumn/winter (GMT) "
    "captures, and local times that are ambiguous or non-existent across a DST "
    "transition, remain unvalidated and are flagged per row"
)
_DECISION7_CLAIM = (
    "ingested by a parser whose NOMINAL alert format was validated field-for-field against "
    "a real captured alert email on 2026-07-16 (Decision 7); edge-case variants (zero or "
    "missing attachments, malformed body, test emails) remain validated against the "
    "reconstruction only"
)
# The fourth standing caveat (Change 2). Same verbatim-stable style as the three
# above: the CLAIM body is byte-identical across reports (audit value); only the
# grammatical lead-in and the injected window range vary. It fires whenever the
# comparison is computed — INCLUDING the no-baseline case — so absence of a
# baseline reads as information, never a missing section.
_COMPARISON_CLAIM = (
    "a period-over-period change is the difference between two alert-event counts for the "
    "two dated windows named above — not evidence of a trend, a seasonal pattern, or a "
    "change in animal abundance; with the little history held here it describes only those "
    "two specific windows (Change 2)"
)
def _not_corroborated(record) -> bool:
    """Whether the BioCLIP cross-check failed to CONFIRM the upstream label.

    Wider than ``disagree``: it also covers the rows where a comparison was
    impossible (``not_evaluable`` — a class outside BioCLIP's taxonomy, a failed
    run). Those are not disagreements and are never counted as such, but neither
    are they corroboration, and a report that implied otherwise would overstate
    the evidence. Defined once so every "unconfirmed" figure in this module uses
    the same rule."""
    return record.agreement_flag != "agree"


def _distance_breakdown(records) -> list[tuple[str, str, int]]:
    """``(distance, phrase, count)`` per taxonomic distance, nearest rank first.

    This is what replaced the retired 'indeterminate' bucket: the same rows, now
    reported by HOW FAR BioCLIP's identification was from the upstream label
    rather than as one unexplained pile. Ordering follows ``DISTANCES`` so a
    reader scans from near miss to scene content, not by whichever bucket
    happened to be largest."""
    counts = Counter(r.taxonomic_distance or "undetermined" for r in records)
    return [(d, DISTANCE_LABELS.get(d, d), counts[d]) for d in DISTANCES if counts.get(d)]


#: Decision 4's standing claim, as ONE literal.
#:
#: It was inline in `_bng_limitations` and nowhere else until the project picker
#: needed to print the same sentence under its event count. A second copy is how
#: a claim whose value is its byte-stability across outputs quietly rots, so the
#: words live here and every surface reads them: the report bolds the clause and
#: renders a markdown bullet, `web/picker.py` prints the plain sentence.
#:
#: Split at the bold so the markdown form is DERIVED rather than duplicated —
#: `COUNT_CAVEAT_CLAIM` is what the report emphasises. Reassembling the two halves
#: reproduces the bullet byte for byte; `test_web_projects` pins that.
COUNT_CAVEAT_CLAIM = "Counts are alert events, not animals"
COUNT_CAVEAT_REST = " — not abundance and not a biodiversity value."
COUNT_CAVEAT = COUNT_CAVEAT_CLAIM + COUNT_CAVEAT_REST

# Figure-free form for the pre-query edge pages (did-you-mean / unrecognised).
_VALIDATION_STANDING = (
    "_Standing note: this system's NOMINAL alert format was validated against a real "
    "captured alert email on 2026-07-16; edge-case variants remain validated against the "
    "reconstruction only (Decision 7)._"
)

#: The ONLY prompt-driven component in this module. Its instructions live in a
#: version-controlled Markdown definition rather than in this file — see
#: ``src/ttr/agentdefs/definitions/report-narrative.md`` and
#: ``docs/agent-architecture.md``. Everything else here (counts, effort, downtime,
#: absence classification, charts, the honesty statements, the whole BNG-aligned
#: report) is deterministic and reaches no model, so none of it has a definition.
#:
#: Four fragments, matching how the prompt has always been assembled:
#:   ``instructions``      the base system prompt
#:   ``outage-system``     appended to the system prompt when an outage is proven
#:   ``outage-facts``      the instruction line placed inside the FACTS message
#:   ``rejection-retry``   the vocabulary gate's retry instruction ({terms})
_NARRATIVE_AGENT = "report-narrative"

# Vocabulary the narrative may not use: each term asserts a count of ANIMALS, which
# directly contradicts the Decision 4 claim ("counts are alert events, not animals")
# printed a few sections below it. Word-boundary matched, case-insensitive.
# 'bird feeder' is fine (singular, adjectival); plural 'birds' is not.
_FORBIDDEN_NARRATIVE_TERMS = (
    "sighting", "sightings", "visit", "visits", "visitation", "visitations",
    "animal", "animals", "individual", "individuals", "specimen", "specimens",
    "creature", "creatures", "birds", "appearance", "appearances",
)
_FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(sorted(_FORBIDDEN_NARRATIVE_TERMS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
#: The narrative must also positively name what it is counting, so a model that
#: merely avoids the blacklist (e.g. "458 common wood pigeons") is still caught.
_REQUIRED_NARRATIVE_PHRASES = ("alert event", "detection")

#: A delivery silence shorter than this is never even considered a gap. Only
#: gaps with back-dated captures are ever REPORTED (see `_coverage_gap_highlight`),
#: so this is a noise floor, not the test.
_MIN_DELIVERY_GAP_SECONDS = 2 * 3600

#: A species must be undetected for at least this long before the report calls it
#: an absence. Well above a garden's nightly quiet so ordinary dark hours are not
#: reported as ecology.
_MIN_SPECIES_ABSENCE_SECONDS = 24 * 3600

#: Below this share of confirmed-up time, a period is treated as pure downtime
#: rather than a mix. Keeps a one-minute overlap from being called 'partly'.
_NEGLIGIBLE_UP_FRACTION = 0.10

#: Longest-first cap on the coverage-gap table, so one bad week cannot bury the report.
_MAX_GAPS_SHOWN = 10

#: Bounded retries before the narrative is withheld altogether (see `_narrative`).
_NARRATIVE_MAX_ATTEMPTS = 3

#: Categorical hues for the species-composition pie, assigned in count-descending
#: order (never cycled). Validated all-pairs CVD-safe in light mode; theme green is
#: deliberately avoided. Species beyond these fold into a grey "Other" slice.
_SPECIES_PALETTE = ("#2a78d6", "#eb6834", "#4a3aa7", "#1baf7a", "#e87ba4", "#eda100")
_OTHER_COLOUR = "#9aa0a6"

#: Small, inspectable management-relevance tags keyed by binomial — reference data
#: (domain knowledge, like the alias table), NOT a data source. Used only to flag,
#: for HUMAN review, which detected non-target species would matter to management IF
#: the automated ID is correct. It never asserts the ID is correct. Extend as needed.
_MANAGEMENT_RELEVANCE = {
    "Felis catus": "domestic cat — predation signal at a feeder",
    "Sciurus carolinensis": "grey squirrel — non-native/invasive in the UK",
}

#: Rendered in place of the deployment place name when no site is configured.
#: Mirrors the "NOT RECORDED — required" idiom the BNG sections already use: an
#: absent fact is stated as absent, never filled in with a plausible-looking one.
_SITE_NOT_CONFIGURED = "site name not configured"

#: Version of the BNG-aligned report layout/wording. Surfaced in the document-control
#: block and the appendix so a printed copy is traceable to a report generation.
_BNG_REPORT_VERSION = "2.0"


def narrative_violations(text: str) -> list[str]:
    """Vocabulary violations in an LLM-written summary — PURE, so it is testable
    without a model.

    Returns forbidden animal-counting terms found, plus a marker when the text
    never names what it counts. Empty list means the text is publishable.

    This is ENFORCEMENT, not a request. The prompt asks for the right vocabulary;
    this checks it, because the VLM confabulation A/B measured that this model
    does not reliably obey prompt instructions (docs/evaluation/
    vlm_confabulation_findings.md). An instruction the system does not verify is
    a hope, not a control.
    """
    found = sorted({m.group(1).lower() for m in _FORBIDDEN_RE.finditer(text or "")})
    if text and not any(p in text.lower() for p in _REQUIRED_NARRATIVE_PHRASES):
        found.append("(never names 'alert events'/'detections')")
    return found

# Used to rank VLM descriptions so a genuinely notable observation (another
# species in frame — e.g. a cat) is surfaced, not buried under "several birds at
# a feeder" (expected, not notable). Another species weighs far more than bird
# count/behaviour, which at a bird feeder is unremarkable.
_OTHER_SPECIES_KEYWORDS = (
    "cat", "kitten", "fox", "squirrel", "dog", "rat", "mouse", "hawk", "magpie",
    "crow", "predator",
)
_BEHAVIOUR_KEYWORDS = (
    "several birds", "multiple", "two birds", "three birds", "flying",
    "wings spread", "chasing", "fighting",
)


@dataclass(frozen=True)
class _Comparison:
    """Deterministically-computed period-over-period result (Change 2). Every
    field is arithmetic on retrieved COUNTS — no cause, no interpretation. The LLM
    never receives this; it is rendered verbatim from these figures so the model
    has no opening to narrate a 'why' it has been measured to invent."""
    prior_window: TimeWindow
    current_count: int              # == the day-by-day table's dated total (reconciled)
    prior_count: int
    has_baseline: bool              # prior_count > 0
    baseline_predates_data: bool    # prior window ends before our earliest dated record
    earliest_dated: Optional[datetime]
    delta: Optional[int]            # current - prior, only when has_baseline
    pct: Optional[float]            # 100*delta/prior, only when prior_count > 0
    direction: str                  # up | down | unchanged | no-baseline


class ReportGeneratorAgent:
    def __init__(
        self,
        retrieval: RetrievalAgent,
        llm,
        *,
        low_confidence_threshold: float = 0.5,
        max_notable: int = 5,
        include_crosscheck: bool = True,
        include_comparison: bool = True,
        weather_small_sample_threshold: int = 20,
        site_name: Optional[str] = None,
        include_evidence_images: bool = True,
    ) -> None:
        self._retrieval = retrieval
        self._llm = llm
        #: Appendix A embeds representative CAMERA FRAMES. False withholds them
        #: and says so in the document - which is a different statement from the
        #: "unavailable" branch below, and the difference is the point: one means
        #: the generator could not read the images, the other means someone chose
        #: not to publish them. A report that blurred the two would be making the
        #: exact kind of unstated claim this project exists to avoid.
        self._include_evidence_images = include_evidence_images
        #: Replaced at the start of every render. Present here so a chart
        #: generator called outside one still has somewhere to put its markup.
        self._slots = ChartSlots()
        self._low_conf = low_confidence_threshold
        self._max_notable = max_notable
        self._include_crosscheck = include_crosscheck   # PRESENTATION only
        self._include_comparison = include_comparison
        self._weather_small_n = weather_small_sample_threshold
        # The deployment place name. Configuration, never a literal: a report
        # generated for one site must not name another. Unset renders the same
        # honest placeholder the BNG document control uses rather than a guess.
        self._site_name = site_name or _SITE_NOT_CONFIGURED

    # --- the two halves: build with charts held aside, then substitute -------
    def _hold(self, markup: str) -> str:
        """Hand a computed chart to this render's slots; get its token back."""
        return self._slots.hold(markup)

    def render(self, species: str, window: TimeWindow) -> RenderedReport:
        """The single-species report, with its charts held out of the document."""
        self._slots = ChartSlots()
        return RenderedReport(self._build(species, window), self._slots.held)

    def render_all_species(self, window: TimeWindow) -> RenderedReport:
        self._slots = ChartSlots()
        return RenderedReport(self._build_all_species(window), self._slots.held)

    def render_bng_aligned(self, window: TimeWindow) -> RenderedReport:
        self._slots = ChartSlots()
        return RenderedReport(self._build_bng_aligned(window), self._slots.held)

    def generate(self, species: str, window: TimeWindow) -> str:
        """Markdown with the charts inline — what the CLI and the tests want."""
        return self.render(species, window).as_markdown()

    def generate_all_species(self, window: TimeWindow) -> str:
        return self.render_all_species(window).as_markdown()

    def generate_bng_aligned(self, window: TimeWindow) -> str:
        return self.render_bng_aligned(window).as_markdown()

    def _build(self, species: str, window: TimeWindow) -> str:
        try:
            key, display = self._retrieval.resolve_identity(species)
        except AmbiguousSpecies as exc:
            return self._did_you_mean(species, exc, window)

        if key is None:
            return self._unrecognised(species, window)

        records = self._retrieval.find(species, window)
        return self._render(species, key, display, window, records)

    # ----------------------------------------------- weather correlation view
    def weather_report(self, species: Optional[str], window: TimeWindow) -> str:
        """Standalone weather-correlation report (STAGE view, not yet wired into the
        main reports). ``species=None`` => combined across all species; otherwise
        the resolved species. Reuses the same retrieval seam as the main reports.

        A FOURTH entry point, and it draws a pie — so it opens its own slots and
        substitutes before returning, exactly as `generate_*` do. Missing it would
        have shipped a report with a bare token where a chart belongs, and only a
        byte-comparison caught it.
        """
        self._slots = ChartSlots()
        if species is None:
            records = self._retrieval.find_all(window)
            scope, is_species = "all monitored species", False
        else:
            try:
                key, display = self._retrieval.resolve_identity(species)
            except AmbiguousSpecies as exc:
                return self._did_you_mean(species, exc, window)
            if key is None:
                return self._unrecognised(species, window)
            records = self._retrieval.find(species, window)
            scope, is_species = (display or key), is_binomial_key(key)

        lines = [f"# Weather correlation — {scope}",
                 self._totals_line(window, len(records), len(records), 0),
                 self._weather_section(window, records, scope, per_species=is_species)]
        return _substitute("\n\n".join(p for p in lines if p),
                           self._slots.held, wrapper=False)

    def _weather_section(self, window: TimeWindow, records, scope: str, *,
                         per_species: bool) -> str:
        """Alert-event counts by weather category, with a pie chart and the
        mandatory caveats. Deterministic — no model in the assertion path.

        Only events with a real category (sunny/cloudy/rainy) enter the proportion;
        unavailable/unknown/unenriched are counted and disclosed separately, never
        folded in or silently dropped. N is stated on every figure, a per-species
        sample below the configured threshold is flagged as too small to support a
        weather-preference claim, and each detection share is shown AGAINST THE
        BASE RATE (how much of the window each condition occupied) so the pie can't
        be read as a preference when it merely tracks the weather's availability."""
        enriched = [r for r in records if r.weather_category is not None]
        unenriched = len(records) - len(enriched)
        cat = Counter(r.weather_category for r in enriched)
        categorised = [c for c in ("sunny", "cloudy", "rainy") if cat.get(c)]
        n = sum(cat.get(c, 0) for c in ("sunny", "cloudy", "rainy"))
        unavailable = cat.get("unavailable", 0)
        unknown = cat.get("unknown", 0)
        fallback = sum(1 for r in enriched if r.weather_used_send_fallback)

        # Exposure denominator: category share of the window's cached weather-hours.
        base = self._retrieval.weather_base_rate(window)
        base_n = sum(base.get(c, 0) for c in ("sunny", "cloudy", "rainy"))

        rows = ["## Weather correlation",
                f"_Alert events by weather at the camera's location ({self._site_name}), "
                f"matched to each detection's capture time. **N = {n}** categorised event(s). "
                "Alert events, not animals._"]

        if n == 0:
            rows.append("_No detections in this scope carried a usable weather category "
                        "(see disclosures below); no proportion can be shown._")
        else:
            colours = {"sunny": "#eda100", "cloudy": "#4a3aa7", "rainy": "#2a78d6"}
            slices = [(c, cat[c], colours[c]) for c in categorised]
            rows.append("")
            rows.append(self._hold(self._pie_svg(slices, n, subject="weather category")))
            rows.append("")
            if base_n:
                rows.append("| Weather | Alert events | Share of N | Share of window's hours |")
                rows.append("| --- | ---: | ---: | ---: |")
                for c in categorised:
                    rows.append(f"| {c} | {cat[c]} | {100 * cat[c] / n:.0f}% | "
                                f"{100 * base.get(c, 0) / base_n:.0f}% |")
                rows.append(f"| **Total categorised (N)** | **{n}** | 100% | 100% |")
                rows.append("")
                rows.append(self._weather_base_rate_note(cat, n, base, base_n))
            else:
                rows.append("| Weather | Alert events | Share of N |")
                rows.append("| --- | ---: | ---: |")
                for c in categorised:
                    rows.append(f"| {c} | {cat[c]} | {100 * cat[c] / n:.0f}% |")
                rows.append(f"| **Total categorised (N)** | **{n}** | 100% |")
                rows.append("_(Base-rate column omitted: no weather-hour cache for this "
                            "window, so detection shares can't be compared to what was "
                            "available.)_")

        # Small-sample flag — a per-species N below threshold cannot support a claim.
        if per_species and 0 < n < self._weather_small_n:
            rows.append(f"> ⚠️ **Sample too small for a weather-preference claim.** N = {n} is "
                        f"below the {self._weather_small_n}-event threshold; these proportions "
                        "are shown for completeness but are not evidence that this species "
                        "prefers any weather. Read them as counts, not a preference.")

        # Disclosures — every event not in N is accounted for, never dropped.
        disc = []
        if unavailable:
            disc.append(f"{self._n(unavailable, 'event')} had **no weather data for the exact "
                        "date/hour** (Open-Meteo gap) and are excluded from N — recorded "
                        "per-event as unavailable, not interpolated")
        if unknown:
            disc.append(f"{self._n(unknown, 'event')} matched an **unrecognised weather code** "
                        "and are excluded from N (raw code stored)")
        if unenriched:
            disc.append(f"{self._n(unenriched, 'event')} have **not been weather-enriched** yet "
                        "(run `ttr backfill-weather`)")
        if fallback:
            disc.append(f"{self._n(fallback, 'event')} in N were matched to **send time**, not "
                        "capture time (no filename capture time was recovered for them) — the "
                        "weather is for the send instant, flagged per row")
        if disc:
            rows.append("**Coverage of N:** " + "; ".join(disc) + ".")

        rows.append(self._weather_caveats(n))
        return "\n".join(rows)

    @staticmethod
    def _weather_base_rate_note(cat: "Counter", n: int, base: dict, base_n: int) -> str:
        """One deterministic sentence stating the largest gap between a category's
        detection share and its share of available hours — factual, no cause. This
        is what stops the pie being read as a preference: a category can dominate
        the pie simply by being the commonest weather."""
        gaps = []
        for c in ("sunny", "cloudy", "rainy"):
            if cat.get(c) or base.get(c):
                det = 100 * cat.get(c, 0) / n
                exp = 100 * base.get(c, 0) / base_n
                gaps.append((det - exp, c, det, exp))
        gaps.sort(key=lambda g: abs(g[0]), reverse=True)
        d, c, det, exp = gaps[0]
        if abs(d) < 3:
            return (f"_Read against the base rate, detection shares broadly track how "
                    f"common each condition was — no category stands out by more than a few "
                    "points, so there is nothing here even to describe as a difference._")
        direction = "MORE" if d > 0 else "LESS"
        return (f"_Read against the base rate: **{c}** is {det:.0f}% of events but "
                f"{exp:.0f}% of the window's hours — detections are {direction} frequent in "
                f"{c} than its availability, the opposite of what the raw pie share alone "
                "might suggest. This is a description of the two numbers, not a cause._")

    @staticmethod
    def _weather_caveats(n: int) -> str:
        return (
            "### What this weather view does and does not show\n\n"
            f"- These are **alert-event counts** by weather (N = {n}), **not animal "
            "abundance** and not visit counts — the same count definition as every other "
            "report (Decision 4).\n"
            "- The weather is measured **at the camera's location, not by the animal**: there "
            "is no on-site sensor, so this is the area's hourly weather, not conditions the "
            "animal experienced at the moment of capture.\n"
            "- **The pie is a share of detections, not a preference.** A category can fill "
            "the pie simply by being the commonest weather; the 'share of the window's hours' "
            "column is the base rate to read each slice against. Compare the two columns, "
            "never the pie alone.\n"
            "- The base rate is over **all hours in the window, day and night**, while "
            "detections are daytime — so it **does not control for time-of-day**, nor for "
            "season or feeder-refill schedule, all of which vary with weather. Any of them "
            "could drive a share difference attributed here to weather.\n"
            "- **Correlation is not a weather preference.** Even where detections and hours "
            "diverge, with one camera, one location, and a few weeks of summer data the "
            "confounds above are not separable, so this is not evidence the species prefers "
            "any weather.")

    @staticmethod
    def _pie_svg(slices: list[tuple], total: int, *, subject: str = "weather category",
                 stack_legend: bool = False) -> str:
        """Inline-SVG pie of proportions (same hand-built approach as the bar charts
        — no new charting dependency). Every slice is DIRECT-LABELLED with its share
        and repeated in a legend, so identity never rests on colour alone (the
        validated palette sits in the CVD-safe band only with that secondary
        encoding). ``stack_legend`` puts one legend entry per line below the pie —
        needed when labels are long or numerous (species), vs the compact row used
        for the three weather categories."""
        import math as _m
        cx, cy, r = 150.0, 130.0, 100.0
        w = 300
        h = (248 + 20 * len(slices)) if stack_legend else 300
        parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" '
                 f'aria-label="Pie chart of alert events by {subject}, N={total}: '
                 + ", ".join(f"{lbl} {c} ({100*c/total:.0f}%)" for lbl, c, _col in slices)
                 + '.">',
                 f'<title>Alert events by {subject} (N={total})</title>']
        # Single-category case: a full circle can't be an arc path; draw a disc.
        if len(slices) == 1:
            lbl, c, col = slices[0]
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{col}"></circle>')
            parts.append(f'<text x="{cx}" y="{cy + 4}" fill="#ffffff" font-size="14" '
                         f'text-anchor="middle">{lbl} 100%</text>')
        else:
            ang = -90.0
            for lbl, c, col in slices:
                sweep = 360.0 * c / total
                a0, a1 = _m.radians(ang), _m.radians(ang + sweep)
                x0, y0 = cx + r * _m.cos(a0), cy + r * _m.sin(a0)
                x1, y1 = cx + r * _m.cos(a1), cy + r * _m.sin(a1)
                large = 1 if sweep > 180 else 0
                parts.append(f'<path d="M {cx:.1f} {cy:.1f} L {x0:.1f} {y0:.1f} '
                             f'A {r} {r} 0 {large} 1 {x1:.1f} {y1:.1f} Z" fill="{col}" '
                             'stroke="#ffffff" stroke-width="2"></path>')
                if sweep >= 25:                    # only label slices with room
                    mid = _m.radians(ang + sweep / 2)
                    lx, ly = cx + 0.62 * r * _m.cos(mid), cy + 0.62 * r * _m.sin(mid)
                    parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" fill="#ffffff" '
                                 f'font-size="13" text-anchor="middle">{100*c/total:.0f}%</text>')
                ang += sweep
        # Legend beneath the pie — swatch + label + count/share, so no slice relies
        # on colour alone (and tiny slices that skipped an in-pie label are named).
        if stack_legend:
            y = 250
            for lbl, c, col in slices:
                parts.append(f'<rect x="8" y="{y - 11}" width="12" height="12" rx="2" '
                             f'fill="{col}"></rect>')
                parts.append(f'<text x="25" y="{y}" fill="#1b1b1b" font-size="12">'
                             f'{lbl} — {c} ({100 * c / total:.1f}%)</text>')
                y += 20
        else:
            lx = 8
            for lbl, c, col in slices:
                parts.append(f'<rect x="{lx}" y="{h - 24}" width="12" height="12" rx="2" '
                             f'fill="{col}"></rect>')
                parts.append(f'<text x="{lx + 17}" y="{h - 14}" fill="#1b1b1b" font-size="12">'
                             f'{lbl} ({c})</text>')
                lx += 40 + 9 * len(f"{lbl} ({c})")
        parts.append("</svg>")
        return "".join(parts)

    # --------------------------------------------------- all-species (summary)
    def _build_all_species(self, window: TimeWindow) -> str:
        """A summary across EVERY species in the window (§5.4 sibling of
        ``generate``).

        Reuses the same deterministic building blocks — counts table, chart,
        capture-time disclosure, period-over-period arithmetic, coverage-gap
        detection, honesty block — and changes only the composition: a species
        breakdown replaces per-species highlights, and the four-way absence
        classification collapses to two (a silence in the all-species stream IS a
        system-wide silence by definition, so 'absent while system confirmed up'
        cannot arise). The single-species report entry (`generate`) is genuinely
        species-shaped, so this is a sibling method rather than a flag — see
        Adjustments/all_species_needed_a_sibling_entry.md."""
        records = self._retrieval.find_all(window)
        dated = len(records)
        undated = self._retrieval.count_undated_all()
        total = dated + undated
        by_day = Counter(window.local_date(r.effective_time_utc).isoformat() for r in records)

        shown = sum(by_day.get(d.isoformat(), 0) for d in window.days())
        consistent = shown == dated and total == dated + undated

        gaps = self._proven_gaps(window)                 # shared: outages (see _render)
        op_first, op_last = self._retrieval.send_time_span()   # operational coverage bound
        data_first, _data_last = self._retrieval.data_span()   # effective earliest, for comparison

        lines = ["# Detection report — all monitored species",
                 self._totals_line(window, total, dated, undated)]

        if consistent:
            lines.append(self._narrative("all monitored species", "(all species)",
                                         window, total, dated, undated, by_day, gaps))
        else:
            logger.error("report_all_species_inconsistent",
                         extra={"dated": dated, "shown": shown, "undated": undated})
            lines.append("## Summary\n\n_Internal figures did not reconcile "
                         f"(dated={dated}, shown in table={shown}, undated={undated}); "
                         "narrative withheld rather than describe inconsistent data._")

        comparison = None
        if consistent and self._include_comparison:
            prior = self._retrieval.find_all(window.previous_period())
            comparison = self._compute_comparison(
                None, None, "all monitored species", window, dated,
                prior_count=len(prior), earliest=data_first, earliest_known=True)

        lines.append(self._what_supports_line(window, records, op_first, op_last))
        lines.append(self._counts_section(window, by_day, dated, op_first, op_last))
        lines.append(self._monitoring_effort_section(window, gaps, op_first, op_last))
        lines.append(self._capture_time_disclosure(window, records))
        lines.append(self._time_of_day_section(window, records))
        if comparison is not None:
            lines.append(self._comparison_section("all monitored species", window, comparison))
        lines.append(self._undated_disclosure("all-species", total, dated, undated))
        lines.append(self._species_breakdown(window, records, dated))
        lines.append(self._species_composition_section(window, records, dated))   # all-species ONLY
        lines.append(self._non_target_review_section(window, records))            # all-species ONLY
        lines.append(self._crosscheck_summary_line(records))
        if any(r.weather_category for r in records):     # only once weather is backfilled
            lines.append(self._weather_section(window, records, "all monitored species",
                                               per_species=False))
        lines.append(self._all_species_downtime_section(window, gaps))
        lines.append(self._downtime_dates_section(window, gaps))
        lines.append(self._unmapped_disclosure(window))
        lines.append(self._honesty_section(total, dated, window, records, comparison))
        return "\n\n".join(p for p in lines if p)

    # ---------------------------------------------------------------- BNG-aligned
    def _build_bng_aligned(self, window: TimeWindow) -> str:
        """A site-level MONITORING report structured along BNG (Biodiversity Net
        Gain) reporting conventions — a NEW third report type alongside ``generate``
        (single-species) and ``generate_all_species``.

        It borrows the STRUCTURE and discipline of DEFRA's Biodiversity Gain Plan
        form, NOT its unit arithmetic. This is habitat-condition monitoring evidence
        that could feed a BNG 'habitat management and monitoring plan'; it is NOT a
        Biodiversity Gain Plan and computes NO biodiversity units, no net-gain
        figure, no distinctiveness/trading scores, and no pre/post-development
        comparison.

        It reuses the existing report's own COMPUTED data (event counts, effort,
        downtime, composition, cross-check) as READ-ONLY inputs — it calls no
        existing render method and mutates nothing, so the single-species and
        all-species reports are byte-unchanged. Every figure is derived from this
        report's own event set; nothing about the site is hardcoded, and an absent
        OS grid reference renders a 'NOT RECORDED — required' placeholder rather
        than an invented value (camera identity/location is structurally absent
        under the email source — Decision 5 watch-item)."""
        records = self._retrieval.find_all(window)
        dated = len(records)
        gaps = self._proven_gaps(window)                       # shared outage detection
        op_first, op_last = self._retrieval.send_time_span()   # operational coverage bound
        effort = self._bng_effort_facts(window, gaps, op_first, op_last)   # ONE effort source
        quality = self._bng_quality_facts(records, dated)                  # ONE quality source

        lines = [
            "# Automated Faunal Activity Monitoring Report\n\n"
            "_BNG-aligned supporting evidence — not a statutory Biodiversity Gain Plan_",
            self._bng_disclaimer(),
            self._bng_document_control(window, records),
            self._bng_executive_summary(window, records, dated, effort, quality),
            self._bng_cards(dated, records, effort, quality),
            self._bng_evidence_quality(quality),
            self._bng_site_identity(window),
            self._bng_provenance(records, dated, quality),
            self._bng_baseline(window),
            self._bng_effort(window, records, gaps, op_first, op_last),
            self._bng_survey_constraints(window, records, gaps),
            self._bng_detected_species(window, records, dated),
            self._bng_figures(window, records, dated, gaps, effort),
            self._bng_detection_event_definition(),
            self._bng_ecological_interpretation(window, records, dated, op_first, op_last),
            self._bng_evidence_limitations(window, records, gaps, quality),
            self._bng_management_considerations(window, records),
            self._bng_data_sharing(),
            self._bng_evidence_images(window, records),
            self._bng_appendices(window, records, gaps, effort, quality),
        ]
        return "\n\n".join(p for p in lines if p)

    def _bng_evidence_images(self, window: TimeWindow, records) -> str:
        """Item 11 — one representative thumbnail per non-dominant classification, with
        timestamp, upstream label + confidence, BioCLIP result + confidence, and
        human-review status. Images are embedded as data URIs (no filesystem path is
        exposed). Degrades to an honest 'unavailable' note when no readable image files
        are accessible (or Pillow is not installed)."""
        rows = ["## Appendix A — Representative image evidence"]
        if not self._include_evidence_images:
            rows.append(
                "_**Withheld from this copy of the report.** The representative frames "
                "are camera images of a private property, so they are not included in a "
                "publicly distributed sample. They were generated; they are not missing, "
                "and nothing failed. **No other part of this report is altered** — every "
                "figure, table, chart and caveat is exactly as generated._")
            return "\n\n".join(rows)
        ordered = sorted(self._by_key(records).items(), key=lambda kv: (-len(kv[1]), kv[0]))
        entries = []
        for _k, evs in ordered[1:]:                       # non-dominant classifications
            rep = uri = None
            for e in sorted(evs, key=lambda r: -(r.upstream_confidence or 0)):
                # Larger frame for review. The upstream alert carries no numeric bounding
                # box, so a reliable object crop cannot be derived; the boxed frame (box
                # burned in by the detector) is preferred, else the original.
                uri = self._thumb_data_uri(e.boxed_image_path or e.original_image_path,
                                           max_px=360)
                if uri:
                    rep = e
                    break
            if rep and uri:
                entries.append((rep, uri))
        if not entries:
            rows.append(
                "_Representative-image evidence was unavailable during report generation "
                "(no readable image files were accessible to the report generator). Each "
                "non-dominant classification would otherwise show a frame with its "
                "timestamp, upstream label and confidence, BioCLIP result and confidence, "
                "and human-review status._")
            return "\n\n".join(rows)
        # Heading + intro + thumbnails are wrapped in one keep-together block so the
        # Appendix-A heading and its intro never strand at the foot of a page with the
        # images pushed to the next (the h2's break-after:avoid keeps the h2 attached).
        intro = ('<p><em>One representative frame per non-dominant classification, for image '
                 'review. These are <strong>illustrative full frames</strong> — the upstream '
                 'alert carries no numeric bounding box, so no reliable object crop can be '
                 'derived; they are context for review, not sufficient standalone '
                 'verification. Images are embedded; no filesystem paths are exposed.</em></p>')
        cells = ['<div class="keep-together">', intro, '<div class="thumbs">']
        for rep, uri in entries:
            name = self._chart_name([rep])
            local = rep.effective_time_utc.astimezone(window._zone) if rep.effective_time_utc else None
            tstr = local.strftime("%Y-%m-%d %H:%M") + " local" if local else "no timestamp"
            up = f"{rep.upstream_label or name}"
            up_c = f"{rep.upstream_confidence:.2f}" if rep.upstream_confidence is not None else "n/a"
            bio_taxon = bio_score = "n/a"
            if rep.bioclip_topk:
                bio_taxon, score = rep.bioclip_topk[0][0], rep.bioclip_topk[0][1]
                bio_score = f"{score:.2f}"
            rid = rep.id if rep.id is not None else "n/a"
            cells.append(
                f'<div class="thumb"><img src="{uri}" alt="{_x(name)} illustrative frame">'
                f'<span class="cap"><strong>{_x(name)}</strong><br>record #{_x(rid)}<br>'
                f'{_x(tstr)}<br>upstream: {_x(up)} ({_x(up_c)})<br>'
                f'BioCLIP: {_x(bio_taxon)} ({_x(bio_score)})<br>'
                f'human-reviewed: Not available</span></div>')
        cells.append("</div></div>")                   # close .thumbs and .keep-together
        rows.append("".join(cells))
        return "\n\n".join(rows)

    @staticmethod
    def _thumb_data_uri(path, max_px: int = 200):
        """Delegates to :func:`ttr.thumbnails.thumb_data_uri` — ONE implementation,
        shared with the ingest page's live thumbnail strip. Still returns None on a
        missing file or a core-only (no Pillow) install; never raises."""
        from ..thumbnails import thumb_data_uri
        return thumb_data_uri(path, max_px=max_px)

    def _bng_appendices(self, window: TimeWindow, records, gaps: list, effort: dict,
                        quality: dict) -> str:
        """Item 17 — appendices supported by the available data: calculation definitions,
        downtime dates/durations, model & threshold metadata, data-quality rules, the
        event-grouping rule, and the report-generation version. No secrets or paths."""
        rows = ["## Appendix B — Definitions, metadata and data-quality rules", ""]
        # B1 calculation definitions
        rows += [
            "### B1 Calculation definitions",
            "- **Alert events per 100 confirmed operational hours** = 100 × (alert events) "
            "÷ (confirmed operational hours). Confirmed operational hours = coverage span "
            "(first-to-last detection, clamped to the window) − proven downtime.",
            "- **Active days per 10 calendar days in the coverage span** = 10 × (calendar "
            "days with ≥1 detection) ÷ (calendar days in the coverage span). The "
            "denominator is calendar days, not confirmed-operational days.",
            "- **Operational coverage** = confirmed operational hours ÷ coverage-span hours.",
            "- **BioCLIP corroboration method** = BioCLIP's single best (top-1) label is "
            "resolved to a scientific binomial and compared for **exact equality** with the "
            "upstream label's binomial; both are mapped through the shared alias table, "
            "which absorbs known synonyms. A match is **corroborated**, anything else is "
            "**not corroborated** (never asserted as an incorrect label). The comparison is "
            "label-only: the 0.5 low-confidence flag is a separate display flag and does "
            "**not** enter the corroboration join, and top-1 (not top-k) is used. BioCLIP "
            "is an automated model — corroboration is not human or ecological verification.",
            "",
        ]
        # B2 downtime dates and durations
        rows.append("### B2 Proven downtime — dates and durations")
        if gaps:
            rows.append("| Outage (UTC) | Duration | Local dates affected |")
            rows.append("| --- | ---: | --- |")
            for start, end, secs, *_ in sorted(gaps, key=lambda g: g[0]):
                touched = self._fmt_day_list(self._days_touched(window, start, end))
                rows.append(f"| {start.strftime('%d %b %H:%M')} → "
                            f"{end.strftime('%d %b %H:%M')} | {secs / 3600:.1f}h | {touched} |")
        else:
            rows.append("_No proven downtime was evidenced in this window._")
        rows.append("")
        # B3 model & threshold metadata
        models = sorted({r.bioclip_model for r in records if r.bioclip_model}) or ["Not recorded"]
        rows += [
            "### B3 Model and threshold metadata",
            f"- **BioCLIP model:** {', '.join(models)}",
            "- **Upstream classifier / model version:** Not recorded (not transmitted in the alert).",
            f"- **Low-confidence flag threshold:** {self._low_conf:g}",
            "- **Upstream confidence:** stored as delivered — 2 dp, best-per-label only.",
            "",
        ]
        # B4 data-quality rules
        rows += [
            "### B4 Data-quality status rules",
            "- Overall status = **Verified** if every classification is human-reviewed, "
            "**Partially verified** if some are, otherwise **Provisional**.",
            "- No human-review record exists in the system, so the status is "
            f"**{quality['status']}** and per-row human-review shows 'Not available'.",
            "",
        ]
        # B5 event-grouping rule
        rows += [
            "### B5 Event-grouping rule",
            "- Alert events are **not** grouped into independent visits/encounters; totals "
            "are raw alert-event counts. Temporally adjacent events may be the same animal "
            "or visit.",
            "",
        ]
        # B6 report generation
        rows += [
            "### B6 Report generation",
            f"- **Report version:** {_BNG_REPORT_VERSION}",
            f"- **Generated:** {window.local_date(datetime.now(timezone.utc)).isoformat()} "
            f"({window.tz})",
            "- **Data source:** dedicated recipient mailbox (TrapTracker RT alert emails).",
        ]
        return "\n".join(rows)

    # ------------------------------------------------------------- cards & charts
    def _bng_cards(self, dated: int, records, effort: dict, quality: dict) -> str:
        """Item 7 — headline summary cards (HTML, styled by _PRINT_STYLE; each card is
        break-inside:avoid so it never splits across a page). Figures derived from the
        shared facts. Values are plain text, HTML-escaped."""
        n_species = sum(1 for _k, evs in self._by_key(records).items()
                        if not self._key_label(evs[0])[1])
        op = f"{effort['operational_h']:.1f}" if effort["has_data"] else "—"
        cov = f"{effort['uptime']:.0f}%" if effort["has_data"] else "—"
        cards = [
            ("Total alert events", str(dated), "raw count, not animals"),
            ("Provisional species labels", str(n_species), "automated, unverified"),
            ("Confirmed operational hours", op, "over known status only"),
            ("Operational coverage", cov, "of covered time"),
            ("BioCLIP not corroborated", f"{quality['pct_unconf']:.0f}%",
             f"{quality['unconfirmed']} of {quality['total']}"),
        ]
        out = ['<div class="cards">']
        for k, v, s in cards:
            out.append(f'<div class="card"><span class="k">{_x(k)}</span>'
                       f'<span class="v">{_x(v)}</span><span class="s">{_x(s)}</span></div>')
        out.append("</div>")
        return "## Headline summary\n\n" + "".join(out)

    @staticmethod
    def _by_key(records) -> dict:
        by_key: dict[str, list] = {}
        for r in records:
            by_key.setdefault(r.canonical_binomial, []).append(r)
        return by_key

    def _bng_figures(self, window: TimeWindow, records, dated: int, gaps: list,
                     effort: dict) -> str:
        """Item 10 — four print-friendly inline-SVG charts: daily alert events by
        species, daily monitoring status (operational vs downtime vs no-data), activity
        by hour, and species composition. Accessible: every series is labelled and the
        status chart uses fill + hatch + text (never colour alone); zero detections are
        drawn distinctly from downtime and from no-data. No invented data."""
        rows = ["## Activity and monitoring figures"]
        if dated == 0:
            rows.append("_No detections in this window to chart._")
            return "\n\n".join(rows)
        rows.append(self._hold(self._chart_daily_species(window, records, gaps)))
        rows.append(self._hold(self._chart_monitoring_status(window, records, gaps, effort)))
        rows.append(self._hold(self._chart_hour_of_day(window, records)))
        rows.append(self._hold(self._chart_composition(records, dated)))
        return "\n\n".join(rows)

    # ---- individual charts (compact, sanitiser-safe SVG: rect/line/text/g only) ----
    def _downtime_local_days(self, window: TimeWindow, gaps: list) -> set:
        days: set = set()
        for start, end, *_ in gaps:
            days |= set(self._days_touched(window, start, end))
        return days

    def _chart_daily_species(self, window: TimeWindow, records, gaps: list) -> str:
        """Stacked daily bars by species over the coverage span; downtime days carry a
        hatched baseline band so a zero-height day reads as 'downtime', not 'quiet'."""
        by_key = self._by_key(records)
        ordered = sorted(by_key.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        colours = {k: (_SPECIES_PALETTE[i] if i < len(_SPECIES_PALETTE) else _OTHER_COLOUR)
                   for i, (k, _e) in enumerate(ordered)}
        days = [d for d in window.days()]
        # Restrict to the coverage span (first..last day with data) to avoid a long
        # empty tail; no-data days outside coverage are not plotted as zero.
        present = sorted({window.local_date(r.effective_time_utc) for r in records})
        if present:
            days = [d for d in days if present[0] <= d <= present[-1]]
        counts = {d: {} for d in days}
        for k, evs in ordered:
            for e in evs:
                d = window.local_date(e.effective_time_utc)
                if d in counts:
                    counts[d][k] = counts[d].get(k, 0) + 1
        down = self._downtime_local_days(window, gaps)
        day_tot = {d: sum(counts[d].values()) for d in days}
        peak = max(day_tot.values()) if day_tot else 1
        W, H, padL, padB, padT = 680, 200, 34, 44, 12
        n = max(len(days), 1)
        bw = (W - padL - 8) / n
        plot_h = H - padB - padT
        svg = [f'<svg viewBox="0 0 {W} {H}" role="img" '
               f'aria-label="Daily alert events by species over the coverage span" '
               f'preserveAspectRatio="xMinYMin meet">']
        svg.append(f'<line x1="{padL}" y1="{padT + plot_h}" x2="{W - 4}" '
                   f'y2="{padT + plot_h}" stroke="#888" stroke-width="1"/>')
        # y gridline at peak
        svg.append(f'<text x="4" y="{padT + 8}" font-size="9" fill="#666">{peak}</text>')
        svg.append(f'<text x="4" y="{padT + plot_h}" font-size="9" fill="#666">0</text>')
        for i, d in enumerate(days):
            x = padL + i * bw
            if d in down:                                    # hatched downtime baseline band
                svg.append(f'<rect x="{x:.1f}" y="{padT + plot_h - 4}" width="{bw - 1:.1f}" '
                           f'height="4" fill="#c9c2b0"/>')
                for hx in range(int(x), int(x + bw), 4):
                    svg.append(f'<line x1="{hx}" y1="{padT + plot_h}" x2="{hx + 4}" '
                               f'y2="{padT + plot_h - 4}" stroke="#8a7f66" stroke-width="0.6"/>')
            y = padT + plot_h
            for k, _evs in ordered:
                c = counts[d].get(k, 0)
                if not c:
                    continue
                h = (c / peak) * (plot_h - 6)
                y -= h
                svg.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(bw - 1, 1):.1f}" '
                           f'height="{h:.1f}" fill="{colours[k]}"/>')
            if i == 0 or i == len(days) - 1 or i == len(days) // 2:
                svg.append(f'<text x="{x:.1f}" y="{H - 26}" font-size="8" fill="#666">'
                           f'{d.strftime("%d %b")}</text>')
        svg.append("</svg>")
        legend = self._hold(
            self._svg_legend([(colours[k], self._chart_name(evs)) for k, evs in ordered]
                             + [("#8a7f66", "day affected by proven downtime")]))
        return ('<div class="keep-together"><p><strong>Daily alert events by species</strong> '
                "(coverage span; a hatched baseline marks a day AFFECTED by proven downtime "
                "— outages are often partial-day, so this does not mean the whole day was "
                "down — distinct from a genuinely quiet day):</p>"
                + "".join(svg) + legend + "</div>")

    def _chart_monitoring_status(self, window: TimeWindow, records, gaps: list,
                                 effort: dict) -> str:
        """A per-day status strip: operational (solid), downtime (hatched), no-data
        (outline). Fill + hatch + legend text — never colour alone."""
        days = list(window.days())
        present = {window.local_date(r.effective_time_utc) for r in records}
        cov_lo = min(present) if present else None
        cov_hi = max(present) if present else None
        W, H, padL, padT, ch = 680, 46, 34, 14, 20
        n = max(len(days), 1)
        cw = (W - padL - 8) / n
        svg = [f'<svg viewBox="0 0 {W} {H}" role="img" '
               f'aria-label="Daily monitoring status: operational vs proven-downtime share, '
               f'or no data" preserveAspectRatio="xMinYMin meet">']
        for i, d in enumerate(days):
            x = padL + i * cw
            in_cov = cov_lo is not None and cov_lo <= d <= cov_hi
            if in_cov:
                # Operational base, then a hatched overlay whose WIDTH is the share of the
                # day affected by proven downtime (often partial), so a partly-down day is
                # not shown as fully down.
                svg.append(f'<rect x="{x:.1f}" y="{padT}" width="{cw - 1:.1f}" '
                           f'height="{ch}" fill="#1b5e3f"/>')
                frac = self._day_downtime_fraction(window, d, gaps)
                if frac > 0:
                    hw = frac * (cw - 1)
                    hx0 = x + (cw - 1) - hw
                    svg.append(f'<rect x="{hx0:.1f}" y="{padT}" width="{hw:.1f}" '
                               f'height="{ch}" fill="#e7e1d2"/>')
                    for hx in range(int(hx0), int(hx0 + hw) + 1, 4):
                        svg.append(f'<line x1="{hx}" y1="{padT}" x2="{hx + 4}" '
                                   f'y2="{padT + ch}" stroke="#8a7f66" stroke-width="0.6"/>')
            else:
                svg.append(f'<rect x="{x:.1f}" y="{padT}" width="{cw - 1:.1f}" '
                           f'height="{ch}" fill="#ffffff" stroke="#bbb" stroke-width="0.6" '
                           f'stroke-dasharray="2 2"/>')
        svg.append(f'<text x="{padL}" y="{H - 4}" font-size="8" fill="#666">'
                   f'{days[0].strftime("%d %b")}</text>')
        svg.append(f'<text x="{W - 4}" y="{H - 4}" font-size="8" fill="#666" '
                   f'text-anchor="end">{days[-1].strftime("%d %b")}</text>')
        svg.append("</svg>")
        legend = self._hold(
            self._svg_legend([("#1b5e3f", "operational"),
                              ("#8a7f66", "affected by proven downtime (hatched share)"),
                              ("#ffffff", "no data / outside coverage (dashed)")]))
        return ('<div class="keep-together"><p><strong>Daily monitoring status</strong> '
                "(one cell per day; the hatched share of a cell is the portion of that day "
                "affected by proven downtime — often partial — distinct from no-data):</p>"
                + "".join(svg) + legend + "</div>")

    def _day_downtime_fraction(self, window: TimeWindow, day, gaps: list) -> float:
        """Share (0–1) of a LOCAL calendar day overlapped by proven downtime, so the
        status strip can show partial-day outages rather than a binary whole-day state."""
        z = window._zone
        ds = datetime.combine(day, time.min, tzinfo=z).astimezone(timezone.utc)
        de = datetime.combine(day, time.max, tzinfo=z).astimezone(timezone.utc)
        total = (de - ds).total_seconds()
        overlap = 0.0
        for start, end, *_ in gaps:
            s, e = max(start, ds), min(end, de)
            if e > s:
                overlap += (e - s).total_seconds()
        return min(overlap / total, 1.0) if total else 0.0

    def _chart_hour_of_day(self, window: TimeWindow, records) -> str:
        """Alert events by local hour of day (0–23) — vertical bars."""
        counts = [0] * 24
        for r in records:
            counts[window.local_hour(r.effective_time_utc)] += 1
        peak = max(counts) or 1
        W, H, padL, padB, padT = 680, 150, 30, 30, 10
        bw = (W - padL - 8) / 24
        plot_h = H - padB - padT
        svg = [f'<svg viewBox="0 0 {W} {H}" role="img" '
               f'aria-label="Alert events by hour of day" preserveAspectRatio="xMinYMin meet">']
        svg.append(f'<line x1="{padL}" y1="{padT + plot_h}" x2="{W - 4}" '
                   f'y2="{padT + plot_h}" stroke="#888" stroke-width="1"/>')
        svg.append(f'<text x="2" y="{padT + 8}" font-size="9" fill="#666">{peak}</text>')
        for h in range(24):
            x = padL + h * bw
            bh = (counts[h] / peak) * (plot_h - 4)
            svg.append(f'<rect x="{x:.1f}" y="{padT + plot_h - bh:.1f}" '
                       f'width="{max(bw - 1.5, 1):.1f}" height="{bh:.1f}" fill="#2a78d6"/>')
            if h % 3 == 0:
                svg.append(f'<text x="{x:.1f}" y="{H - 12}" font-size="8" fill="#666">'
                           f'{h:02d}</text>')
        svg.append(f'<text x="{padL}" y="{H - 2}" font-size="8" fill="#888">hour of day '
                   f'(local, Europe/London)</text>')
        svg.append("</svg>")
        return ('<div class="keep-together"><p><strong>Activity by hour of day</strong> '
                "(local; hours with no bar had no detections in this window):</p>"
                + "".join(svg) + "</div>")

    def _chart_composition(self, records, dated: int) -> str:
        """Species composition as a HORIZONTAL bar chart (a pie would hide the minor
        categories under the dominant one). Each row carries its exact count + share, so
        even a tiny bar is readable. These are automated alert-event classifications."""
        by_key = self._by_key(records)
        ordered = sorted(by_key.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        colours = {k: (_SPECIES_PALETTE[i] if i < len(_SPECIES_PALETTE) else _OTHER_COLOUR)
                   for i, (k, _e) in enumerate(ordered)}
        rowh, W, padL, barx = 20, 680, 6, 250
        maxbar = W - barx - 60
        H = rowh * len(ordered) + 14
        peak = max(len(evs) for _k, evs in ordered) or 1
        svg = [f'<svg viewBox="0 0 {W} {H}" role="img" '
               f'aria-label="Species composition of alert events" '
               f'preserveAspectRatio="xMinYMin meet">']
        for i, (k, evs) in enumerate(ordered):
            y = 10 + i * rowh
            name = self._chart_name(evs)
            svg.append(f'<text x="{padL}" y="{y + 4}" font-size="9.5" fill="#1b1b1b">'
                       f'{_x(name[:40])}</text>')
            bw = max((len(evs) / peak) * maxbar, 2)
            svg.append(f'<rect x="{barx}" y="{y - 6}" width="{bw:.1f}" height="12" '
                       f'fill="{colours[k]}"/>')
            svg.append(f'<text x="{barx + bw + 4:.1f}" y="{y + 4}" font-size="9" '
                       f'fill="#333">{len(evs)} ({100 * len(evs) / dated:.1f}%)</text>')
        svg.append("</svg>")
        return ('<div class="keep-together"><p><strong>Species composition of alert '
                "events</strong> (automated classifications; horizontal bars with exact "
                "counts so minor labels stay readable — a pie would hide them):</p>"
                + "".join(svg) + "</div>")

    @staticmethod
    def _chart_name(evs) -> str:
        return evs[0].display_common_name or (evs[0].canonical_binomial or "(no key)")

    @staticmethod
    def _svg_legend(items) -> str:
        """A colour+label legend (styled by _PRINT_STYLE .legend). Swatches are tiny
        inline SVGs (no inline CSS), and every entry carries a TEXT label, so identity is
        never colour-alone."""
        parts = ['<div class="legend">']
        for colour, label in items:
            parts.append(
                f'<span><svg viewBox="0 0 10 10" width="10" height="10" role="img" '
                f'aria-label=""><rect x="0" y="0" width="10" height="10" rx="2" '
                f'fill="{_x(colour)}" stroke="#999" stroke-width="0.5"/></svg>'
                f'{_x(label)}</span>')
        parts.append("</div>")
        return "".join(parts)

    def _bng_document_control(self, window: TimeWindow, records) -> str:
        """Item 1 — a compact document-control table. Every value is derived from the
        data/config or shown as 'Not recorded'/'Not provided'; nothing is fabricated.
        Site identity, camera id, grid reference, and the people fields are simply not
        carried by the email source, so they render honestly as unavailable."""
        report_date = window.local_date(datetime.now(timezone.utc)).isoformat()
        models = sorted({r.bioclip_model for r in records if r.bioclip_model})
        bioclip_ver = ", ".join(models) if models else "Not recorded"
        rows = [
            "## Document control",
            "",
            "| Field | Value |",
            "| --- | --- |",
            "| Site name | Not recorded |",
            "| Monitoring point / camera ID | Not recorded |",
            "| OS grid reference | Not recorded — required |",
            f"| Monitoring period | {window.label()} |",
            f"| Report version | {_BNG_REPORT_VERSION} |",
            f"| Date generated | {report_date} |",
            "| Prepared by | Not provided |",
            "| Reviewed by | Not provided |",
            "| Data source | Dedicated recipient mailbox — TrapTracker RT alert emails |",
            "| Upstream classifier / model version | Not recorded — not transmitted in the alert |",
            f"| BioCLIP model version | {bioclip_ver} |",
        ]
        return "\n".join(rows)

    def _bng_evidence_quality(self, quality: dict) -> str:
        """Item 3 — a prominent evidence-quality panel making the identification
        uncertainty impossible to miss. Non-corroboration is never stated as proof the
        upstream label is wrong; the status derives from `_bng_quality_facts`."""
        q = quality
        if q["total"] == 0:
            return ("## Evidence quality\n\n_No classifications in this window to "
                    "assess._")
        reviewed = "Not available" if q["reviewed"] == 0 else str(q["reviewed"])
        return "\n".join([
            "## Evidence quality",
            "",
            f"> **BioCLIP did not independently corroborate {q['unconfirmed']} of "
            f"{q['total']} upstream classifications (~{q['pct_unconf']:.0f}%).** Lack of "
            "corroboration does not necessarily indicate an incorrect classification, but "
            "the species-level results remain provisional until image review.",
            "",
            "| Evidence-quality measure | Value |",
            "| --- | ---: |",
            f"| Total upstream classifications | {q['total']} |",
            f"| BioCLIP exact-label corroborated | {q['confirmed']} |",
            f"| Not corroborated | {q['unconfirmed']} |",
            f"| Exact-label agreement rate | {q['pct_conf']:.0f}% |",
            f"| Exact-label non-agreement rate | {q['pct_unconf']:.0f}% |",
            f"| Classifications human-reviewed | {reviewed} |",
            f"| **Overall status** | **{q['status']}** |",
            "",
            "_Corroboration method: BioCLIP's single best (top-1) label is resolved to a "
            "scientific binomial and compared for exact equality with the upstream label's "
            "binomial (both mapped through the shared alias table, which absorbs known "
            "synonyms); a match is 'corroborated', anything else is 'not corroborated'. The "
            "comparison is label-only — the 0.5 low-confidence flag does not enter it — and "
            "BioCLIP is an automated model, not human or ecological verification (see "
            "Appendix B1)._",
        ])

    @staticmethod
    def _bng_disclaimer() -> str:
        """The condensed not-a-Gain-Plan / no-units statement, verbatim, at the very top."""
        return (
            "> **This report adopts the structure of a Biodiversity Gain Plan to present "
            "species-monitoring evidence. It is not a statutory Biodiversity Gain Plan and "
            "does not calculate biodiversity units.** Camera-trap detections are presented "
            "as evidence of faunal activity only.")

    def _coverage_span_days(self, window: TimeWindow, op_first, op_last) -> int:
        """Number of LOCAL calendar days in the data-coverage span (first-to-last
        detection, clamped to the window) — the honest denominator for 'days with
        data'. NOT the requested-window day count: the window's extra days are
        unknown-status (§5 / baseline), not species-absent, so counting them as absence
        would be the very no-data-is-not-no-detection error the report avoids. Uses the
        same send-time span as the effort section. 0 when the window holds no data."""
        if op_first is None or op_last is None:
            return 0
        cov_start = max(window.start_utc, op_first)
        cov_end = min(window.end_utc, op_last)
        if cov_end < cov_start:
            return 0
        return (window.local_date(cov_end) - window.local_date(cov_start)).days + 1

    def _bng_effort_facts(self, window: TimeWindow, gaps: list, op_first, op_last) -> dict:
        """ONE definition of the effort figures, shared by §4, the executive summary,
        the headline cards, and the effort-standardised indices — so no two sections
        can disagree. Identical formulas to the standard monitoring report's effort
        section: coverage is the first-to-last detection span clamped to the window;
        operational = coverage − proven downtime. Never rates over unknown status."""
        window_h = (window.end_utc - window.start_utc).total_seconds() / 3600
        downtime_h = self._downtime_hours(gaps, window)
        cov_days = self._coverage_span_days(window, op_first, op_last)
        if op_first is None or op_last is None:
            return {"has_data": False, "window_h": window_h, "coverage_h": 0.0,
                    "downtime_h": downtime_h, "operational_h": 0.0, "uptime": 0.0,
                    "cov_start": None, "cov_end": None, "cov_days": 0}
        cov_start = max(window.start_utc, op_first)
        cov_end = min(window.end_utc, op_last)
        coverage_h = max((cov_end - cov_start).total_seconds() / 3600, 0.0)
        operational_h = max(coverage_h - downtime_h, 0.0)
        uptime = 100 * operational_h / coverage_h if coverage_h else 0.0
        return {"has_data": True, "window_h": window_h, "coverage_h": coverage_h,
                "downtime_h": downtime_h, "operational_h": operational_h, "uptime": uptime,
                "cov_start": cov_start, "cov_end": cov_end, "cov_days": cov_days}

    @staticmethod
    def _bng_quality_facts(records, dated: int) -> dict:
        """ONE definition of the identification-quality figures, shared by the evidence-
        quality panel, the executive summary and the cards. 'Confirmed' means the BioCLIP
        cross-check AGREED with the upstream label; everything else is not independently
        corroborated (never asserted as wrong). Human-review data does not exist in the
        system, so the overall status derives to 'Provisional'."""
        confirmed = sum(1 for r in records if r.agreement_flag == "agree")
        unconfirmed = dated - confirmed
        reviewed = 0                                   # no human-review field exists
        pct_unconf = 100 * unconfirmed / dated if dated else 0.0
        pct_conf = 100 * confirmed / dated if dated else 0.0
        if dated and reviewed >= dated:
            status = "Verified"
        elif reviewed > 0:
            status = "Partially verified"
        else:
            status = "Provisional"
        return {"total": dated, "confirmed": confirmed, "unconfirmed": unconfirmed,
                "reviewed": reviewed, "pct_conf": pct_conf, "pct_unconf": pct_unconf,
                "status": status}

    def _bng_executive_summary(self, window: TimeWindow, records, dated: int,
                               effort: dict, quality: dict) -> str:
        """Item 6 — a cautious, plain-language overview a non-specialist can read in under a
        minute. Every figure is derived from this window's own data (event total, species
        count, dominant classification, less-frequent labels, operational hours/coverage,
        BioCLIP corroboration). It states the corroboration limit, the absence of
        competent-ecologist verification, the baseline purpose, and the explicit
        exclusions — and never implies the machine classifications are verified records."""
        rows = ["## Executive summary"]
        start = window.local_date(window.start_utc).isoformat()
        end = window.local_date(window.end_utc).isoformat()
        exclusions = ("This report is a monitoring baseline for future comparison; it does "
                      "not measure abundance, habitat condition, or site-wide biodiversity, "
                      "and it calculates no biodiversity units. It is not a statutory "
                      "Biodiversity Gain Plan.")
        if dated == 0:
            rows.append(
                f"Between {start} and {end}, one fixed feeder-facing monitoring point "
                f"recorded no alert events. {exclusions}")
            return "\n\n".join(rows)

        by_key: dict[str, list] = {}
        for r in records:
            by_key.setdefault(r.canonical_binomial, []).append(r)
        ordered = sorted(by_key.items(), key=lambda kv: (-len(kv[1]), kv[0]))

        def common(evs) -> str:
            return evs[0].display_common_name or self._key_label(evs[0])[0]

        # Biological-species count and less-frequent label count, both derived.
        n_species = sum(1 for _k, evs in ordered if not self._key_label(evs[0])[1])
        _dk, dom = ordered[0]
        dom_pct = 100 * len(dom) / dated
        n_minor = max(n_species - 1, 0)

        sents = [
            f"Between {start} and {end}, one fixed feeder-facing monitoring point recorded "
            f"{self._n(dated, 'alert event')} across {n_species} automated species "
            f"classifications."]
        if effort["has_data"]:
            sents.append(
                f"The system was confirmed operational for {effort['operational_h']:.1f} "
                f"hours ({effort['uptime']:.0f}% of covered time).")
        sents.append(
            f"The most frequent automated classification was {common(dom)} "
            f"({dom_pct:.1f}% of events), with {self._n(n_minor, 'less-frequent label')} "
            "recorded less often.")
        sents.append(
            f"BioCLIP independently corroborated only {quality['pct_conf']:.0f}% of "
            f"classifications ({quality['unconfirmed']} of {quality['total']} not "
            "corroborated), and no competent ecologist has verified the identifications, so "
            "the species-level results are provisional.")
        sents.append(exclusions)
        rows.append(" ".join(sents))
        return "\n\n".join(rows)

    def _bng_site_identity(self, window: TimeWindow) -> str:
        """§1 — mirrors the Gain Plan's site-identity section. The OS grid reference
        is NOT in the detection data (no camera location is transmitted), so it renders
        an explicit 'required' placeholder and is never fabricated."""
        report_date = window.local_date(datetime.now(timezone.utc)).isoformat()
        return "\n".join([
            "## 1. Submission and site identity",
            "",
            f"- **Report date (generated):** {report_date}",
            f"- **Monitoring window:** {window.label()}",
            "- **Site / monitoring point:** a single fixed, feeder-facing trail camera at "
            "one location. The detection data carries no camera identity or coordinates "
            "(none is transmitted with the alerts), so the site is described from the "
            "known deployment, not asserted from the data.",
            "- **OS grid reference (camera location):** **NOT RECORDED — required.** No "
            "surveyed grid reference is present in the detection data; a real submission "
            "requires one, and none is fabricated here.",
            "- **Monitoring-point ID:** Not recorded. **Camera orientation:** Not recorded. "
            "**Deployment start / end:** Not recorded. (None of these is transmitted with "
            "the alert; none is inferred.)",
            "- **Feeder-facing status:** yes — the camera faces a feeder (known deployment "
            "context, not asserted from the data).",
            "- **Site / camera-position map:** Not available — no map or camera-position "
            "diagram is available to the report generator.",
            "- **Monitoring area — what it covers and does not:** the field of view of one "
            "fixed camera pointed at a feeder. It records what passes that single point "
            "within the camera's frame and trigger. It does **not** cover the wider site, "
            "other habitat features, or anything outside the camera's field of view; its "
            "temporal coverage is bounded (see §5).",
        ])

    def _bng_provenance(self, records, dated: int, quality: dict) -> str:
        """§2 — the HONEST INVERSE of the Gain Plan's competent-person declaration:
        states what was automated vs human-verified. The corroboration figure comes from
        the shared quality facts, so it matches the evidence-quality panel exactly."""
        rows = [
            "## 2. Provenance and competence",
            "_Unlike a statutory Biodiversity Gain Plan, this report does not include "
            "certification by a competent ecologist. Instead, this section explains which "
            "aspects of the report were generated automatically and which require human "
            "verification._",
            "",
            "- **No competent ecologist has verified the species identifications in this "
            "report.** Every identification is a machine output routed to a human for "
            "review, not certified by one.",
            "- **Identifications are automated:** the upstream classifier assigns the "
            "species label, and an independent BioCLIP taxonomic model provides a "
            "cross-check. Both are model outputs.",
        ]
        if dated == 0:
            rows.append("- **Cross-check outcome:** no detections in this window to cross-check.")
        else:
            rows.append(
                f"- **Cross-check outcome (derived from this window):** BioCLIP did "
                f"**not corroborate {quality['unconfirmed']} of {quality['total']} "
                f"({quality['pct_unconf']:.0f}%)** of the upstream labels (see the "
                "Evidence-quality panel). A label that was not corroborated is not "
                "necessarily incorrect, but it is not an independently corroborated one "
                "either.")
        rows.append(
            "- **Non-target identifications are provisional:** identifications of "
            "less-frequent (non-dominant) species are especially uncertain and must be "
            "checked against the actual images before any use.")
        return "\n".join(rows)

    def _bng_baseline(self, window: TimeWindow) -> str:
        """§3 — this window establishes a BASELINE (relevant date = window start); no
        prior baseline exists, so no change is computed."""
        start = window.local_date(window.start_utc).isoformat()
        return "\n".join([
            "## 3. Baseline status",
            "",
            f"- This monitoring window **establishes a baseline**. The relevant date is "
            f"the window start (**{start}**); the first record in the window forms the "
            "initial baseline observation.",
            "- **No prior baseline exists** to measure against. This report therefore "
            "states current activity-monitoring evidence only.",
            "- **No change is computed** — no trend is presented as habitat change, and no "
            "difference of any kind is calculated. A later monitoring window could be "
            "compared against this baseline, but that comparison is not made here.",
        ])

    def _bng_effort(self, window: TimeWindow, records, gaps: list, op_first, op_last) -> str:
        """§4 — the SAME effort figures as the standard monitoring report (identical
        formulas over the shared ``_downtime_hours`` and the send-time span), under a
        survey-effort heading. Recomputed read-only; the existing section is untouched."""
        f = self._bng_effort_facts(window, gaps, op_first, op_last)
        rows = ["## 4. Monitoring period and survey effort",
                "_The monitoring period and effort behind the counts — the same figures as "
                "the standard monitoring report._",
                ""]
        if not f["has_data"]:
            rows.append(
                f"- The dataset holds no dated detections in this window "
                f"({f['window_h']:.0f}h requested), so no operational time can be stated.")
            return "\n".join(rows)
        rows += [
            f"- **Requested window:** {window.label()} (**{f['window_h']:.0f}h**).",
            f"- **Coverage (first-to-last detection span):** "
            f"{f['cov_start'].strftime('%Y-%m-%d %H:%M')} → "
            f"{f['cov_end'].strftime('%Y-%m-%d %H:%M')} UTC — a span of "
            f"**{f['coverage_h']:.1f}h** (not the calendar range those dates touch).",
            f"- **Proven system downtime within coverage:** **{f['downtime_h']:.1f}h**.",
            f"- **Confirmed operational:** **{f['operational_h']:.1f} of "
            f"{f['coverage_h']:.1f} hours ({f['uptime']:.0f}%)**.",
            "",
            "_Counts elsewhere in this report are raw over this effort, not divided by "
            "operational hours; with one monitoring point and no time-of-day control an "
            "events-per-hour rate would not be sound as an abundance measure._",
            "",
        ]
        rows.append(self._bng_effort_indices(window, records, f))
        return "\n".join(rows)

    def _bng_effort_indices(self, window: TimeWindow, records, f: dict) -> str:
        """Item 9 — effort-standardised INDICES of recorded camera activity (never
        abundance), computed only where operational status is known. Raw totals are
        untouched; these sit alongside them. Definitions are restated in the appendix.
        Not computed over unknown-status time: the denominators are CONFIRMED
        operational hours and days WITH data, never the requested-window span."""
        dated = len(records)
        if not f["has_data"] or f["operational_h"] <= 0 or dated == 0:
            return ("_Effort-standardised indices are not computed for this window: no "
                    "confirmed operational time over which they would be defined._")
        active_days = len({window.local_date(r.effective_time_utc) for r in records})
        cov_days = f["cov_days"] or 1
        events_per_100h = 100 * dated / f["operational_h"]
        active_per_10d = 10 * active_days / cov_days
        # Rendered as an HTML table wrapped in a keep-together block (item 7) so the whole
        # five-row table stays on one page rather than splitting across a page break.
        return "".join([
            '<div class="keep-together">',
            "<p><strong>Effort-standardised indices</strong> <em>(indices of recorded "
            "camera activity, not abundance — see the calculation definitions in Appendix "
            "B1):</em></p>",
            "<table><thead><tr><th>Index</th><th>Value</th></tr></thead><tbody>",
            "<tr><td>Alert events per 100 confirmed operational hours</td>"
            f"<td>{events_per_100h:.1f}</td></tr>",
            "<tr><td>Active days per 10 calendar days in the coverage span</td>"
            f"<td>{active_per_10d:.1f}</td></tr>",
            f"<tr><td>Confirmed operational hours</td><td>{f['operational_h']:.1f}</td></tr>",
            f"<tr><td>Confirmed downtime hours</td><td>{f['downtime_h']:.1f}</td></tr>",
            f"<tr><td>Operational coverage</td><td>{f['uptime']:.0f}%</td></tr>",
            "</tbody></table></div>",
        ])

    @staticmethod
    def _bng_season(window: TimeWindow) -> str:
        """A season phrase derived from the window's own local months, or '' if it
        spans more than one season (so nothing is over-claimed)."""
        names = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring",
                 5: "spring", 6: "summer", 7: "summer", 8: "summer", 9: "autumn",
                 10: "autumn", 11: "autumn"}
        seasons = {names[d.month] for d in window.days()}
        return f" in {seasons.pop()}" if len(seasons) == 1 else ""

    def _bng_survey_constraints(self, window: TimeWindow, records, gaps: list) -> str:
        """§5 — maps this report's real limitations to the Gain Plan's 'survey
        constraints' field. Outage dates reuse the same proven gaps and day list the
        standard downtime section shows; window length, season, and the time-of-day
        span are all derived from this window's own events."""
        rows = ["## 5. Survey constraints",
                "_The survey constraints on this monitoring evidence — the appropriate "
                "place to record them, gathered once, here._",
                "",
                "- **Single camera, single location, feeder-facing** — detections are what "
                "passed one fixed point, not a site-level or transect survey.",
                "- **Automated identifications** — unverified; see §2."]
        if gaps:
            touched: set = set()
            for start, end, *_ in sorted(gaps, key=lambda g: g[0]):
                touched |= set(self._days_touched(window, start, end))
            rows.append(
                f"- **Proven monitoring outages** — delivery stopped for part of the window "
                f"while the camera kept capturing; affected local dates: "
                f"{self._fmt_day_list(sorted(touched))}. On those dates the absence of an "
                "alert is absence of DELIVERY, not of animals — the captures are still dated "
                "and counted on their true days (see the standard report's downtime dates).")
        else:
            rows.append(
                "- **Monitoring outages** — no delivery silence in this window was evidenced "
                "as an outage; any quiet stretch is undetermined (a real lull and undetected "
                "downtime cannot be told apart).")
        rows.append(
            f"- **Short window** — a {len(window.days())}-day window{self._bng_season(window)}; "
            "not a full-year or multi-season survey.")
        hours = sorted({window.local_hour(r.effective_time_utc) for r in records})
        if hours:
            rows.append(
                f"- **Time-of-day coverage** — detections span {hours[0]:02d}:00–"
                f"{hours[-1]:02d}:00 local; no detections fall outside that span in this "
                "window, so hours beyond it are unrepresented, not evidenced as inactive.")
        return "\n".join(rows)

    def _bng_detected_species(self, window: TimeWindow, records, dated: int) -> str:
        """§6 (items 4 + 5) — renamed to 'Automated species classification results' so no
        record is asserted as a definitive presence, and rendered as a classification-
        VALIDATION table (per-label BioCLIP confirmed/unconfirmed, human-reviewed, final
        status). Human-review data does not exist, so per-row it shows 'Not available' and
        every final status is 'Provisional'. The events-not-animals warning is retained."""
        rows = ["## 6. Automated species classification results",
                "_These are **automated, provisional** classifications, not confirmed "
                "presences. **No detection count is a biodiversity value**, and counts are "
                "alert EVENTS (one alert-rule × image match) — not animals, not abundance, "
                "and not a community or biodiversity assessment. Scientific names are the "
                "model's labels pending image review._",
                ""]
        if dated == 0:
            rows.append("_No classifications in this window to report._")
            return "\n".join(rows)
        by_key: dict[str, list] = {}
        for r in records:
            by_key.setdefault(r.canonical_binomial, []).append(r)
        ordered = sorted(by_key.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        rows.append(f"Automated classification labels (N = {dated} alert events):")
        rows.append("")
        rows.append("| Species label | Alert events | Share | Active days | "
                    "BioCLIP corroborated | Not corroborated | Human-reviewed | Final status |")
        rows.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
        tot_corr = 0
        for _k, evs in ordered:
            label, _reserved = self._key_label(evs[0])
            active = len({window.local_date(e.effective_time_utc) for e in evs})
            corroborated = sum(1 for e in evs if e.agreement_flag == "agree")
            tot_corr += corroborated
            not_corr = len(evs) - corroborated
            rows.append(
                f"| {label} | {len(evs)} | {100 * len(evs) / dated:.1f}% | {active} | "
                f"{corroborated} | {not_corr} | Not available | Provisional |")
        # Total row: additive columns summed; per-species active days are NOT additive
        # (a day may be active for several labels), so that cell is left blank.
        rows.append(f"| **Total (N)** | **{dated}** | 100% | — | **{tot_corr}** | "
                    f"**{dated - tot_corr}** | — | — |")
        rows.append("")
        non_top = dated - len(ordered[0][1])
        if non_top:
            rows.append(
                f"_Non-dominant counts ({self._n(non_top, 'event')} across "
                f"{len(ordered) - 1} smaller labels) are **provisional** — with n this "
                "small a few misclassifications visibly change the shares (see §2). "
                "'Not available' human-review reflects that no human-review record exists "
                "in the system; adding one would populate that column and the final "
                "status._")
        return "\n".join(rows)

    @staticmethod
    def _and_list(items) -> str:
        """'a', 'a and b', or 'a, b and c' — for inline species lists."""
        items = list(items)
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        return ", ".join(items[:-1]) + " and " + items[-1]

    def _bng_ecological_interpretation(self, window: TimeWindow, records, dated: int,
                                       op_first, op_last) -> str:
        """Interpretation the detection data SUPPORTS — dominance, recurrence, and a
        management flag — stated confidently where earned and flagged only where the
        evidence is thin. Every figure derives from this window's own events; the only
        non-figure input is the existing, documented `_MANAGEMENT_RELEVANCE` reference
        table (shared with the all-species review section), used to identify which
        detected species are management-relevant. It never asserts an ID is correct.
        Active-day shares are read against the data-coverage span (days WITH data), not
        the requested window, whose extra days are unknown-status, not species-absent."""
        rows = ["## Interpretation of automated classifications",
                "_Inferences the detection data supports, for the monitoring operator — "
                "read as automated-classification ACTIVITY at one monitored point, not "
                "confirmed presence (§6)._",
                ""]
        if dated == 0:
            rows.append("_No detections in this window to interpret._")
            return "\n".join(rows)

        by_key: dict[str, list] = {}
        for r in records:
            by_key.setdefault(r.canonical_binomial, []).append(r)
        ordered = sorted(by_key.items(), key=lambda kv: (-len(kv[1]), kv[0]))

        def label(evs) -> str:
            return self._key_label(evs[0])[0]

        def active_days(evs) -> int:
            return len({window.local_date(e.effective_time_utc) for e in evs})

        # Dominant — plain, with a descriptor DERIVED from the share (no hedge). The
        # active-day denominator is the coverage span (days WITH data), matching the §6
        # table — never the wider requested window.
        cov_days = self._coverage_span_days(window, op_first, op_last)
        _dk, dom = ordered[0]
        dom_pct = 100 * len(dom) / dated
        descriptor = ("the overwhelming majority of" if dom_pct >= 90
                      else "the majority of" if dom_pct >= 50
                      else "the largest single share of")
        rows.append(
            f"- **Dominant automated classification — {label(dom)}.** {len(dom)} of {dated} "
            f"events ({dom_pct:.1f}%) on {active_days(dom)} of {cov_days} days with data: "
            f"the upstream classifier assigned this label to {descriptor} the alert events "
            "in this window.")

        non_dom = ordered[1:]
        recurring = [(k, evs) for k, evs in non_dom if active_days(evs) >= 2]
        # Management-relevant AND thinly evidenced (not already covered as recurring):
        # the single-flag case the review table exists to surface.
        flagged = [(k, evs) for k, evs in non_dom
                   if k in _MANAGEMENT_RELEVANCE and active_days(evs) < 2]

        # Infrequent but RECURRING — stated plainly, by active-day counts.
        if recurring:
            parts = [f"{label(evs)} on {active_days(evs)} days "
                     f"({self._n(len(evs), 'event')})" for _k, evs in recurring]
            rows.append(
                f"- **Infrequent but recurring — {self._and_list(parts)}.** Low counts, "
                "but spread across multiple distinct days rather than single episodes: "
                "recurring, low-frequency automated classifications across multiple days.")

        # Management-relevant flag — one detection, one flag (not a blanket hedge). The
        # KIND of concern is read from the reference tag; the number of events and the
        # unverified count are derived from the events.
        for k, evs in flagged:
            tag = _MANAGEMENT_RELEVANCE.get(k, "")
            if "predation" in tag or "predator" in tag:
                concern = "a potential predator at a feeder"
            elif "invasive" in tag or "non-native" in tag:
                concern = "a non-native/invasive species"
            else:
                concern = "management-relevant at a feeder"
            notconf = sum(1 for e in evs if _not_corroborated(e))
            unver = " with an unverified identification" if notconf else ""
            lead = ("the most management-relevant detection in this window"
                    if len(flagged) == 1 else "a management-relevant detection")
            rows.append(
                f"- **Management-relevant, provisional — {label(evs)}.** {lead.capitalize()} "
                f"— {concern} — but it rests on only {self._n(len(evs), 'event')}{unver} "
                "(§2), so treat it as provisional pending image review.")

        rows.append("")
        rows.append("_These describe automated classifications and their recorded activity "
                    "at a single monitored point — not confirmed presence, abundance, or "
                    "wider site biodiversity._")
        return "\n".join(rows)

    def _bng_detection_event_definition(self) -> str:
        """Item 8 — a methodology subsection defining exactly what one alert event is,
        and stating plainly that events are NOT grouped into independent visits (the code
        performs no such grouping), so repeats may be the same animal or visit."""
        return "\n".join([
            "## Methodology — detection-event definition",
            "",
            "One **alert event** is a single upstream alert: one (alert-rule × image) "
            "match delivered by the detector for one triggering image. It is a *record of "
            "a classification event*, not a count of animals: one animal can raise many "
            "alert events, and two animals in one frame raise a single event.",
            "",
            "Temporally adjacent images may therefore represent the **same animal or "
            "visit**. **Alert events have not been grouped into independent visits.** "
            "Repeated alerts may therefore represent the same animal or visit, and the "
            "totals in this report are raw alert-event counts, not counts of independent "
            "encounters.",
        ])

    def _bng_evidence_limitations(self, window: TimeWindow, records, gaps: list,
                                  quality: dict) -> str:
        """Item 13 — ONE prominent, consolidated limitations section. Derives the
        variable parts (downtime presence, window length/season, time-of-day span,
        corroboration %) from the data; the short reminders elsewhere point here rather
        than repeating full paragraphs."""
        n_days = len(window.days())
        season = self._bng_season(window)
        hours = sorted({window.local_hour(r.effective_time_utc) for r in records})
        tod = (f"{hours[0]:02d}:00–{hours[-1]:02d}:00 local" if hours else "not established")
        downtime = ("proven system downtime occurred within the window (see the appendix "
                    "for dates)" if gaps else "no proven downtime was evidenced in this window")
        return "\n".join([
            "## Evidence limitations",
            "_The limitations on this evidence, consolidated. Short reminders elsewhere "
            "refer back here._",
            "",
            "- **One fixed monitoring point** — a single camera at one location; no wider-"
            "site coverage.",
            "- **Feeder-facing** — detections are what passed one feeder, not a site-level "
            "or transect survey.",
            "- **Automated, unverified identifications** — labels are model outputs; no "
            "competent-ecologist verification.",
            f"- **Limited independent corroboration** — BioCLIP did not corroborate "
            f"{quality['unconfirmed']} of {quality['total']} labels "
            f"({quality['pct_unconf']:.0f}%); results are provisional pending image review.",
            f"- **System downtime** — {downtime}; absence during downtime is not evidence "
            "of animal absence.",
            f"- **Short monitoring window** — {n_days} days{season}; not a full-year or "
            "multi-season survey.",
            f"- **Incomplete time-of-day representation** — detections span {tod}; hours "
            "beyond that span are unrepresented, not evidenced as inactive.",
            f"- **{COUNT_CAVEAT_CLAIM}**{COUNT_CAVEAT_REST}",
            "- **Possible repeated detections of the same visit** — events are not grouped "
            "into independent encounters (see the detection-event definition).",
            "- **No habitat-condition assessment** — a camera does not assess habitat "
            "condition.",
            "- **No biodiversity-unit calculation** — this is not a statutory Biodiversity "
            "Gain Plan.",
        ])

    def _bng_management_considerations(self, window: TimeWindow, records) -> str:
        """Item 14 — recommended monitoring actions as an ACTION TABLE (action, evidence/
        reason, trigger, responsible role, priority, status). Priorities follow documented
        rules below; no named person is assigned (none exists in the data — the role is
        the generic 'Monitoring operator'). The predator-mitigation row is gated and only
        appears when a management-relevant predator was detected."""
        by_key: dict[str, list] = {}
        for r in records:
            by_key.setdefault(r.canonical_binomial, []).append(r)
        ordered = sorted(by_key.items(), key=lambda kv: (-len(kv[1]), kv[0]))

        def common(evs) -> str:
            return evs[0].display_common_name or self._key_label(evs[0])[0]

        non_dom = ordered[1:]
        role = "Monitoring operator"
        # Priority rule (documented, deterministic): actions that address the report's
        # stated provisional-quality risk (unverified IDs, model disagreement) or a
        # management-relevant predator are High; recording missing required metadata is
        # High; routine continuation/comparison is Medium; conditional habitat work is Low.
        rows = ["## Recommended monitoring actions",
                "_Actionable next steps. Priority follows documented rules (High = addresses "
                "the provisional-quality risk or missing required metadata; Medium = routine "
                "continuation; Low = conditional). No named person is assigned; the "
                "responsible role is the monitoring operator._",
                "",
                "| Recommended action | Evidence / reason | Trigger | Responsible role | "
                "Priority | Status |",
                "| --- | --- | --- | --- | --- | --- |"]

        def row(action, reason, trigger, priority):
            rows.append(f"| {action} | {reason} | {trigger} | {role} | {priority} | Open |")

        if non_dom:
            names = self._and_list(common(evs) for _k, evs in non_dom)
            row("Human-review the non-dominant detections",
                f"Provisional low-count labels ({names})", "Before acting on any label",
                "High")
        row("Record the camera OS grid reference",
            "Grid reference is required and not recorded", "Before the next submission",
            "High")
        row("Human-review labels not corroborated by BioCLIP",
            "BioCLIP did not corroborate most upstream labels",
            "Before relying on any species-level result", "High")
        row("Continue monitoring across seasons",
            "This is a short single-season window", "Ongoing", "Medium")
        row("Compare equivalent future windows against this baseline",
            "A baseline is established (§3)", "At each future monitoring window", "Medium")
        row("Inspect the feeder and camera after unexpected composition changes",
            "Composition change may be a system artefact, not ecology",
            "On a marked change in the label mix", "Medium")
        row("Undertake a habitat survey where habitat condition is required",
            "A camera does not assess habitat condition", "If condition data is needed",
            "Low")

        predators = [common(evs) for k, evs in ordered if k in _MANAGEMENT_RELEVANCE
                     and ("predation" in _MANAGEMENT_RELEVANCE[k]
                          or "predator" in _MANAGEMENT_RELEVANCE[k])]
        if predators:
            row(f"Review repeated {self._and_list(predators)} classifications before "
                "considering mitigation",
                "Provisional predator classification at a feeder",
                "Only after confirmed ID and repeated records", "High")
        return "\n".join(rows)

    @staticmethod
    def _bng_data_sharing() -> str:
        """§7 — a short data-sharing note mirroring the Gain Plan's §10. Statement only."""
        return (
            "## 7. Data sharing\n\n"
            "This monitoring data could, in principle, be shared with the **Local "
            "Environmental Records Centre (LERC)** for the area. However, the species "
            "labels in this report are **automated and unverified**, and are **not ready "
            "for external ecological recording** as they stand: any submission would first "
            "require **human verification of the identifications and appropriate "
            "preparation** by a competent recorder. This is a statement of possibility "
            "only; this report shares no data and takes no action.")

    @staticmethod
    def _key_label(sample) -> tuple[str, bool]:
        """(display label, is_reserved_key) for a canonical key, from a sample
        record. Reserved ``nonbio:``/``unmapped:`` keys are labelled as what they
        are, never dressed up as species."""
        key = sample.canonical_binomial or "(no key)"
        if key.startswith("nonbio:"):
            return f"{key.split(':', 1)[1]} (non-biological class)", True
        if key.startswith("unmapped:"):
            return f"{key.split(':', 1)[1]} (unmapped token)", True
        common = sample.display_common_name
        return (f"{common} (*{key}*)" if common else f"*{key}*"), False

    def _species_breakdown(self, window: TimeWindow, records, dated: int) -> str:
        """Composition of the window by species — the genuinely useful part of an
        all-species view, and the deterministic replacement for per-species
        highlights. Every dated in-window event is attributed to exactly one key
        (mapped binomial, ``nonbio:``, or ``unmapped:``), so the rows sum to the
        table's dated total — no silent drops."""
        by_key: dict[str, list] = {}
        for r in records:
            by_key.setdefault(r.canonical_binomial, []).append(r)

        rows = ["## Species breakdown",
                "_Every dated event in the window, grouped by species key. The counts below "
                f"sum to the day-by-day table's dated total ({dated}). Alert events, not "
                f"animals; {window.day_basis_label()}._"]
        rows.append("")
        rows.append("| Species / key | Alert events | Active days |")
        rows.append("| --- | ---: | ---: |")

        ordered = sorted(by_key.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        reserved = [evs for _key, evs in ordered if self._key_label(evs[0])[1]]
        for _key, evs in ordered:
            label, _is_reserved = self._key_label(evs[0])
            active = len({window.local_date(e.effective_time_utc) for e in evs})
            rows.append(f"| {label} | {len(evs)} | {active} |")
        rows.append(f"| **Total** | **{dated}** | |")

        # The deliberate difference from per-species reports, stated UNCONDITIONALLY
        # so a reader comparing views understands the methodology even when no
        # reserved keys happen to be present (the user's explicit ask): here the
        # total counts every key; per-species reports exclude unmapped/non-biological
        # and disclose them separately.
        rows.append("")
        note = ("_This all-species total counts **every** key. If any `unmapped:` "
                "(unrecognised upstream tokens) or non-biological classes (e.g. Person, Car) "
                "are present, they are included here — whereas a single-species report "
                "**excludes** them from its count and discloses them separately. So a "
                "per-species figure need not sum to this total without them.")
        if reserved:
            n_reserved = sum(len(evs) for evs in reserved)
            note += (f" This window contains {self._n(n_reserved, 'such event')}, listed "
                     "above and counted in the total.")
        else:
            note += " This window contains none, so every row here is a mapped species."
        rows.append(note + "_")
        return "\n".join(rows)

    def _crosscheck_summary_line(self, records) -> str:
        """One line stating the BioCLIP cross-check OUTCOME across the window — no
        per-detection rationales (those live in the per-species reports), but never
        silent.

        Omitting this from the all-species summary would let a reader conclude
        'nothing to flag' when the cross-check in fact failed on most of the
        dataset — the most important quality fact about it. Uncertainty-as-absence,
        so it is stated explicitly. Honours REPORT_INCLUDE_CROSSCHECK exactly as
        the per-species flags section does: hidden means disclosed-as-hidden, never
        vanished."""
        n = len(records)
        rows = ["## Cross-check summary"]
        if n == 0:
            rows.append("_No detections in this window to cross-check._")
            return "\n".join(rows)

        unconfirmed = [r for r in records if _not_corroborated(r)]
        if not unconfirmed:
            rows.append("_The BioCLIP taxonomic cross-check corroborated the upstream label on "
                        f"all {n} detection(s) in this window._")
            return "\n".join(rows)

        if not self._include_crosscheck:
            rows.append(
                "_BioCLIP cross-check display is off (REPORT_INCLUDE_CROSSCHECK=false): "
                f"{len(unconfirmed)} of {n} cross-check flag(s) hidden here but still stored "
                "and available in the data._")
            return "\n".join(rows)

        disagree = [r for r in unconfirmed if r.agreement_flag == "disagree"]
        not_eval = [r for r in unconfirmed if r.agreement_flag == "not_evaluable"]
        pct = 100 * len(unconfirmed) / n
        rows.append(
            f"**The cross-check did not corroborate {len(unconfirmed)} of {n} detections "
            f"({pct:.0f}%)** — BioCLIP's own top-1 identification was not the upstream "
            f"species ({len(disagree)} disagree"
            + (f", {len(not_eval)} not comparable" if not_eval else "")
            + "). This is a dataset-wide quality fact; the per-detection rationales are in "
              "the individual per-species reports.")
        # Say HOW FAR apart the disagreements were. A near miss inside the same genus
        # and a read of the grass behind the feeder are both 'disagree', and a report
        # that did not separate them would hide the more useful of the two findings.
        breakdown = _distance_breakdown(disagree)
        if breakdown:
            rows.append("")
            rows.append("Taxonomic distance of the disagreements — how far BioCLIP's "
                        "identification sat from the upstream label:")
            rows.append("")
            rows.append("| BioCLIP's read vs the upstream label | Events "
                        "| Share of disagreements |")
            rows.append("| --- | ---: | ---: |")
            for _key, phrase, count in breakdown:
                rows.append(f"| {phrase} | {count} | {100 * count / len(disagree):.0f}% |")
        if not_eval:
            rows.append("")
            rows.append(
                f"_{len(not_eval)} further detection(s) could not be cross-checked at all "
                "(a class outside BioCLIP's taxonomy, or a BioCLIP run that produced no "
                "result). They are excluded from the agree/disagree denominator rather "
                "than counted as disagreements._")
        return "\n".join(rows)

    def _all_species_downtime_section(self, window: TimeWindow, gaps: list) -> str:
        """Coverage as SYSTEM-WIDE downtime only (the collapsed classification).

        In an all-species view every silence is, by definition, a silence across
        the whole mailbox — nothing of any species arrived — so the per-species
        four-way split (no_data / absent / mixed / unconfirmed) collapses to two:
        a silence is either proven downtime (back-dated captures recovered) or
        undetermined. 'Absent while the system was confirmed up' has no reference
        point here and is not claimed. Recovery counts are the dataset-wide totals,
        which for an all-species report is the correct scope, not a misattribution."""
        proven = sorted(gaps, key=lambda g: -g[2])
        rows = ["## System downtime"]
        if not proven:
            rows.append(
                "No delivery silence in this window was followed by back-dated captures, "
                "the only evidence that distinguishes an outage from a genuinely quiet "
                "period. Any quiet stretch here is therefore **undetermined** — a real "
                "lull and undetected downtime cannot be told apart from this data.")
            return "\n".join(rows)

        total_recovered = sum(n for *_, n in proven)
        rows.append(
            f"**{self._n(len(proven), 'period')} of proven system downtime** — no alert was "
            "delivered for ANY species while the camera kept capturing, shown by "
            f"{self._n(total_recovered, 'event')} captured during the silence and delivered "
            "only after it ended. During these periods **no species' presence is "
            "verifiable**: this is absence of data, not absence of animals.")
        rows.append("")
        rows.append("_In an all-species view a silence is either proven downtime (below) or "
                    "undetermined; the per-species distinction 'absent while the system was "
                    "confirmed operational' cannot apply, because there is no other species "
                    "left to confirm the system was up._")
        rows.append("")
        rows.append("| Silence (UTC) | Duration | All-species events recovered afterwards |")
        rows.append("| --- | ---: | ---: |")
        for start, end, gap, n in proven[:_MAX_GAPS_SHOWN]:
            rows.append(f"| {start.strftime('%d %b %H:%M')} → {end.strftime('%d %b %H:%M')} "
                        f"| {gap / 3600:.1f}h | {n} |")
        if len(proven) > _MAX_GAPS_SHOWN:
            rows.append(f"\n_Showing the {_MAX_GAPS_SHOWN} longest of {len(proven)} proven "
                        "downtime periods, longest first._")
        return "\n".join(rows)

    def _downtime_dates_section(self, window: TimeWindow, gaps: list) -> str:
        """Dataset-wide: the LOCAL calendar dates each proven outage touches, framed
        so a reader never reads an affected date as missing or empty. Restates the
        System-downtime records deliberately (the user's ask) with the dates made
        obvious. Derived from the same `gaps`; nothing hardcoded. Appears identically
        on every report type (it is a system property)."""
        if not gaps:
            return ""
        proven = sorted(gaps, key=lambda g: g[0])
        rows = [
            "## System downtime — dates affected",
            "**During every outage below the camera kept capturing images. Those images "
            "are still dated and counted on their true capture days — so an outage means "
            "alerts were DELAYED or not delivered, never \"no images that day\" and never "
            "\"no animals that day.\" This is absence of delivery, not of data or animals; "
            "the affected dates are not missing and not zero — they carry real captured "
            "events, just delivered late.**",
            "",
            "| Outage (UTC) | Local dates affected (Europe/London) |",
            "| --- | --- |",
        ]
        for start, end, *_ in proven:
            touched = self._days_touched(window, start, end)
            dates = self._fmt_day_list(touched)
            rows.append(f"| {start.strftime('%d %b %H:%M')} → {end.strftime('%d %b %H:%M')} "
                        f"| {dates} |")
        rows.append("")
        rows.append("_These are the same proven outages listed under 'System downtime', "
                    "re-framed by the calendar dates they touch. Events captured during them "
                    "appear on those dates in the day-by-day table, delivered late — see "
                    "'How these events were dated'._")
        return "\n".join(rows)

    def _species_composition_section(self, window: TimeWindow, records, dated: int) -> str:
        """Composition of alert EVENTS by species (all-species report only): a pie +
        a share-of-N table. COMPOSITION ONLY — no diversity index, no 'biodiversity'
        claim (with one camera and n=17 non-dominant events an index would look
        authoritative and mean nothing). Counts derive from the report's own dated
        event set; the ID-confidence caveat cites the report's own cross-check."""
        by_key: dict[str, list] = {}
        for r in records:
            by_key.setdefault(r.canonical_binomial, []).append(r)
        if not by_key or dated == 0:
            return ("## Species composition of alert events\n\n_No dated events in this "
                    "window to compose._")

        ordered = sorted(by_key.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        # Assign fixed palette in count order; fold any beyond the palette into Other.
        slices, table = [], []
        for i, (_k, evs) in enumerate(ordered):
            label, _reserved = self._key_label(evs[0])
            common = evs[0].display_common_name or _k
            table.append((label, len(evs)))
            if i < len(_SPECIES_PALETTE):
                slices.append((common, len(evs), _SPECIES_PALETTE[i]))
        if len(ordered) > len(_SPECIES_PALETTE):
            folded = sum(len(evs) for _k, evs in ordered[len(_SPECIES_PALETTE):])
            slices.append((f"other ({len(ordered) - len(_SPECIES_PALETTE)} species)",
                           folded, _OTHER_COLOUR))

        rows = ["## Species composition of alert events",
                "_Composition of detection EVENTS, not of animals or abundance — one event "
                "is one (alert-rule × image) match; repeat visits and multiple animals in "
                f"frame are not resolved (Decision 4). N = {dated} dated events._",
                "",
                self._hold(self._pie_svg(slices, dated, subject="species", stack_legend=True)),
                ""]

        # Caveat 2: automated, mostly-unconfirmed IDs — cite the report's own cross-check.
        not_conf = sum(1 for r in records if _not_corroborated(r))
        top = len(ordered[0][1])
        non_top = dated - top
        k_small = len(ordered) - 1
        if self._include_crosscheck:
            id_note = (f"the BioCLIP cross-check did not confirm {not_conf} of {dated} "
                       f"({100 * not_conf / dated:.0f}%) — see 'Data-quality flags'")
        else:
            id_note = ("cross-check figures are hidden (REPORT_INCLUDE_CROSSCHECK=false) but "
                       "still stored")
        if non_top:
            rows.append(
                f"_**Species IDs are automated and mostly unconfirmed:** {id_note}. Treat the "
                f"non-dominant counts ({self._n(non_top, 'event')} across {k_small} smaller "
                "species) as **PROVISIONAL** — with n this small, a few misclassifications "
                "visibly change the shares._")
        else:
            rows.append(f"_**Species IDs are automated and mostly unconfirmed:** {id_note}._")

        # Caveat 1 (events not animals) is in the intro; table then caveat 3.
        rows.append("")
        rows.append("| Species | Alert events | Share of N |")
        rows.append("| --- | ---: | ---: |")
        for label, c in table:
            rows.append(f"| {label} | {c} | {100 * c / dated:.1f}% |")
        rows.append(f"| **Total (N)** | **{dated}** | 100% |")
        rows.append("")
        rows.append("_Single camera, single location, feeder-facing: this measures what was "
                    "detected at one point, not the site's community composition._")
        return "\n".join(rows)

    def _what_supports_line(self, window: TimeWindow, records, op_first, op_last) -> str:
        """The one place the report says what it DOES support (item 1). About the
        DOMINANT species in the report's own event set; strength and cadence scale
        with the evidence so it is never boosterish. No caveats here — they live
        elsewhere; this is the single positive-findings sentence."""
        if not records:
            return ""
        by_key: dict[str, list] = {}
        for r in records:
            by_key.setdefault(r.canonical_binomial, []).append(r)
        dom_key, dom_recs = max(by_key.items(), key=lambda kv: len(kv[1]))
        name = dom_recs[0].display_common_name or dom_key
        dom_days = Counter(window.local_date(r.effective_time_utc) for r in dom_recs)
        active = len(dom_days)
        first = min(dom_days)
        last = max(dom_days)
        # Busiest span: the min–max date of the dominant's three busiest days.
        top = sorted(dom_days.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        busy = sorted(d for d, _ in top)
        b0, b1 = busy[0], busy[-1]
        if b0 == b1:
            busy_span = self._fmt_day(b0.isoformat())
        elif (b0.year, b0.month) == (b1.year, b1.month):
            busy_span = f"{b0.day}–{b1.day} {b0:%b %Y}"
        else:
            busy_span = f"{b0.day} {b0:%b}–{b1.day} {b1:%b %Y}"

        # Coverage days (operational span) to scale the cadence claim honestly.
        cov_days = 0
        if op_first and op_last:
            cov_days = len(self._days_touched(window, max(window.start_utc, op_first),
                                              min(window.end_utc, op_last)))
        frac = active / cov_days if cov_days else 0.0
        n_dom = len(dom_recs)
        if n_dom >= 50 and frac >= 0.8:
            strength, cadence = "strong evidence of", "sustained, near-daily"
        elif n_dom >= 10 and frac >= 0.4:
            strength, cadence = "evidence of", "recurring"
        else:
            strength, cadence = "limited evidence of", "occasional"

        k = len(by_key) - 1
        others = f"; {k} other species also detected (unconfirmed)" if k else ""
        return (f"**What this supports:** {strength} {cadence} {name} activity at a single "
                f"feeder-facing camera — recorded on {active} of {cov_days} days with data "
                f"({first.isoformat()} to {last.isoformat()}), busiest {busy_span}{others}.")

    def _time_of_day_section(self, window: TimeWindow, records) -> str:
        """Hour-of-day (local) distribution of dated events (item 3, every report).
        A 24-bar histogram + a 3-hour-bucket table that sums to N, and the one
        honest read it supports (diurnal, daytime-only capture). Derived from the
        report's own effective times; dataset-wide in that it shows on every report."""
        dated = [r for r in records if r.effective_time_utc is not None]
        if not dated:
            return ""
        hours = Counter(window.local_hour(r.effective_time_utc) for r in dated)
        counts = [hours.get(h, 0) for h in range(24)]
        n = sum(counts)
        active_hours = [h for h in range(24) if counts[h]]
        peak = max(range(24), key=lambda h: counts[h])
        lo, hi = active_hours[0], active_hours[-1]

        rows = ["## Time-of-day distribution",
                f"_Dated alert events by hour of capture, local time ({window.tz}). "
                f"N = {n}. Events, not animals._",
                "",
                self._hold(self._hour_histogram_svg(counts)),
                ""]
        rows.append(
            f"Detections concentrate in the {lo:02d}:00–{hi:02d}:59 window, peaking at "
            f"{peak:02d}:00, with none overnight — consistent with a diurnal species and "
            "daytime-only capture.")
        rows.append("")
        rows.append("| Hours (local) | Alert events |")
        rows.append("| --- | ---: |")
        for start in range(0, 24, 3):
            bucket = sum(counts[start:start + 3])
            rows.append(f"| {start:02d}:00–{start + 2:02d}:59 | {bucket} |")
        rows.append(f"| **Total** | **{n}** |")
        rows.append("")
        rows.append("_This is detection TIMING under whatever the camera's trigger/schedule "
                    "was — not a behavioural rate, and still uncorrected for effort-by-hour "
                    "(the system was not equally up every hour). Events, not animals._")
        return "\n".join(rows)

    @staticmethod
    def _hour_histogram_svg(counts: list[int]) -> str:
        """24-bar hour-of-day histogram, same hand-built inline-SVG style as the
        day-by-day chart (green bars, 0/max gridline, sparse hour ticks)."""
        n = 24
        maxc = max(counts) if counts else 0
        pad_l, pad_r, pad_t, pad_b = 30, 12, 16, 42
        plot_h, col_w, bar_w = 130, 26, 18
        base_y = pad_t + plot_h
        width = pad_l + n * col_w + pad_r
        height = pad_t + plot_h + pad_b
        scale = (plot_h / maxc) if maxc > 0 else 0.0
        green, axis, muted = "#1b5e3f", "#cbd6ce", "#4a4a4a"
        parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
                 f'aria-label="Histogram of alert events by local hour 0 to 23, peaking at '
                 f'{maxc}.">',
                 f'<title>Alert events by local hour of day</title>',
                 f'<line x1="{pad_l}" y1="{base_y}" x2="{width - pad_r}" y2="{base_y}" '
                 f'stroke="{axis}" stroke-width="1"></line>',
                 f'<text x="{pad_l - 6}" y="{base_y + 4}" fill="{muted}" font-size="10" '
                 'text-anchor="end">0</text>']
        if maxc > 0:
            parts.append(f'<line x1="{pad_l}" y1="{pad_t}" x2="{width - pad_r}" y2="{pad_t}" '
                         f'stroke="{axis}" stroke-width="1" stroke-dasharray="2 3"></line>')
            parts.append(f'<text x="{pad_l - 6}" y="{pad_t + 4}" fill="{muted}" font-size="10" '
                         f'text-anchor="end">{maxc}</text>')
        for h, c in enumerate(counts):
            if c > 0:
                bh = c * scale
                x = pad_l + h * col_w + (col_w - bar_w) / 2
                parts.append(f'<rect x="{x:.1f}" y="{base_y - bh:.1f}" width="{bar_w}" '
                             f'height="{bh:.1f}" rx="2" fill="{green}"></rect>')
            if h % 3 == 0:
                cx = pad_l + h * col_w + col_w / 2
                parts.append(f'<text x="{cx:.1f}" y="{base_y + 16}" fill="{muted}" '
                             f'font-size="9" text-anchor="middle">{h:02d}</text>')
        parts.append("</svg>")
        return "".join(parts)

    def _non_target_review_section(self, window: TimeWindow, records) -> str:
        """Route the non-dominant detections to HUMAN review (item 4, all-species
        only) with per-event local timestamps so the images can be pulled. Never
        asserts the automated IDs are correct — the whole point is verification.
        Management-relevant species (predators, invasives) are flagged first via
        the small _MANAGEMENT_RELEVANCE reference table."""
        by_key: dict[str, list] = {}
        for r in records:
            by_key.setdefault(r.canonical_binomial, []).append(r)
        if len(by_key) < 2:
            return ""
        dom_key = max(by_key.items(), key=lambda kv: len(kv[1]))[0]
        others = {k: evs for k, evs in by_key.items() if k != dom_key}

        not_conf = sum(1 for r in records if _not_corroborated(r))
        pct = 100 * not_conf / len(records) if records else 0
        rows = ["## Non-target detections — for review",
                f"**These species IDs are automated and unconfirmed (the cross-check did not "
                f"confirm {not_conf} of {len(records)}, {pct:.0f}%; see 'Data-quality flags'). "
                "Management-relevant detections below — a predator or a non-native species — "
                "must be checked against the actual images before any predator- or "
                "invasive-management decision. Do not act on the automated label alone.**"]

        # Flagged (management-relevant) species first, then the rest by count.
        def sort_key(item):
            k, evs = item
            return (0 if k in _MANAGEMENT_RELEVANCE else 1, -len(evs), k)

        for k, evs in sorted(others.items(), key=sort_key):
            label, _reserved = self._key_label(evs[0])
            tag = _MANAGEMENT_RELEVANCE.get(k)
            head = f"**{label}** — {self._n(len(evs), 'event')}"
            if tag:
                head += f" · ⚑ {tag}"
            rows.append("")
            rows.append(head + ":")
            for e in sorted(evs, key=lambda r: r.effective_time_utc or datetime.min.replace(tzinfo=timezone.utc)):
                if e.effective_time_utc is None:
                    rows.append("- (no usable timestamp)")
                else:
                    local = e.effective_time_utc.astimezone(window._zone)
                    rows.append(f"- {local.strftime('%d %b %Y %H:%M')} (local)")
        return "\n".join(rows)

    # ------------------------------------------------------------- rendering
    def _render(self, term, key, display, window, records) -> str:
        name = display or key
        is_species = is_binomial_key(key)

        # One reconciled set of figures, computed ONCE and shared by the header,
        # the table, the undated disclosure, and the LLM facts — so they cannot
        # disagree. total = dated (placed on days) + undated (no usable time).
        dated = len(records)                                  # find() = dated, in-window
        undated = self._retrieval.count_undated(key)          # no usable timestamp
        total = dated + undated
        by_day = Counter(window.local_date(r.effective_time_utc).isoformat() for r in records)

        # Internal-consistency guard: every dated event must be accounted for by
        # a row in the day-by-day table (shown == dated), and total must add up.
        # If the arithmetic ever fails, do NOT hand incoherent facts to the LLM to
        # smooth over (the seam a live run just exposed) — surface it instead.
        shown = sum(by_day.get(d.isoformat(), 0) for d in window.days())
        consistent = shown == dated and total == dated + undated

        # Proven outages and OPERATIONAL coverage — computed ONCE and shared by the
        # narrative (trend caveat), the day-by-day table/chart (no-data marking),
        # the monitoring-effort line, and the zero-day breakdown, so no two of them
        # can disagree about downtime or coverage. Coverage is the SEND-time span
        # (last confirmed-operational point), not capture time — a delivered alert
        # proves the system was up even when its capture is dated earlier.
        gaps = self._proven_gaps(window)
        op_first, op_last = self._retrieval.send_time_span()

        lines: list[str] = []
        heading = f"# Detection report — {name}"
        if is_species and display:
            heading += f" (*{key}*)"
        elif not is_species:
            heading += "  \n_Non-species class — the BioCLIP taxonomic cross-check does not apply._"
        lines.append(heading)
        lines.append(self._totals_line(window, total, dated, undated))

        if consistent:
            lines.append(self._narrative(name, key, window, total, dated, undated, by_day, gaps))
        else:
            logger.error("report_figures_inconsistent",
                         extra={"key": key, "dated": dated, "shown": shown, "undated": undated})
            lines.append("## Summary\n\n_Internal figures did not reconcile "
                         f"(dated={dated}, shown in table={shown}, undated={undated}); "
                         "narrative withheld rather than describe inconsistent data._")

        # Period-over-period comparison (Change 2). Computed ONLY over reconciled
        # figures; the current-window figure IS `dated` (the table's dated total),
        # so the comparison and the table are provably consistent. Deliberately NOT
        # fed to the LLM — see `_compute_comparison` / `_narrative`.
        comparison = None
        if consistent and self._include_comparison:
            comparison = self._compute_comparison(term, key, name, window, dated)

        lines.append(self._what_supports_line(window, records, op_first, op_last))
        lines.append(self._counts_section(window, by_day, dated, op_first, op_last))
        lines.append(self._monitoring_effort_section(window, gaps, op_first, op_last))
        lines.append(self._capture_time_disclosure(window, records))
        lines.append(self._time_of_day_section(window, records))
        if comparison is not None:
            lines.append(self._comparison_section(name, window, comparison))
        lines.append(self._undated_disclosure(name, total, dated, undated))
        lines.append(self._highlights_section(window, key, name, records, by_day, undated,
                                              gaps, op_first, op_last))
        lines.append(self._downtime_dates_section(window, gaps))   # dataset-wide, every report
        lines.append(self._vlm_evidence_section(window, records))
        lines.append(self._flags_section(window, records, is_species))
        if any(r.weather_category for r in records):     # only once weather is backfilled
            lines.append(self._weather_section(window, records, name, per_species=is_species))
        lines.append(self._unmapped_disclosure(window))
        lines.append(self._honesty_section(total, dated, window, records, comparison))
        return "\n\n".join(p for p in lines if p)

    @staticmethod
    def _totals_line(window: TimeWindow, total: int, dated: int, undated: int) -> str:
        line = f"**Window:** {window.label()}  \n**Alert events:** {total}"
        if undated:
            line += f" — {dated} placed on days below, {undated} without a usable timestamp"
        return line

    def _undated_disclosure(self, name: str, total: int, dated: int, undated: int) -> str:
        """Conditional: rows of THIS species with no usable timestamp. They are in
        the total but cannot be placed on a day, so they are excluded from the
        table — disclosed here, and reconciled with the total (uncertainty, not
        absence)."""
        if not undated:
            return ""
        return ("## Detections without a timestamp\n\n"
                f"**{undated} of the {total} {name} detection(s) had no usable timestamp** "
                "(the alert-send time was missing, so they cannot be placed on a day). They "
                f"are included in the total of {total} but not in the day-by-day table, which "
                f"shows only the {dated} dated event(s). They remain stored and queryable.")

    @staticmethod
    def _capture_time_disclosure(window: TimeWindow, records) -> str:
        """How the rendered events were placed on days, and where the two clocks
        disagree (Decision 3, amended).

        Deterministic and computed from stored columns only. Both clocks are kept:
        this states how many events WOULD have landed on a different day under the
        old send-time basis, and by how much — the divergence is the evidence, so
        it is reported rather than quietly corrected away.
        """
        if not records:
            return ""
        by_cap = [r for r in records if r.day_basis == "capture"]
        fallback = [r for r in records if r.day_basis == "send"]
        unvalidated = [r for r in by_cap if not r.capture_time_tz_validated]

        # Day-level disagreement between the two clocks, over rows that carry both.
        both = [r for r in by_cap if r.event_time_utc is not None]
        moved = [r for r in both
                 if window.local_date(r.capture_time_utc)
                 != window.local_date(r.event_time_utc)]
        lags = [(r.event_time_utc - r.capture_time_utc).total_seconds() for r in both]

        rows = ["## How these events were dated"]
        if by_cap:
            rows.append(
                f"**{len(by_cap)} of {len(records)} event(s) are placed by camera capture "
                f"time**, decoded from the image filename and counted by {window.day_basis_label()}. "
                "The alert send time remains stored for every one of them and was not "
                "overwritten.")
        if fallback:
            rows.append(
                f"**{len(fallback)} event(s) had no decodable capture time** and are placed "
                "by alert send time instead, flagged in the data rather than dropped.")
        if moved:
            worst = max((r.event_time_utc - r.capture_time_utc) for r in moved)
            hrs = worst.total_seconds() / 3600
            days = sorted({window.local_date(r.capture_time_utc).isoformat() for r in moved})
            rows.append(
                f"**{len(moved)} event(s) would have been dated to a DIFFERENT day under the "
                f"old send-time basis** (largest gap {hrs:.1f}h between capture and send). "
                f"Their true capture days are: {', '.join(days)}. This is the mis-dating the "
                "capture-time key corrects; the send timestamps are retained as evidence.")
        elif both:
            median = sorted(lags)[len(lags) // 2]
            rows.append(
                f"No event in this window changes day between the two clocks (median "
                f"capture-to-send delay {median:.0f}s).")
        if unvalidated:
            rows.append(
                f"**{len(unvalidated)} event(s) fall outside the validated BST window** for "
                "the timezone conversion (see the note below); their capture times are "
                "decoded but not validated.")
        return "\n\n".join(rows)

    def _unmapped_disclosure(self, window: TimeWindow) -> str:
        """Conditional (only when present): detections that could not be mapped to
        a known species — an unmapped token, or no upstream label at all. The
        reader sees the report, not the log, so this belongs in the report."""
        unmapped = self._retrieval.find_unmapped(window)
        if not unmapped:
            return ""
        tokens = sorted({self._unmapped_token(r) for r in unmapped})
        return ("## Unmapped detections\n\n"
                f"**{len(unmapped)} detection(s) could not be mapped to a known species** "
                f"(raw upstream tokens: {', '.join(tokens)}). They are stored and queryable "
                "under `unmapped:<token>` but are NOT counted above; the alias table does "
                "not yet cover them.")

    @staticmethod
    def _unmapped_token(r) -> str:
        if r.upstream_label:
            return r.upstream_label
        if r.canonical_binomial and ":" in r.canonical_binomial:
            return r.canonical_binomial.split(":", 1)[1]   # strip 'unmapped:' prefix
        return "?"

    def _counts_section(self, window: TimeWindow, by_day: "Counter", dated: int,
                        data_first=None, data_last=None) -> str:
        cov_first, cov_last = self._coverage_local_dates(window, data_first, data_last)
        rows = ["## Day-by-day alert-event counts",
                f"_The {dated} dated event(s); this table sums to {dated}. Counts are alert "
                "events, not animals — see honesty notes below._",
                "",
                self._hold(self._counts_chart(window, by_day, cov_first, cov_last)),
                "",
                "_The bars are the SAME per-day counts as the table below — discrete daily "
                "alert-event counts, not a trend line. No values are derived, smoothed, or "
                "interpolated; every bar equals its table row._",
                "",
                f"| Date ({window.day_basis_label()}) | Alert events |", "| --- | ---: |"]
        no_data = 0
        for day in window.days():
            iso = day.isoformat()
            count = by_day.get(iso, 0)
            if count:
                cell = str(count)
            elif cov_first is not None and cov_first <= day <= cov_last:
                cell = "0"                       # in-coverage zero — a true zero-activity day
            else:
                cell = "no data"                 # item 2: outside coverage => status unknown
                no_data += 1
            rows.append(f"| {iso} | {cell} |")
        rows.append(f"| **Total (dated)** | **{dated}** |")
        if no_data:
            rows.append(f"_**{no_data} day(s) shown as 'no data'** fall before the first or "
                        "after the last detection the dataset holds — the monitoring status "
                        "then is UNKNOWN, not zero activity. A trailing run of these is "
                        "unresolved coverage, not animals leaving; only days within the "
                        "recorded span can be a true zero._")
        return "\n".join(rows)

    @staticmethod
    def _counts_chart(window: TimeWindow, by_day: "Counter", cov_first=None,
                      cov_last=None) -> str:
        """A static, dependency-free inline-SVG bar chart of the EXACT per-day
        counts in the table (one bar per day, height = that day's alert-event
        count). Deterministic and computed from the same `by_day` the table uses,
        so the two can never disagree — this is presentation of existing figures,
        not a new number. BARS, deliberately, not a line: the counts are discrete
        per-day events; a connecting line would imply a continuous trend the data
        does not support. No per-bar value labels (the table is authoritative and
        adjacent); only 0/max gridlines give magnitude. The web layer sanitises
        this against a fixed tag/attribute allowlist (no script/href/handlers)."""
        days = window.days()
        counts = [by_day.get(d.isoformat(), 0) for d in days]
        n = len(days)
        maxc = max(counts) if counts else 0

        # Layout in SVG user units; width="100%" scales it to the container.
        pad_l, pad_r, pad_t, pad_b = 30, 12, 16, 42
        plot_h, col_w, bar_w = 150, 30, 20
        base_y = pad_t + plot_h
        width = pad_l + n * col_w + pad_r
        height = pad_t + plot_h + pad_b
        scale = (plot_h / maxc) if maxc > 0 else 0.0

        green, axis, muted = "#1b5e3f", "#cbd6ce", "#4a4a4a"
        first, last = days[0].isoformat(), days[-1].isoformat()
        parts = [
            f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
            f'aria-label="Bar chart of daily alert-event counts from {first} to {last}; '
            f'the same values as the table, peaking at {maxc}.">',
            f'<title>Daily alert-event counts ({first} to {last})</title>',
            f'<line x1="{pad_l}" y1="{base_y}" x2="{width - pad_r}" y2="{base_y}" '
            f'stroke="{axis}" stroke-width="1"></line>',
            f'<text x="{pad_l - 6}" y="{base_y + 4}" fill="{muted}" font-size="10" '
            'text-anchor="end">0</text>',
        ]
        # Item 2: shade out-of-coverage day columns as "no data", so an empty
        # trailing (or leading) run reads as unresolved coverage, not zero activity.
        def _out(d):
            return cov_first is None or d < cov_first or d > cov_last
        run_start = None
        runs = []
        for i, d in enumerate(days):
            if _out(d):
                run_start = i if run_start is None else run_start
            elif run_start is not None:
                runs.append((run_start, i - 1)); run_start = None
        if run_start is not None:
            runs.append((run_start, len(days) - 1))
        for a, b in runs:
            rx = pad_l + a * col_w
            rw = (b - a + 1) * col_w
            parts.append(f'<rect x="{rx:.1f}" y="{pad_t}" width="{rw:.1f}" height="{plot_h}" '
                         'fill="#e6eae6"></rect>')
            parts.append(f'<text x="{rx + rw / 2:.1f}" y="{pad_t + plot_h / 2:.1f}" '
                         f'fill="{muted}" font-size="9" text-anchor="middle">no data</text>')
        if maxc > 0:
            parts.append(
                f'<line x1="{pad_l}" y1="{pad_t}" x2="{width - pad_r}" y2="{pad_t}" '
                f'stroke="{axis}" stroke-width="1" stroke-dasharray="2 3"></line>')
            parts.append(
                f'<text x="{pad_l - 6}" y="{pad_t + 4}" fill="{muted}" font-size="10" '
                f'text-anchor="end">{maxc}</text>')
        for i, c in enumerate(counts):
            if c <= 0:
                continue
            h = c * scale
            x = pad_l + i * col_w + (col_w - bar_w) / 2
            y = base_y - h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" '
                f'rx="3" fill="{green}"></rect>')
        # Sparse x-axis date ticks so labels never collide (~12 max, horizontal).
        step = max(1, math.ceil(n / 12))
        for i, d in enumerate(days):
            if i % step:
                continue
            cx = pad_l + i * col_w + col_w / 2
            parts.append(
                f'<text x="{cx:.1f}" y="{base_y + 16}" fill="{muted}" font-size="9" '
                f'text-anchor="middle">{d.strftime("%m-%d")}</text>')
        parts.append("</svg>")
        return "".join(parts)

    # ------------------------------------------------- period-over-period (Change 2)
    def _compute_comparison(self, term, key, name, window: TimeWindow,
                            current_count: int, *, prior_count: Optional[int] = None,
                            earliest: Optional[datetime] = None,
                            earliest_known: bool = False) -> _Comparison:
        """Deterministic. Reads the prior equal-length period via the retrieval
        agent (same query path, same species key) and computes the change with
        PLAIN ARITHMETIC — delta, direction, and a percent ONLY when the prior
        count is > 0 (never a divide-by-zero; an empty or pre-data baseline yields
        the 'no baseline' state instead). Nothing here interprets or explains; the
        result is figures, and the LLM is never handed them.

        The all-species path supplies ``prior_count``/``earliest`` directly (there
        is no single species term to resolve), reusing the same arithmetic and the
        same ``_comparison_section`` rendering rather than a parallel one."""
        prior_window = window.previous_period()
        if prior_count is None:
            prior_count = len(self._retrieval.find(term, prior_window))   # dated, in prior window
        if not earliest_known:
            earliest = self._retrieval.earliest_dated(key)
        predates = earliest is None or prior_window.end_utc < earliest
        has_baseline = prior_count > 0
        delta = pct = None
        if has_baseline:
            delta = current_count - prior_count
            pct = 100.0 * delta / prior_count
            direction = "up" if delta > 0 else "down" if delta < 0 else "unchanged"
        else:
            direction = "no-baseline"
        return _Comparison(prior_window, current_count, prior_count, has_baseline,
                           predates, earliest, delta, pct, direction)

    def _comparison_section(self, name: str, window: TimeWindow, comp: _Comparison) -> str:
        prior_range = self._fmt_range(comp.prior_window)
        curr_range = self._fmt_range(window)
        rows = ["## Period-over-period comparison"]

        if comp.has_baseline:
            if comp.direction == "unchanged":
                change = (f"Alert events were **unchanged at {comp.current_count}**")
            else:
                verb = "rose" if comp.direction == "up" else "fell"
                change = (f"Alert events **{verb} from {comp.prior_count} to "
                          f"{comp.current_count}** ({comp.delta:+d}, {comp.pct:+.0f}%)")
            rows.append(
                f"{change} between the previous equal-length period "
                f"(**{prior_range}**, {self._n(comp.prior_count, 'event')}) and this window "
                f"(**{curr_range}**, {self._n(comp.current_count, 'event')}).")
            rows.append("")
            rows.append(self._hold(
                self._comparison_chart(prior_range, comp.prior_count,
                                       curr_range, comp.current_count)))
        else:
            if comp.earliest_dated is None:
                why = f"no dated {name} records exist in the data at all"
            elif comp.baseline_predates_data:
                why = (f"the previous equal-length period (**{prior_range}**) is entirely "
                       f"before the earliest dated {name} record held "
                       f"({comp.earliest_dated.date().isoformat()})")
            else:
                why = (f"the previous equal-length period (**{prior_range}**) contains "
                       f"0 dated {name} alert events")
            rows.append(
                f"**No comparable prior period exists in the data** — {why}, so no change "
                f"can be computed. (This window: **{curr_range}**, "
                f"{self._n(comp.current_count, 'dated alert event')}.)")

        # Scope + reconciliation: the current figure IS the table's dated total, and
        # the exclusions match what the table already discloses (uncertainty, not
        # absence). Provable consistency, stated plainly.
        rows.append(
            "")
        rows.append(
            f"_Compared on **dated** alert events only. This window's figure "
            f"(**{comp.current_count}**) is the day-by-day table's dated total; the prior "
            f"figure (**{comp.prior_count}**) is the same measure over the previous period. "
            "Undated events (no usable timestamp) and unmapped detections are excluded from "
            "BOTH — they cannot be placed in a period — and are disclosed separately._")
        return "\n".join(rows)

    @staticmethod
    def _comparison_chart(prior_label: str, prior_c: int,
                          curr_label: str, curr_c: int) -> str:
        """Two labelled bars — prior vs current — labelled with the ACTUAL date
        ranges, not 'previous'/'current' (which would hide which periods are
        compared and imply an adjacency/equivalence the data may not have). Same
        static-SVG, no-trend-line discipline as the day-by-day chart."""
        bars = [(prior_label, prior_c), (curr_label, curr_c)]
        maxc = max(prior_c, curr_c) or 1
        pad_l, pad_r, pad_t, pad_b = 12, 12, 22, 40
        plot_h, col_w, bar_w = 130, 190, 104
        base_y = pad_t + plot_h
        width = pad_l + 2 * col_w + pad_r
        height = pad_t + plot_h + pad_b
        scale = plot_h / maxc
        green, axis, muted, ink = "#1b5e3f", "#cbd6ce", "#4a4a4a", "#123c29"
        parts = [
            f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
            f'aria-label="Two-bar comparison of dated alert-event counts: {prior_label} had '
            f'{prior_c}, {curr_label} had {curr_c}.">',
            f'<title>Period-over-period: {prior_label} ({prior_c}) vs {curr_label} ({curr_c})</title>',
            f'<line x1="{pad_l}" y1="{base_y}" x2="{width - pad_r}" y2="{base_y}" '
            f'stroke="{axis}" stroke-width="1"></line>',
        ]
        for i, (label, c) in enumerate(bars):
            cx = pad_l + i * col_w + col_w / 2
            h = c * scale
            x = cx - bar_w / 2
            y = base_y - h
            if c > 0:
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" '
                    f'rx="3" fill="{green}"></rect>')
            label_y = (y - 6) if c > 0 else (base_y - 6)
            parts.append(
                f'<text x="{cx:.1f}" y="{label_y:.1f}" fill="{ink}" font-size="13" '
                f'font-weight="600" text-anchor="middle">{c}</text>')
            parts.append(
                f'<text x="{cx:.1f}" y="{base_y + 20}" fill="{muted}" font-size="11" '
                f'text-anchor="middle">{label}</text>')
        parts.append("</svg>")
        return "".join(parts)

    @staticmethod
    def _fmt_range(win: TimeWindow) -> str:
        """Human date-range label, e.g. '26 Jun–2 Jul 2026'. Avoids strftime %-d
        (absent on Windows) by building the day number directly."""
        days = win.days()
        d0, d1 = days[0], days[-1]
        if d0 == d1:
            return f"{d0.day} {d0:%b %Y}"
        if (d0.year, d0.month) == (d1.year, d1.month):
            return f"{d0.day}–{d1.day} {d0:%b %Y}"
        if d0.year == d1.year:
            return f"{d0.day} {d0:%b}–{d1.day} {d1:%b %Y}"
        return f"{d0.day} {d0:%b %Y}–{d1.day} {d1:%b %Y}"

    # ------------------------------------------------ computed highlights
    def _highlights_section(self, window: TimeWindow, key: str, name: str,
                            records, by_day: "Counter", undated: int, gaps: list,
                            op_first=None, op_last=None) -> str:
        """Notable observations, COMPUTED from stored records — no model in the
        assertion path.

        Every other section of this report states deterministic facts and keeps the
        LLM out of the claim; this one used to hand it the microphone. Everything
        below is arithmetic over columns already in the database, on the same basis
        as the day-by-day table (alert events, local days, capture time where
        decoded), with excluded rows disclosed rather than quietly folded in.
        """
        # `gaps` (proven outages) is computed once by the caller and shared with the
        # narrative, effort line, table, and coverage blocks, so none can disagree.
        others = self._retrieval.find_others(key, window)
        rows = ["## Notable observations",
                f"_Computed from the stored detection records — no model involvement. Same "
                f"basis as the table above: alert events (not animals), {window.day_basis_label()}, "
                "capture time where it was decoded._"]

        # --- busiest / quietest ACTIVE day ------------------------------------
        active = {d: c for d, c in by_day.items() if c}
        if active:
            busiest = max(active.items(), key=lambda kv: (kv[1], kv[0]))
            quietest = min(active.items(), key=lambda kv: (kv[1], kv[0]))
            rows.append(f"**Busiest day:** {self._fmt_day(busiest[0])} — "
                        f"{self._n(busiest[1], 'alert event')}.")
            if quietest[0] != busiest[0]:
                rows.append(f"**Quietest active day:** {self._fmt_day(quietest[0])} — "
                            f"{self._n(quietest[1], 'alert event')}.")
            rows.append(self._zero_day_breakdown(window, name, by_day, gaps, others,
                                                 op_first, op_last))
        else:
            rows.append(f"**No {name} alert events fell in this window**, so there is no "
                        "busiest or quietest day to report.")

        # --- non-target activity ----------------------------------------------
        rows.append(self._non_target_highlight(window, name, others))

        # --- delivery gaps (data gap, NOT an activity gap) --------------------
        absences = self._species_absences(window, key, gaps, others)
        rows.append(self._coverage_gap_highlight(window, key, name, gaps, absences))

        # --- scoping: anything these highlights could not see ------------------
        caveats = []
        if undated:
            caveats.append(f"{self._n(undated, 'event')} of this species had no usable "
                           "timestamp and could not be placed on a day, so they are absent "
                           "from the busiest/quietest figures above (see below)")
        unmapped = self._retrieval.find_unmapped(window)
        if unmapped:
            caveats.append(f"{self._n(len(unmapped), 'detection')} could not be mapped to a "
                           "species and are therefore NOT counted in the non-target list "
                           "above (see 'Unmapped detections')")
        if caveats:
            rows.append("_Scope: " + "; ".join(caveats) + "._")
        rows.append("_Cross-check agreement is reported under 'Data-quality flags' below, "
                    "where the per-detection rationales live._")
        return "\n\n".join(r for r in rows if r)

    def _zero_day_breakdown(self, window: TimeWindow, name: str,
                            by_day: "Counter", gaps: list[tuple], others,
                            op_first=None, op_last=None) -> str:
        """Days with no detections, split by WHY they are empty.

        A raw "N of M days had none" conflates three different things, and two of
        them are not quiet cameras:

        * days the window covers but the **dataset does not** — a window can run
          past the last confirmed-operational point, and those days are absence of
          DATA (same send-time coverage bound the day-by-day table marks 'no data');
        * days overlapped by a **proven delivery silence** — absence of *delivery*,
          explained in the coverage-gap table below;
        * days the system was demonstrably delivering and saw nothing — the only
          ones that mean the species was not detected.

        Leaving them merged would let one screen imply both "no animals" and
        "outage" for the same day without connecting them.
        """
        zero_days = [d for d in window.days() if not by_day.get(d.isoformat(), 0)]
        total_days = len(window.days())
        if not zero_days:
            return (f"**Days with no {name} alert events:** none — every one of the "
                    f"{total_days} days in this window carried at least one.")

        # Coverage = the SEND-time span (op_first/op_last), the same bound the
        # day-by-day table uses to mark 'no data', so the two cannot disagree.
        first = window.local_date(op_first) if op_first else None
        last = window.local_date(op_last) if op_last else None
        uncovered = [d for d in zero_days
                     if (first is not None and d < first) or (last is not None and d > last)]
        covered = [d for d in zero_days if d not in uncovered]

        # Classified by the SAME function the coverage block uses, over the same
        # `gaps`, so a day cannot be 'confirmed absent' in one section and 'no
        # data' in the other. Each day is judged on its own local span.
        other_times = sorted(r.effective_time_utc for r in others
                             if r.effective_time_utc is not None)
        kinds: dict[str, list] = {}
        for d in covered:
            a = datetime.combine(d, time.min, tzinfo=window._zone).astimezone(timezone.utc)
            b = datetime.combine(d, time.max, tzinfo=window._zone).astimezone(timezone.utc)
            kind, _down, _up, _n = self._classify_interval(a, b, gaps, other_times)
            kinds.setdefault(kind, []).append(d)

        out = [f"**Days with no {name} alert events:** {len(zero_days)} of {total_days} in "
               "the window — but they do not all mean the same thing:"]
        if uncovered:
            span = (f"the data held runs {first.isoformat()} to {last.isoformat()}"
                    if first and last else "no records are held at all")
            out.append(f"- **{len(uncovered)} outside the data's coverage** ({span}); the "
                       "window extends beyond the records, so these are days with no DATA, "
                       "not days with no detections.")
        if kinds.get("no_data"):
            out.append(f"- **{len(kinds['no_data'])} lost to a delivery silence** listed "
                       "under 'System downtime' below — on these days the absence is of "
                       "alert *delivery*, and says nothing about what was in front of the "
                       "camera.")
        if kinds.get("mixed"):
            out.append(f"- **{len(kinds['mixed'])} partly affected by a delivery silence** — "
                       "the system was down for part of the day and confirmed delivering "
                       f"for the rest, so {name} was genuinely absent for part of it and "
                       "unobservable for the other part.")
        if kinds.get("absent"):
            out.append(f"- **{len(kinds['absent'])} with the system confirmed delivering** "
                       f"(other species arrived that day) and no {name} detected — only "
                       "these support 'none seen that day'.")
        if kinds.get("unconfirmed"):
            out.append(f"- **{len(kinds['unconfirmed'])} undetermined** — no proven outage, "
                       "but nothing of ANY species arrived either, so a real absence and "
                       "undetected downtime cannot be told apart.")
        return "\n".join(out)

    @staticmethod
    def _days_touched(window: TimeWindow, start: datetime, end: datetime) -> list:
        """Local calendar days any part of ``[start, end]`` falls on."""
        first, last = window.local_date(start), window.local_date(end)
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    def _non_target_highlight(self, window: TimeWindow, name: str, others) -> str:
        """Detections of OTHER species in the same window — for a single-species
        report this is often the genuinely notable content, and nothing in a VLM
        description supplies it. Renders an explicit negative when the answer is
        zero, so a computed highlight never vanishes silently (uncertainty is not
        absence)."""
        if not others:
            return ("**Non-target activity:** none — no detections of any other mapped "
                    "species fell in this window.")

        grouped: dict[str, list] = {}
        for r in others:
            grouped.setdefault(r.canonical_binomial, []).append(r)
        lines = [f"**Non-target activity:** {self._n(len(others), 'alert event')} from "
                 f"{len(grouped)} other species in the same window."]
        for binomial, evs in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            display = next((e.display_common_name for e in evs if e.display_common_name), None)
            label = f"**{display}** (*{binomial}*)" if display else f"**{binomial}**"
            days = sorted({window.local_date(e.effective_time_utc) for e in evs})
            lines.append(f"- {label} — {self._n(len(evs), 'alert event')} on "
                         f"{self._fmt_day_list(days)}")
        return "\n".join(lines)

    # -------------------------------------------- outage / coverage / effort
    @staticmethod
    def _coverage_local_dates(window: TimeWindow, data_first, data_last):
        """The window's data-coverage bounds as LOCAL dates: (first, last), or
        (None, None) when the dataset holds nothing. Days outside these bounds are
        absence of DATA, not of activity — the reader must not read them as zero."""
        if data_first is None or data_last is None:
            return None, None
        return window.local_date(data_first), window.local_date(data_last)

    @staticmethod
    def _downtime_hours(gaps: list[tuple], window: TimeWindow) -> float:
        """Total PROVEN-downtime hours, each gap clamped to the window."""
        lo, hi = window.start_utc, window.end_utc
        total = 0.0
        for start, end, *_ in gaps:
            s, e = max(start, lo), min(end, hi)
            if e > s:
                total += (e - s).total_seconds() / 3600
        return total

    def _outage_trend_caveat(self, gaps: list[tuple], window: TimeWindow,
                             by_day: "Counter", dated: int) -> str:
        """Deterministic reconciliation of the trend claim with the outage — the
        two facts the reader would otherwise have to combine themselves. Fired
        whenever a proven outage falls in the window; the LLM is separately told
        not to describe a trend, but this sentence stands regardless of what it
        writes, in the same section as the claim.

        The reach is QUANTIFIED (item 2), not vibes: how many active days a proven
        outage touches and how much of the event total sits on them — all derived
        from the outage records and the daily table."""
        largest = max(gaps, key=lambda g: g[2])
        hrs = largest[2] / 3600
        rng = f"{largest[0].strftime('%d %b')}–{largest[1].strftime('%d %b')} UTC"
        more = "" if len(gaps) == 1 else f"; the largest of {len(gaps)} proven outages here"

        outage_dates = set()
        for start, end, *_ in gaps:
            outage_dates |= set(self._days_touched(window, start, end))
        active = [d for d in window.days() if by_day.get(d.isoformat(), 0)]
        touched = [d for d in active if d in outage_dates]
        events_on_touched = sum(by_day.get(d.isoformat(), 0) for d in touched)
        if active and touched:
            pct = 100 * events_on_touched / dated if dated else 0
            reach = (f" {len(touched)} of {len(active)} active days are touched by a proven "
                     f"outage; those days carry {events_on_touched} of {dated} events "
                     f"({pct:.0f}%).")
        elif active:
            reach = (f" None of the {len(active)} active days is touched by a proven outage "
                     "(the outages fall on days with no events).")
        else:
            reach = ""
        return (f"_⚠️ **Trend caution:** a proven ~{hrs:.0f}h system outage "
                f"({rng}{more}) falls within this window and distorts the day-by-day "
                "shape — it suppresses counts around the silence and releases a backlog "
                f"afterwards.{reach} Any apparent rise or decline is not corrected for "
                "operational uptime and must not be read as animal behaviour._")

    def _monitoring_effort_section(self, window: TimeWindow, gaps: list[tuple],
                                   op_first, op_last) -> str:
        """Uptime denominator (item 3): the effort behind the raw counts. Descriptive
        only — states hours operational vs the hours we have data for, and DECLINES
        to compute events-per-hour, because with the confounds noted that rate would
        not be sound.

        Coverage is the first-to-last DETECTION SPAN (send-time bound, the last
        confirmed-operational point), NOT the calendar range the dates touch — a
        18.1-day span across 19 calendar days is 434.9h, not 456h. The line names
        the two timestamps and labels the number a span so the basis is explicit,
        and uses one precision (.1f) for coverage in both the display and the
        uptime arithmetic."""
        window_h = (window.end_utc - window.start_utc).total_seconds() / 3600
        downtime_h = self._downtime_hours(gaps, window)
        if op_first is None or op_last is None:
            return ("## Monitoring effort\n\n_The dataset holds no dated detections in this "
                    f"window ({window_h:.0f}h requested), so no operational time can be "
                    "stated. The counts above cannot be read as effort-relative._")
        # Clamp the detection span to the window, so a window that clips the data
        # reports only its own covered portion. Recomputed per report and window.
        cov_start = max(window.start_utc, op_first)
        cov_end = min(window.end_utc, op_last)
        coverage_h = max((cov_end - cov_start).total_seconds() / 3600, 0.0)
        out_h = max(window_h - coverage_h, 0.0)
        operational_h = max(coverage_h - downtime_h, 0.0)
        uptime = 100 * operational_h / coverage_h if coverage_h else 0.0
        rows = ["## Monitoring effort",
                f"The requested window spans **{window_h:.0f}h**. Of that, the dataset's "
                "coverage — from the first to the last detection delivered "
                f"({cov_start.strftime('%Y-%m-%d %H:%M')} → "
                f"{cov_end.strftime('%Y-%m-%d %H:%M')} UTC), a span of **{coverage_h:.1f}h** "
                "(not the calendar range those dates touch) — carries the data; the "
                f"remaining **{out_h:.0f}h** fall outside it, before the first or after the "
                "last detection, where monitoring status is **unknown**, not zero activity."]
        rows.append(
            f"Within that span, proven system downtime accounts for **{downtime_h:.1f}h**, so "
            f"the system was **confirmed operational for {operational_h:.1f} of "
            f"{coverage_h:.1f} hours ({uptime:.0f}%)**.")
        rows.append(
            "_The daily counts above are **raw over this effort** — deliberately NOT divided "
            "by operational hours. With one camera, one location, and no control for "
            "time-of-day, an events-per-hour rate would not be a sound figure; the uptime is "
            "given so the counts can be read against the effort behind them, not turned into "
            "a rate here._")
        return "\n\n".join(rows)

    def _proven_gaps(self, window: TimeWindow) -> list[tuple]:
        """Delivery silences that are EVIDENCED as outages, computed once and shared
        by the zero-day line and the coverage-gap table so the two cannot disagree.

        Returns ``(start, end, seconds, recovered_count)`` per gap.
        """
        timeline = self._retrieval.delivery_timeline(window)
        if len(timeline) < 2:
            return []
        sends = [s for s, _ in timeline]
        proven = []
        for i in range(len(sends) - 1):
            start, end = sends[i], sends[i + 1]
            gap = (end - start).total_seconds()
            if gap < _MIN_DELIVERY_GAP_SECONDS:
                continue
            # Captures taken during the silence that were STILL UNDELIVERED when it
            # ended (send strictly after the gap closed). The `s > end` clause is
            # what makes this evidence: under normal operation an alert is sent
            # ~33s after its capture, so every event's own capture necessarily
            # falls inside the silence preceding its own send — counting those
            # would mark every quiet night as an outage. Only genuinely back-dated
            # events, delivered later than the alert that reopened the channel,
            # show that capture continued while delivery did not.
            recovered = [c for s, c in timeline if c is not None and start < c < end and s > end]
            if recovered:
                proven.append((start, end, gap, len(recovered)))
        return proven

    @staticmethod
    def _classify_interval(a: datetime, b: datetime, system_gaps: list[tuple],
                           other_times: list) -> tuple[str, float, float, int]:
        """THE single definition of what a period with no detections means.

        Shared by the zero-day breakdown and the coverage block so a period can
        never be 'confirmed absent' in one section and 'no data' in another.
        Returns ``(kind, downtime_hours, confirmed_up_hours, others_delivered)``:

        * ``no_data`` — downtime accounts for essentially all of it, or nothing of
          any species arrived to prove otherwise. Presence unverifiable.
        * ``absent`` — no downtime at all, and other species WERE arriving, so the
          system is confirmed working and the absence is real.
        * ``mixed`` — both, in material amounts. Reported as a split rather than
          rounded to either pole: calling it all 'no data' would discard hours the
          system provably worked, and calling it all 'absent' would claim hours it
          did not.
        * ``unconfirmed`` — no proven outage, but nothing of any species arrived
          either, so a real absence and undetected downtime cannot be told apart.
        """
        length = (b - a).total_seconds()
        overlapping = [g for g in system_gaps if g[0] < b and g[1] > a]
        downtime = sum(min(g[1], b).timestamp() - max(g[0], a).timestamp()
                       for g in overlapping)
        up = max(length - downtime, 0.0)
        delivered = sum(1 for t in other_times if a < t < b)
        if not overlapping:
            return ("absent" if delivered else "unconfirmed", 0.0, up / 3600, delivered)
        if not delivered or (length and up / length < _NEGLIGIBLE_UP_FRACTION):
            return "no_data", downtime / 3600, up / 3600, delivered
        return "mixed", downtime / 3600, up / 3600, delivered

    def _species_absences(self, window: TimeWindow, key: str,
                          system_gaps: list[tuple], others) -> list[tuple]:
        """Extended periods with no detection of THIS species, each classified by
        whether the system was up at the time.

        Returns ``(start, end, seconds, kind, evidence)`` where ``kind`` is:

        * ``no_data`` — the period overlaps a proven system-wide outage, so this
          species' presence is simply unverifiable then. NOT a statement about the
          species.
        * ``absent`` — no system outage, and other species WERE being delivered
          throughout, so the camera is confirmed working and the absence is real.
        * ``unconfirmed`` — no proven outage, but nothing of any species arrived
          either, so downtime and a genuinely dead-quiet period cannot be told
          apart. Stated as such rather than resolved by assumption.

        Bounded to the dataset's own span: outside it there is no data at all, and
        that is the zero-day breakdown's business, not an absence claim.
        """
        first_dt, last_dt = self._retrieval.data_span()
        if first_dt is None or last_dt is None:
            return []
        span_start = max(window.start_utc, first_dt)
        span_end = min(window.end_utc, last_dt)
        if span_start >= span_end:
            return []

        seen = sorted(c or s for s, c in
                      self._retrieval.delivery_timeline(window, key))
        marks = [span_start] + [t for t in seen if span_start <= t <= span_end] + [span_end]
        other_times = sorted(r.effective_time_utc for r in others
                             if r.effective_time_utc is not None)

        out = []
        for a, b in zip(marks, marks[1:]):
            length = (b - a).total_seconds()
            if length < _MIN_SPECIES_ABSENCE_SECONDS:
                continue
            kind, down_h, up_h, delivered = self._classify_interval(
                a, b, system_gaps, other_times)
            out.append((a, b, length, kind, (down_h, up_h, delivered)))
        return out

    def _coverage_gap_highlight(self, window: TimeWindow, key: str, name: str,
                                proven: list[tuple], absences: list[tuple]) -> str:
        """Silences, reported SPECIES-SCOPED and split by what they mean.

        Two silences look identical on a timeline and mean opposite things, so the
        report never merges them:

        * **System downtime** — the mailbox delivered nothing for ANY species.
          Computed dataset-wide (one species falling quiet is not an outage) and so
          it appears on every species report, because it bears on whether that
          species' zero-days are real. During it this species' presence is
          UNVERIFIABLE: absence of data, not absence of animals.
        * **Species absence** — the system was demonstrably delivering other
          species throughout, and this one still had nothing. That is a real
          observation about the species.

        Recovery counts are scoped to THIS species. A dataset-wide figure beside a
        filtered report is a misattribution: the 53h outage recovered 34 events, but
        on a carrion crow report none of them were crows.
        """
        proven = sorted(proven, key=lambda g: -g[2])
        species_timeline = self._retrieval.delivery_timeline(window, key)
        out = []

        if proven:
            scoped = []
            for start, end, gap, _all_n in proven:
                mine = [c for s, c in species_timeline
                        if c is not None and start < c < end and s > end]
                scoped.append((start, end, gap, len(mine)))
            mine_total = sum(n for *_, n in scoped)
            out += [
                f"**System downtime — no data, not an absence of animals:** "
                f"{self._n(len(proven), 'period')} in this window where no alert was "
                "delivered for ANY species while the camera kept capturing. This is a "
                "property of the system, so it appears identically on every species "
                f"report. **{name} presence is unverifiable during these periods** — "
                "anything captured then and never delivered is unknown and unknowable "
                "from this data.",
                "",
                f"| Silence (UTC) | Duration | {name} events recovered afterwards |",
                "| --- | ---: | ---: |",
            ]
            for start, end, gap, n in scoped[:_MAX_GAPS_SHOWN]:
                out.append(f"| {start.strftime('%d %b %H:%M')} → "
                           f"{end.strftime('%d %b %H:%M')} | {gap / 3600:.1f}h | {n} |")
            if len(scoped) > _MAX_GAPS_SHOWN:
                out.append(f"\n_Showing the {_MAX_GAPS_SHOWN} longest of {len(scoped)} "
                           "evidenced periods, longest first._")
            note = (f"_Counts are {name} events only — captured during the silence and "
                    "delivered after it ended. They are NOT the dataset-wide totals: a "
                    "downtime period can recover many events of other species and none of "
                    "this one. 'Recovered' is also not the same as 'mis-dated', since an "
                    "event can be held back and still arrive on the day it was captured._")
            if mine_total == 0:
                zero_note = (
                    f"_**No {name} events at all were recovered from these silences.** The "
                    "downtime is real and dataset-wide, but none of the delayed traffic was "
                    f"{name} — so these rows say only that {name} presence could not be "
                    "observed during them, never that any were present._")
                note = zero_note + "\n\n" + note
            out += ["", note]
        else:
            out.append(
                "**System downtime:** none detected — no delivery silence in this window "
                "was followed by back-dated captures, which is the only evidence that "
                "distinguishes an outage from a genuinely quiet period.")

        # --- species-specific silences ---------------------------------------
        real = [a for a in absences if a[3] == "absent"]
        nodata = [a for a in absences if a[3] == "no_data"]
        unconfirmed = [a for a in absences if a[3] == "unconfirmed"]
        mixed = [a for a in absences if a[3] == "mixed"]
        if real:
            out.append("")
            out.append(f"**{name} absent — system confirmed operational:** these are real "
                       "absences, not missing data. The camera was delivering other species "
                       "throughout each period below.")
            for a, b, length, _k, (_d, _u, delivered) in sorted(real, key=lambda x: -x[2]):
                out.append(f"- No {name} detected {a.strftime('%d %b %H:%M')} → "
                           f"{b.strftime('%d %b %H:%M')} UTC ({length / 3600:.1f}h) while "
                           f"{self._n(int(delivered), 'alert event')} of other species "
                           "arrived — the system was demonstrably working.")
        if nodata:
            out.append("")
            out.append(f"**{name} not detected, but the system was down:** "
                       f"{self._n(len(nodata), 'period')} where a gap in this species' "
                       "stream is accounted for by the downtime above, so nothing can be "
                       "concluded about the species from it.")
            for a, b, length, _k, (down_h, _u, _n) in sorted(nodata, key=lambda x: -x[2]):
                out.append(f"- {a.strftime('%d %b %H:%M')} → {b.strftime('%d %b %H:%M')} UTC "
                           f"({length / 3600:.1f}h), of which {down_h:.1f}h was system "
                           "downtime — no data, presence unverifiable.")
        if mixed:
            out.append("")
            out.append(f"**{name} not detected — partly downtime, partly confirmed "
                       f"absence:** {self._n(len(mixed), 'period')} spanning both. The split "
                       "is stated rather than rounded to one or the other, because calling "
                       "the whole period 'no data' would write off hours the system was "
                       "provably delivering, and calling it all 'absent' would claim "
                       "knowledge of hours it was not.")
            for a, b, length, _k, ev in sorted(mixed, key=lambda x: -x[2]):
                down_h, up_h, delivered = ev
                out.append(
                    f"- {a.strftime('%d %b %H:%M')} → {b.strftime('%d %b %H:%M')} UTC "
                    f"({length / 3600:.1f}h total): **{down_h:.1f}h system downtime** "
                    f"({name} presence unverifiable) and **{up_h:.1f}h with the system "
                    f"confirmed delivering** ({self._n(int(delivered), 'alert event')} of "
                    f"other species arrived) — {name} genuinely absent for that part.")
        if unconfirmed:
            out.append("")
            out.append(f"**{name} not detected, system status unconfirmed:** "
                       f"{self._n(len(unconfirmed), 'period')} with no alert of ANY species "
                       "and no proven outage, so a real absence and undetected downtime "
                       "cannot be told apart. Reported as undetermined rather than resolved "
                       "by assumption.")
            for a, b, length, _k, _ev in sorted(unconfirmed, key=lambda x: -x[2])[:5]:
                out.append(f"- {a.strftime('%d %b %H:%M')} → {b.strftime('%d %b %H:%M')} UTC "
                           f"({length / 3600:.1f}h)")
        return "\n".join(out)

    @staticmethod
    def _fmt_day(iso: str) -> str:
        d = date.fromisoformat(iso)
        return f"{d.day} {d:%b %Y}"

    @staticmethod
    def _fmt_day_list(days: list, limit: int = 6) -> str:
        """Compact day list, e.g. '4, 11 Jul 2026'. Truncates rather than running on."""
        shown, extra = days[:limit], len(days) - limit
        if len({(d.year, d.month) for d in shown}) == 1:
            body = ", ".join(str(d.day) for d in shown) + f" {shown[0]:%b %Y}"
        else:
            body = ", ".join(f"{d.day} {d:%b}" for d in shown) + f" {shown[-1]:%Y}"
        return body + (f" (+{extra} more day(s))" if extra > 0 else "")

    def _vlm_evidence_section(self, window: TimeWindow, records) -> str:
        described = [r for r in records if r.vlm_ok and r.vlm_description]
        header = [
            "## Unverified model descriptions (not observations)",
            "_Raw output from the vision model, kept as EVIDENCE for the confabulation "
            "finding — it asserts nothing, and nothing above depends on it. The computed "
            "highlights above are the observations; these are not._  \n"
            "_Boxed-image descriptions, ranked by animal/scene content. **The VLM is "
            "unreliable: it fabricates on-image text (timestamps, URLs, temperatures, camera "
            "labels) AND can invent animals or species that are not present. A prompt change "
            "instructing it to ignore overlays did not measurably reduce this.** Every "
            "description below is unverified model output — do not treat any quoted text, "
            "count, or species identification as fact._", ""]
        if not described:
            return ("## Unverified model descriptions (not observations)\n\n_None available "
                    "for these events. The computed highlights above are unaffected._")

        def score(r) -> int:
            text = r.vlm_description.lower()
            # Another species dominates; bird count/behaviour at a feeder is minor.
            return (5 * sum(1 for k in _OTHER_SPECIES_KEYWORDS if k in text)
                    + sum(1 for k in _BEHAVIOUR_KEYWORDS if k in text))

        ranked = sorted(described, key=lambda r: (score(r), r.effective_time_utc), reverse=True)
        shown = [r for r in ranked if score(r) > 0][: self._max_notable]
        if not shown:
            header.append("_No description contained notable animal/scene content beyond the "
                          "target species at a feeder._")
            return "\n".join(header)
        for r in shown:
            when = window.local_date(r.effective_time_utc).isoformat()
            ident = r.display_common_name or r.canonical_binomial or "unknown"
            header.append(
                f"- **{when}** ({ident}): \"{_as_plain_text(r.vlm_description)}\"")
        header.append(f"_Selected {len(shown)} of {len(described)} descriptions by animal/scene "
                      "content; the remainder are near-duplicates of the target species at a feeder._")
        return "\n".join(header)

    def _flags_section(self, window: TimeWindow, records, is_species: bool) -> str:
        unconfirmed = [r for r in records if _not_corroborated(r)]
        low_conf = [r for r in records
                    if r.upstream_confidence is not None and r.upstream_confidence <= self._low_conf]
        # Distinguish a model that TRIED and errored (error set) from one that was
        # never run (ok False, no error) — e.g. rows ingested via `store`. Calling
        # the latter a "failure" would imply the model tried and failed.
        bioclip_failed = [r for r in records if not r.bioclip_ok and r.bioclip_error]
        bioclip_not_run = [r for r in records if not r.bioclip_ok and not r.bioclip_error]
        vlm_failed = [r for r in records if not r.vlm_ok and r.vlm_error]
        vlm_not_run = [r for r in records if not r.vlm_ok and not r.vlm_error]

        rows = ["## Data-quality flags"]
        if not (unconfirmed or low_conf or bioclip_failed or bioclip_not_run
                or vlm_failed or vlm_not_run):
            rows.append("_None flagged for the rendered records._")
            return "\n".join(rows)

        if unconfirmed and not self._include_crosscheck:
            # Presentation toggle (REPORT_INCLUDE_CROSSCHECK=false): hide the
            # cross-check flags but DON'T pretend none exist — say they are hidden,
            # not absent (the data is still stored). Keeps the report honest.
            rows.append(
                f"_BioCLIP cross-check display is off (REPORT_INCLUDE_CROSSCHECK=false): "
                f"{len(unconfirmed)} of {len(records)} cross-check flag(s) hidden here but "
                "still stored and available in the data._")
        elif unconfirmed:
            disagree = [r for r in unconfirmed if r.agreement_flag == "disagree"]
            not_eval = [r for r in unconfirmed if r.agreement_flag == "not_evaluable"]
            pct = 100 * len(unconfirmed) / len(records)
            rows.append(
                f"**The cross-check did not corroborate {len(unconfirmed)} of {len(records)} "
                f"detections ({pct:.0f}%)** — BioCLIP's own top-1 identification was not the "
                "upstream species. The verdict is species-level: a near miss in the same "
                "genus is still a disagreement, so the distance below is what says how "
                "serious each one is.")
            # The distance breakdown IS the explanation. It replaced an
            # 'indeterminate' bucket that said only "not comparable" and, on this
            # corpus, absorbed 78% of the data — which read as a mystery pile rather
            # than as the finding it actually was.
            for key, phrase, count in _distance_breakdown(disagree):
                share = 100 * count / len(disagree)
                rows.append(f"- **{phrase} ({count}, {share:.0f}% of disagreements)**")
                examples = [x for x in disagree
                            if (x.taxonomic_distance or "undetermined") == key]
                for r in examples[:2]:
                    when = window.local_date(r.effective_time_utc).isoformat()
                    rows.append(f"  - {when}: {r.agreement_rationale}")
            if not_eval:
                rows.append(
                    f"- **not comparable ({len(not_eval)}):** BioCLIP could not be asked the "
                    "question — a class outside its taxonomy, or a run that produced no "
                    "result. Excluded from the agree/disagree denominator, NOT counted as a "
                    "disagreement — e.g.:")
                for r in not_eval[:2]:
                    when = window.local_date(r.effective_time_utc).isoformat()
                    rows.append(f"  - {when}: {r.agreement_rationale}")
        if low_conf:
            rows.append(f"**Low upstream confidence (<= {self._low_conf:g}): {len(low_conf)} event(s).**")
        if bioclip_failed or vlm_failed:
            parts = []
            if bioclip_failed:
                parts.append(f"BioCLIP on {len(bioclip_failed)}")
            if vlm_failed:
                parts.append(f"VLM on {len(vlm_failed)}")
            rows.append(f"**Enrichment failed ({', '.join(parts)} event(s)):** a model was run "
                        "but returned an error; the events are still recorded.")
        if bioclip_not_run or vlm_not_run:
            parts = []
            if bioclip_not_run:
                parts.append(f"BioCLIP on {len(bioclip_not_run)}")
            if vlm_not_run:
                parts.append(f"VLM on {len(vlm_not_run)}")
            rows.append(f"**Enrichment not run ({', '.join(parts)} event(s)):** ingested without "
                        "enrichment (e.g. via `store`), not a model failure.")
        return "\n".join(rows)

    @staticmethod
    def _n(count: int, noun: str) -> str:
        return f"{count} {noun}" + ("" if count == 1 else "s")

    def _lead(self, count: int, noun: str) -> str:
        # Grammatical lead-in only — the CLAIM body (the constants) is byte-identical
        # regardless of count, so verbatim-stability holds while the prose reads right.
        return f"The **1 {noun}**" if count == 1 else f"Each of the **{count} {noun}s**"

    @staticmethod
    def _send_proxy_stats(window: TimeWindow, records) -> Optional[dict]:
        """Per-report send-vs-capture divergence, from THIS report's own dated events
        that carry both a capture and a send time. Returns None when none qualify
        (e.g. all send-fallback, or an empty report) — the caveat then stands
        figure-free. Same event set the rest of the report is built from, so the
        denominator can never be another scope's N (the bug this fixes)."""
        both = [r for r in records
                if r.day_basis == "capture" and r.event_time_utc is not None
                and r.capture_time_utc is not None]
        if not both:
            return None
        within = sum(1 for r in both
                     if abs((r.event_time_utc - r.capture_time_utc).total_seconds()) <= 60)
        moved = [r for r in both
                 if window.local_date(r.capture_time_utc) != window.local_date(r.event_time_utc)]
        max_off = max((abs((window.local_date(r.event_time_utc)
                            - window.local_date(r.capture_time_utc)).days) for r in moved),
                      default=0)
        n = len(both)
        return {"n": n, "within_pct": 100 * within / n,
                "mis": len(moved), "mis_pct": 100 * len(moved) / n, "max_off": max_off}

    def _honesty_section(self, total: int, dated: int, window: TimeWindow, records,
                         comp: Optional[_Comparison] = None) -> str:
        # Standing caveats, each tied to a figure on the page (not an abstract
        # block under an "always applies" header that invites skipping). The
        # claim wording is verbatim-stable; only the counts/lead-ins change.
        if total:
            s1 = (f"- {self._lead(total, 'alert event')} here is an alert-event count, not an "
                  f"animal — {_COUNT_CLAIM}. So this is {self._n(total, 'event')}, "
                  f"not {self._n(total, 'animal')}.")
        else:
            s1 = f"- **0 alert events** here does not mean no animals were present — {_COUNT_CLAIM}."

        # Per-report empirical divergence, injected around the figure-free claim so
        # the denominator is always THIS report's own N.
        proxy = self._send_proxy_stats(window, records)
        if proxy:
            offset = (f" (largest offset {proxy['max_off']} day(s))" if proxy["mis"] else "")
            emp = (f" In this report's {self._n(proxy['n'], 'dated event')} carrying both a "
                   f"capture and a send time, {proxy['within_pct']:.1f}% were delivered within "
                   f"60s of capture and {proxy['mis']} ({proxy['mis_pct']:.1f}%) fall on a "
                   f"different calendar day under send time{offset}.")
        else:
            emp = ""

        if dated:
            s2 = (f"- {self._lead(dated, 'dated event')} in the table above is "
                  f"{_SEND_TIME_CLAIM}.{emp} Note that {_CAPTURE_TZ_CLAIM}.")
        else:
            s2 = (f"- Any dated event would be {_SEND_TIME_CLAIM}. "
                  f"Note that {_CAPTURE_TZ_CLAIM}.")

        s3 = f"- Every event here (**{self._n(total, 'event')}** in total) was {_DECISION7_CLAIM}."
        parts = ["## What these figures mean", s1, s2, s3]

        # Fourth standing caveat — fires whenever the comparison ran, INCLUDING the
        # no-baseline case (absence of history is itself information, not a missing
        # line). Verbatim-stable claim body; only the lead-in and range vary.
        if comp is not None:
            span = ("" if comp.earliest_dated is None else
                    f" The earliest dated record held for this class is "
                    f"{comp.earliest_dated.date().isoformat()}.")
            if comp.has_baseline:
                lead = (f"- This report weighs the window against **1 prior equal-length "
                        f"period** ({self._fmt_range(comp.prior_window)})")
            else:
                lead = ("- This report found **no prior equal-length period with data** to "
                        "weigh the window against")
            parts.append(f"{lead}; {_COMPARISON_CLAIM}.{span}")
        return "\n\n".join(parts)

    def _narrative(self, name, key, window, total, dated, undated, by_day, gaps) -> str:
        # Feed the LLM the reconciled totals PLUS deterministically-computed
        # variation signals (peaks, zero days, trend) — and forbid re-listing the
        # per-day counts (the table already does that). Computing the signals here
        # keeps them accurate rather than hoping the model derives them.
        definition = load_agent(_NARRATIVE_AGENT)
        outage = bool(gaps)
        parts = [f"Species: {name} (canonical key: {key}).",
                 f"Window: {window.label()}.",
                 f"Total alert events: {total} ({dated} dated, {undated} without a usable timestamp)."]
        active = {d: c for d, c in by_day.items() if c}
        if active:
            span = sorted(active)
            peaks = sorted(active.items(), key=lambda kv: kv[1], reverse=True)[:3]
            zeros = [d.isoformat() for d in window.days()
                     if span[0] <= d.isoformat() <= span[-1] and by_day.get(d.isoformat(), 0) == 0]
            vals = [active[d] for d in span]
            third = max(1, len(vals) // 3)
            early, late = sum(vals[:third]) / third, sum(vals[-third:]) / third
            trend = ("declining" if late < early * 0.6 else
                     "rising" if late > early * 1.4 else "roughly steady")
            parts += [
                f"Busiest days: {', '.join(f'{d} ({c})' for d, c in peaks)}.",
                f"First activity {span[0]} ({active[span[0]]}), last {span[-1]} ({active[span[-1]]}).",
                f"Zero-activity days within the active range: {', '.join(zeros) if zeros else 'none'}.",
            ]
            # Item 1: a proven outage in the window distorts the very shape a trend
            # is read from, so the trend is a confounded figure — DON'T hand the LLM
            # a direction to describe. Give it the confound instead; the LLM is out
            # of the assertion path here as everywhere.
            if outage:
                parts.append(definition.section("outage-facts"))
            else:
                parts.append(f"Overall trend across the window: {trend}.")
        facts = " ".join(parts)

        system = definition.instructions
        if outage:
            system = definition.instructions + "\n" + definition.section("outage-system")
        body = self._llm_narrative_body(facts, system)

        # Item 1: whatever the model wrote (or didn't), attach the deterministic
        # outage caveat in the SAME section, so a reader never has to combine the
        # trend claim and the outage log from two places to see the confound.
        segments = [s for s in (body, self._outage_trend_caveat(gaps, window, by_day, dated)
                                 if outage else "") if s]
        return "## Summary\n\n" + "\n\n".join(segments) if segments else ""

    def _llm_narrative_body(self, facts: str, system: str) -> str:
        """The LLM's prose, subject to the vocabulary gate — the ONLY part of the
        report in the model's words. A narrative that miscounts animals is REJECTED
        and regenerated, and withheld entirely if the model will not comply; the
        figures and honesty language never depend on it. Returns the body text (or
        a stated fallback), never the '## Summary' heading — the caller composes."""
        for attempt in range(1, _NARRATIVE_MAX_ATTEMPTS + 1):
            try:
                text = (self._llm.generate(facts, system=system) or "").strip()
            except Exception as exc:   # a report is always produced, even if the LLM is down
                logger.warning("report_llm_unavailable", extra={"error": repr(exc)})
                return ("_Narrative summary unavailable (LLM error); figures below stand on "
                        "their own._")
            if not text:
                return ""
            violations = narrative_violations(text)
            if not violations:
                return text
            logger.warning("narrative_vocabulary_violation",
                           extra={"attempt": attempt, "terms": violations})
            retry = load_agent(_NARRATIVE_AGENT).section("rejection-retry")
            system = system + "\n" + retry.replace("{terms}", ", ".join(violations))

        logger.error("narrative_withheld_vocabulary",
                     extra={"attempts": _NARRATIVE_MAX_ATTEMPTS, "terms": violations})
        return ("_Narrative summary withheld: the language model repeatedly described these "
                f"figures as counts of animals (using: {', '.join(violations)}) after "
                f"{_NARRATIVE_MAX_ATTEMPTS} attempts, which contradicts what the figures "
                "actually count. The figures below stand on their own._")

    # ------------------------------------------------------------ edge cases
    # These pages precede any query, so there are no figures to tie the caveats
    # to; they carry only the figure-free provisional standing note.
    def _did_you_mean(self, term: str, exc: AmbiguousSpecies, window: TimeWindow) -> str:
        options = "\n".join(f"- {common or key} (`{key}`)" for key, common in exc.candidates)
        return (f"# Detection report — '{term}'\n\n"
                f"**'{term}' is ambiguous — did you mean one of these?**\n\n{options}\n\n"
                "Re-run with a specific name to get counts.\n\n" + _VALIDATION_STANDING)

    def _unrecognised(self, term: str, window: TimeWindow) -> str:
        return (f"# Detection report — '{term}'\n\n"
                f"**'{term}' did not resolve to any known class** in the alias table "
                "(coverage is a known limitation). No records queried.\n\n"
                + _VALIDATION_STANDING)
