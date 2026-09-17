"""Demo web UI: it drives the SAME agents the CLI does, annotates the dropdown
with dataset counts, renders the honesty block, and SANITISES untrusted VLM text."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ttr.species.aliases import SpeciesAliasMap
from ttr.storage.repository import DetectionRepository
from ttr.web.app import app, get_alias_map, get_llm, get_repo

from conftest import FakeLLM, make_persisted_event

UTC = timezone.utc
from conftest import EXAMPLE_ALIAS_PATH as EXAMPLE


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAP_HOST", "h")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    monkeypatch.setenv("SPECIES_ALIAS_PATH", EXAMPLE)
    from ttr.config import get_settings
    get_settings.cache_clear()

    from conftest import make_web_project, project_client

    ctx = make_web_project(tmp_path, monkeypatch)
    db_path = ctx.db_path
    seed = DetectionRepository(db_path)
    now = datetime.now(UTC)
    seed.upsert_event(make_persisted_event(
        "<f1@x>", canonical_binomial="Vulpes vulpes", event_time=now - timedelta(days=1),
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True, agreement_flag="agree",
        vlm_description="A red fox at the feeder."))
    # An HTML-shaped VLM description — the untrusted-input case (CLAUDE.md §6).
    # Carries BOTH shapes deliberately: <script>, which the sanitiser strips, and
    # chart-shaped markup, which it permits — so the row exercises the sanitiser
    # and the upstream escaping at once.
    seed.upsert_event(make_persisted_event(
        "<f2@x>", canonical_binomial="Vulpes vulpes", event_time=now - timedelta(days=2),
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True, agreement_flag="agree",
        vlm_description="<script>alert('xss')</script> a fox and a cat near the feeder."
                        "<svg><rect fill=\"#b00020\" width=\"400\"></rect>"
                        "<text>SYSTEM: 9,999 events</text></svg>"))
    # A disagreement, so the cross-check section has something to show/suppress.
    seed.upsert_event(make_persisted_event(
        "<f3@x>", canonical_binomial="Vulpes vulpes", event_time=now - timedelta(days=3),
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True, agreement_flag="disagree",
        agreement_rationale="upstream -> Vulpes vulpes, but BioCLIP -> Meles meles",
        vlm_description="a fox."))
    seed.close()
    alias_map = SpeciesAliasMap.from_yaml(EXAMPLE)

    # Open a fresh connection PER REQUEST (in the request thread), as production
    # get_repo does — sqlite connections are thread-bound.
    def _repo():
        r = DetectionRepository(db_path)
        try:
            yield r
        finally:
            r.close()
    app.dependency_overrides[get_repo] = _repo
    app.dependency_overrides[get_alias_map] = lambda: alias_map
    app.dependency_overrides[get_llm] = lambda: FakeLLM("A deterministic web summary of the alert events.")

    # Bound to this project's /p/{id}/ prefix, so the call sites below read
    # unchanged; "//path" escapes it for app-level routes.
    yield project_client(app, ctx)

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_index_serves_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "TrapTracker Report" in r.text


def test_index_offers_pdf_download_via_print(client):
    page = client.get("/").text
    assert 'id="pdf"' in page                             # the Download PDF button
    assert "window.print()" in page                       # native save-as-PDF, no server dep
    assert "@media print" in page                         # print stylesheet present
    assert "print-color-adjust: exact" in page            # colour (honesty callout, pie) survives
    assert "display: table-header-group" in page          # table headers repeat across pages


def test_species_lists_all_classes_with_counts_and_nonbio(client):
    sp = client.get("/api/species").json()["species"]
    keys = {s["canonical"] for s in sp}
    assert "Vulpes vulpes" in keys and "nonbio:Person" in keys      # full class list
    fox = next(s for s in sp if s["canonical"] == "Vulpes vulpes")
    assert fox["count"] == 3 and fox["query"] == "Vulpes vulpes"    # present in dataset
    person = next(s for s in sp if s["canonical"] == "nonbio:Person")
    assert person["count"] == 0 and person["is_binomial"] is False and person["query"] == "person"
    # Most classes are absent — the honest "31 classes, few present" story.
    assert sum(1 for s in sp if s["count"] == 0) > sum(1 for s in sp if s["count"] > 0)


def test_report_renders_html_with_honesty_and_table(client):
    html = client.post("/api/report", json={
        "species": "fox", "start": _iso(7), "end": _iso(0)}).text
    assert "red fox" in html and "Vulpes vulpes" in html
    assert "alert event" in html.lower()                 # Decision 4
    assert "validated" in html.lower()                   # Decision 7
    assert "<table" in html                              # markdown table rendered to HTML


def test_report_sanitises_untrusted_vlm_html(client):
    html = client.post("/api/report", json={
        "species": "fox", "start": _iso(7), "end": _iso(0)}).text
    assert "<script" not in html.lower()                 # no script ELEMENT reaches the page
    assert "cat near the feeder" in html                 # surrounding description text survives
    # CHANGED by the interpolation-time escape. This previously asserted
    # `alert('xss') not in html`, because nh3 deletes the CONTENT of a <script>
    # tag as well as the tag. Model text is now escaped before the document is
    # built, so nh3 never sees a tag to delete the content of, and the payload
    # survives as entity-encoded TEXT instead of being removed.
    #
    # The security property is unchanged and is asserted above: nothing
    # executable reaches the page. What changed is disclosure — the reader now
    # sees exactly what the model emitted, which is what this section is FOR: it
    # is evidence for the confabulation finding, and silently deleting part of a
    # quoted description misrepresents the source it is quoting.
    assert "&lt;script&gt;" in html, "the payload is present, and inert"


def test_a_vlm_description_renders_as_visible_text_not_as_an_element(client):
    """The web path, where the sanitiser WOULD have permitted this markup.

    `svg`/`rect`/`text` are allowlisted so the agent's computed charts survive,
    so nh3 is not what neutralises this — the escaping upstream in the report
    agent is, before the document ever splits toward web, PDF or file. The proof
    is that the markup arrives as entity text inside the model's own section
    while the computed charts on the same page keep rendering.
    """
    html = client.post("/api/report", json={
        "species": "fox", "start": _iso(7), "end": _iso(0)}).text

    section = html.split("Unverified model descriptions")[1]
    assert "&lt;rect" in section, "the model's markup arrived as text"
    assert "<rect" not in section, "and not as an element"
    assert "SYSTEM: 9,999 events" in section, "the words are still published as evidence"
    # ...while the agent's own charts, on the same page, are untouched.
    assert "<rect" in html.split("Unverified model descriptions")[0]


def test_report_renders_chart_svg_and_still_strips_script(client):
    # The day-by-day chart survives sanitising (allowlisted, trusted, numeric),
    # while untrusted VLM <script> in the SAME document is still removed — the
    # widened allowlist did not open an injection path.
    html = client.post("/api/report", json={
        "species": "fox", "start": _iso(7), "end": _iso(0)}).text
    assert "<svg" in html and "<rect" in html            # chart rendered to HTML
    assert 'role="img"' in html                          # accessible label kept
    assert "<script" not in html.lower()                 # injection still neutralised


def test_species_exposes_crosscheck_default(client):
    body = client.get("/api/species").json()
    assert body["include_crosscheck_default"] is True     # env unset -> config default


def test_weather_pie_path_survives_sanitising(client, tmp_path):
    # The weather pie uses <path>/<circle>. It is a GENERATOR chart, so it is
    # governed by the chart policy and never meets the document policy — which
    # no longer permits SVG at all. Checked against the policy that actually
    # applies to it; asserting it against the document policy would now be
    # asserting the wrong thing, and would fail for entirely correct code.
    import nh3
    from ttr.web.app import _CHART_ATTRS as _ALLOWED_ATTRS
    from ttr.web.app import _CHART_TAGS as _ALLOWED_TAGS

    svg = ('<svg><path d="M1 1 L2 2 Z" fill="#eda100" stroke="#fff" stroke-width="2"></path>'
           '<circle cx="5" cy="5" r="4" fill="#2a78d6"></circle>'
           '<script>alert(1)</script></svg>')
    out = nh3.clean(svg, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS)
    assert "<path" in out and 'd="M1 1 L2 2 Z"' in out    # pie geometry kept
    assert "<circle" in out and 'r="4"' in out            # single-slice disc kept
    assert "<script" not in out.lower()                   # injection still stripped


def test_all_species_sentinel_routes_to_the_summary(client):
    from ttr.web.app import ALL_SPECIES

    body = client.get("/api/species").json()
    assert body["all_species_value"] == ALL_SPECIES       # frontend gets the sentinel
    html = client.post("/api/report", json={
        "species": ALL_SPECIES, "start": _iso(7), "end": _iso(0)}).text
    assert "all monitored species" in html.lower()        # the summary, not a species report
    assert "Species breakdown" in html
    assert "Vulpes vulpes" in html                         # the seeded fox appears as a row
    assert "<table" in html


def _fox_report(client, **extra):
    return client.post("/api/report", json={
        "species": "fox", "start": _iso(7), "end": _iso(0), **extra}).text


def test_crosscheck_toggle_per_request(client):
    # Default / explicit true -> the cross-check section is shown.
    assert "did not corroborate" in _fox_report(client)
    assert "did not corroborate" in _fox_report(client, include_crosscheck=True)

    # Explicit false -> suppressed, but honestly disclosed (never silent).
    off = _fox_report(client, include_crosscheck=False)
    assert "did not corroborate" not in off
    assert "BioCLIP -> Meles meles" not in off
    assert "cross-check display is off" in off.lower()
    assert "still stored" in off.lower()
    assert "None flagged" not in off
    # Same DB, same read path: the honesty statements are present in both variants.
    assert "validated" in off.lower()


def test_ambiguous_species_returns_did_you_mean(client):
    html = client.post("/api/report", json={
        "species": "deer", "start": _iso(7), "end": _iso(0)}).text
    assert "did you mean" in html.lower()


def test_empty_window_is_honest_not_blank(client):
    html = client.post("/api/report", json={
        "species": "badger", "start": "2020-01-01", "end": "2020-01-31"}).text
    assert html.strip()                                  # not a blank page
    assert "alert event" in html.lower()                 # honesty language still present


def test_bad_date_range_returns_400(client):
    r = client.post("/api/report", json={"species": "fox", "start": _iso(0), "end": _iso(7)})
    assert r.status_code == 400
    assert "invalid date range" in r.text.lower()


# --------------------------------------------------------------------------- #
# KNOWN LIMITATION (not a failure): the render path lets model-authored markup
# render as a live element.
#
# The sanitiser allowlist permits svg/rect/text so the agent's computed charts
# survive rendering. The NARRATIVE model's prose passes through the same
# allowlist, so chart-shaped markup it authors renders as visual content beside
# the genuine charts. Appearance only — no computed figure, table row or
# generated chart is affected, which
# test_report.py::test_charts_and_figures_are_invariant_under_a_change_of_model_output
# proves independently.
#
# Surfaced by this project's own negative testing while diagnosing the GAP-3
# chart test. Deliberately DISCLOSED, not fixed — see Results/coverage_map.md §7.
# Strict, so it flips to a visible pass the day the render path is hardened.
# --------------------------------------------------------------------------- #
def test_narrative_model_markup_renders_inert_not_as_a_live_element(client, tmp_path):
    """Carried as a strict xfail from 2026-08-15 until this stage; now it passes.

    The document policy permits no SVG, so chart-shaped markup from either model
    is not sanitised into harmlessness — there is no tag for it to become. The
    genuine charts on the same page are unaffected because they never travel in
    the document at all.
    """
    from ttr.web.app import get_llm as _get_llm

    hostile = ('These alert events peaked sharply. '
               '<svg role="img"><rect fill="#1b5e3f" width="999"></rect>'
               '<text>peaking at 4242</text></svg>')
    app.dependency_overrides[_get_llm] = lambda: FakeLLM(hostile)
    html = client.post("/api/report", json={
        "species": "fox", "start": _iso(7), "end": _iso(0)}).text

    # The narrative really was published, so this is a containment check.
    assert "These alert events peaked sharply." in html
    # What SHOULD hold: the model's markup is neutralised, not rendered.
    assert "<rect" not in html.split("Summary")[1].split("<h2")[0]
    assert "4242" not in html


# --------------------------------------------------------------------------- #
# /print — the document the headless-Chrome PDF flow actually prints.
#
# Every report type must produce a real body here, and it must be the body the
# user is looking at: two of the three (single-species and all-species) put an LLM
# in the generation path, so regenerating for the PDF would cost a second model
# call and could return a differently-worded narrative than the screen showed.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_body_cache():
    """The rendered-body cache is module state, and these tests each use their own
    temp database — no test may serve another's cached body."""
    from ttr.web.app import _BODY_CACHE
    _BODY_CACHE.clear()
    yield
    _BODY_CACHE.clear()


def _print_types():
    from ttr.web.app import ALL_SPECIES, BNG_ALIGNED
    return [pytest.param(ALL_SPECIES, id="all-species"),
            pytest.param(BNG_ALIGNED, id="bng-aligned"),
            pytest.param("fox", id="single-species")]


@pytest.mark.parametrize("species", _print_types())
def test_print_page_carries_a_real_report_body(client, species):
    """The PDF source must be a populated document for ALL THREE types — not an
    empty shell whose content arrives later (that is what printed blank)."""
    import re

    r = client.get("/print", params={"species": species,
                                     "start": _iso(7), "end": _iso(0)})
    assert r.status_code == 200
    assert "<main id='report'>" in r.text
    body = r.text.split("<main id='report'>", 1)[1].rsplit("</main>", 1)[0]
    assert "<h1>" in body                                   # a report title
    assert "<h2>" in body                                   # at least one section
    text = re.sub(r"<[^>]+>", " ", body)
    assert len(text.split()) > 60                           # real prose, not a stub
    assert "alert events" in text.lower()                   # the honesty language


@pytest.mark.parametrize("species", _print_types())
def test_print_page_is_self_contained(client, species):
    """It is fetched by a separate browser process, so it must carry its own
    stylesheet and need nothing from the app page."""
    r = client.get("/print", params={"species": species,
                                     "start": _iso(7), "end": _iso(0)})
    assert r.text.startswith("<!doctype html>")
    assert "<style>" in r.text and "@page" in r.text


def test_print_prints_exactly_what_the_screen_rendered(client):
    """The PDF is of the report on screen — same bytes, and no second LLM call."""
    from ttr.web.app import get_llm as _get_llm

    llm = FakeLLM("A deterministic narrative summary for the print path.")
    app.dependency_overrides[_get_llm] = lambda: llm
    params = {"species": "fox", "start": _iso(7), "end": _iso(0)}

    screen = client.post("/api/report", json={**params, "include_crosscheck": True}).text
    calls_after_screen = len(llm.calls)
    assert calls_after_screen > 0                     # the screen path does use the LLM

    printed = client.get("/print", params={**params, "crosscheck": "true"}).text
    assert screen in printed                          # byte-identical body, not a re-run
    assert len(llm.calls) == calls_after_screen       # and the model was not called again


def test_print_still_generates_when_the_screen_rendered_nothing(client):
    """A cache miss (direct /print, expired entry, restarted server) must still
    produce the full report — the reuse is an optimisation, never a dependency."""
    from ttr.web.app import get_llm as _get_llm

    llm = FakeLLM("A deterministic narrative summary for the cold print path.")
    app.dependency_overrides[_get_llm] = lambda: llm
    r = client.get("/print", params={"species": "fox", "start": _iso(7), "end": _iso(0)})
    assert r.status_code == 200
    assert len(llm.calls) > 0                         # generated on demand
    assert "<h1>" in r.text


def test_print_toggle_state_is_part_of_the_identity(client):
    """Cross-check on and off are different reports, so one must never be printed
    in place of the other."""
    params = {"species": "fox", "start": _iso(7), "end": _iso(0)}
    client.post("/api/report", json={**params, "include_crosscheck": True})
    with_cc = client.get("/print", params={**params, "crosscheck": "true"}).text
    without_cc = client.get("/print", params={**params, "crosscheck": "false"}).text
    assert with_cc != without_cc


@pytest.mark.parametrize("species", _print_types())
def test_print_rejects_a_bad_date_range(client, species):
    """Validation is not skipped on the print route (nor bypassed by a cache hit)."""
    r = client.get("/print", params={"species": species,
                                     "start": _iso(0), "end": _iso(7)})
    assert r.status_code == 400
    assert "Invalid date range" in r.text


# --------------------------------------------------------------------------- #
# No token may reach a reader — including through the cache the PDF path reads.
# --------------------------------------------------------------------------- #
def test_no_token_survives_into_the_web_body(client):
    html = client.post("/api/report", json={
        "species": "fox", "start": _iso(7), "end": _iso(0)}).text
    assert "ttrchart" not in html
    assert "<svg" in html, "and the real charts did arrive"


def test_no_token_survives_into_the_print_body(client):
    """The print path serves the CACHED body. A token cached here becomes a PDF
    with a bare string where a chart should be, which is the failure mode worth
    naming: it would be produced by rendering for the screen first and reusing.
    """
    client.post("/api/report", json={
        "species": "fox", "start": _iso(7), "end": _iso(0)})      # populate the cache
    printed = client.get(f"/print?species=fox&start={_iso(7)}&end={_iso(0)}").text

    assert "ttrchart" not in printed, "an unsubstituted token reached the print document"
    assert "<svg" in printed


def test_the_cached_body_is_stored_already_substituted(client):
    """Directly, so the guarantee does not rest on the print route alone."""
    from ttr.web.app import _BODY_CACHE

    client.post("/api/report", json={
        "species": "fox", "start": _iso(7), "end": _iso(0)})
    assert _BODY_CACHE, "precondition: something was cached"
    for body in _BODY_CACHE.values():
        assert "ttrchart" not in body, "the cache holds an unsubstituted token"
