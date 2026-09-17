"""The ingest page: what it must show, and what it must never claim.

The page is JS-driven — the run states are painted from `/api/ingest/state` —
so these assert the markup and the client's invariants at the source level, the
way `test_web_create` asserts the creation form's POST-not-GET. The rendered
cascade is covered separately by `tests/test_web_contrast.py`.
"""

from __future__ import annotations

import re

import pytest

from ttr.web.ingest_page import INGEST_PAGE
from ttr.web.picker import PROJECT_SLOT, banner


@pytest.fixture
def page() -> str:
    return INGEST_PAGE.replace(PROJECT_SLOT, banner("Example Site", "a1b2c3d4-0000"))


def _script(page: str) -> str:
    return page[page.index("// Every value rendered below"):page.index("</script>")]


def _code(page: str) -> str:
    """The script with its comments stripped.

    The comment forbidding `innerHTML` contains the word `innerHTML`, so a
    naive search for the sink finds the rule against it.
    """
    lines = _script(page).split("\n")
    return "\n".join(l for l in lines if not l.lstrip().startswith("//"))


def _flat(text: str) -> str:
    """Whitespace-collapsed, so a sentence that wraps in the source still
    reads as one string."""
    return re.sub(r"\s+", " ", text)


# --------------------------------------------------------------------------- #
# The promises this page makes about upstream
# --------------------------------------------------------------------------- #
def test_the_upstream_token_is_rendered_exactly_as_stored(page):
    """No re-resolution, no correction, no case-folding, no prettifying.

    The token goes from the payload into `textContent` and nothing happens to it
    on the way. This asserts the absence of a transform, which is the kind of
    thing that gets added later by someone making labels "nicer".
    """
    s = _script(page)
    line = next(l for l in s.split("\n") if "'lab'" in l)

    assert "p.upstream_label" in line
    for transform in (".replace(", ".toLowerCase(", ".toUpperCase(", ".split("):
        assert transform not in line, f"the upstream token is transformed: {line.strip()}"


def test_the_page_never_claims_the_detection_system_was_consulted(page):
    flat = _flat(page)

    assert "the detection system is never called" in flat
    assert "its class token is never overwritten" in flat
    assert "the upstream class token is never overwritten" in flat


# --------------------------------------------------------------------------- #
# A cross-check difference is an outcome, not a problem
# --------------------------------------------------------------------------- #
def test_agree_and_differ_carry_the_same_chip(page):
    """Two neutral outcomes of one check. If `differs` were styled as a warning,
    a reader could infer a verdict from the colour — which is exactly what the
    project forbids, here as in the reports."""
    s = _script(page)
    # The whole chip builder, so the class and the text are judged together even
    # though the `differs` branch assembles its text over several lines.
    fn = s[s.index("function crossCheckChip"):s.index("function appendCard")]

    assert fn.count("chip chip-x") == 4, "every cross-check outcome shares one chip"
    assert "chip-warn" not in fn, "a cross-check outcome is styled as a problem"
    # And the warning chip is reserved for the two things that ARE problems.
    warn_uses = re.findall(r"'chip chip-warn', '([^']+)'", s)
    assert sorted(warn_uses) == ["enrichment error", "not stored"], warn_uses


def test_a_disagreement_carries_how_far_off_it_was(page):
    """`taxonomic_distance` is already computed and stored; surfacing it turns a
    binary into something a reader can weigh — a near miss and a read of the
    vegetation are not the same finding."""
    s = _script(page)

    assert "p.taxonomic_distance" in s
    assert "cross-check differs" in s
    assert "not_evaluable" in s, "the excluded-denominator case is named too"


# --------------------------------------------------------------------------- #
# Errors: shown, and the two kinds kept apart
# --------------------------------------------------------------------------- #
def test_a_stored_row_with_a_stage_error_and_an_unstored_event_are_different(page):
    """`ok:true` with a failed enricher is a row that EXISTS carrying its error.
    `ok:false` is a record that was not stored at all. Merging them would either
    overstate what is in the database or understate what failed."""
    s = _script(page)

    assert "The row is stored with its error: " in s
    assert "This event was not stored. " in s
    # Counted apart, never summed into one figure.
    assert "runCount.errored += 1" in s
    assert "runCount.notStored += 1" in s
    assert "'stored with an error'" in s and "'not stored'" in s


def test_the_counters_are_counted_from_the_run_not_estimated(page):
    """Three counters, each a count of events actually received. Nothing here
    reads a total the run cannot know."""
    s = _script(page)

    for label in ("'images this run'", "'rows written'", "'stored with an error'"):
        assert label in s
    # The two the payload cannot support are absent rather than guessed.
    for absent in ("messages seen", "new to this project", "skipped, already seen"):
        assert absent not in page, f"{absent!r} cannot be counted from the payload"


# --------------------------------------------------------------------------- #
# Progress: no invented denominator
# --------------------------------------------------------------------------- #
def test_the_progress_bar_is_indeterminate_and_states_no_total(page):
    """`run_once` iterates a generator; the number of messages a poll will yield
    is not known before it finishes. Any "n of m" here would be invented."""
    s = _script(page)

    assert "bar bar-indet" in s
    assert "aria-busy" in s
    assert "'Processing image '" in s
    for invented in ("' of '", "aria-valuemax", "aria-valuenow", "percent"):
        assert invented not in s, f"a denominator crept in: {invented}"


def test_the_dropped_events_line_repeats_what_the_buffer_reports(page):
    """The ring buffer says how many entries it evicted. A view that knows it is
    incomplete should say so rather than looking whole."""
    s = _script(page)

    assert "d.dropped > 0" in s
    assert "earlier events in this run are no longer shown" in s


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #
def test_stop_does_not_exist_until_a_run_does(page):
    """It is built into the running panel, not rendered disabled in the idle
    one — a control that cannot do anything should not be on screen."""
    body = page[page.index('<section class="runcard">'):page.index("</script>")]
    markup = body[:body.index("<script")] if "<script" in body else body

    assert 'id="stop"' not in markup, "Stop is in the idle markup"
    assert "stop.id = 'stop'" in _script(page), "Stop is never created"


def test_fetch_and_poll_are_disabled_while_a_run_is_in_flight(page):
    s = _script(page)

    assert "$('once').disabled = running" in s
    assert "$('loop').disabled = running" in s
    # And a missing credential disables them too (§5).
    assert "|| !hasCredential" in s


def test_no_credential_names_the_command_that_stores_one(page):
    s = _script(page)

    assert "No mailbox credential stored" in s
    assert "ttr project set-password" in s


# --------------------------------------------------------------------------- #
# The page's standing safety properties
# --------------------------------------------------------------------------- #
def test_nothing_is_ever_written_as_markup(page):
    """Email-derived text reaches this page as JSON and is rendered through
    `textContent` only. `innerHTML` anywhere here would be a hole."""
    code = _code(page)

    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in code, f"{sink} used on a page that renders untrusted text"


def test_a_thumbnail_is_only_ever_a_data_uri(page):
    """The server builds the thumbnail; anything else gets the placeholder, so a
    payload cannot talk the page into fetching a URL."""
    s = _script(page)

    assert "p.thumb.startsWith('data:image/')" in s


def test_the_page_asks_the_network_for_nothing(page):
    for attr in ("src", "href", "action", "poster"):
        for value in re.findall(rf'{attr}="([^"]*)"', page):
            assert not value.startswith(("http://", "https://", "//")), value
    for forbidden in ("fonts.googleapis", "fonts.gstatic", "cdn.", "unpkg",
                      "jsdelivr", "@import"):
        assert forbidden not in page, forbidden


def test_the_project_is_named_on_the_page(page):
    assert "Example Site" in page
    assert "a1b2c3d4" in page


# --------------------------------------------------------------------------- #
# The runner's stopped flag
# --------------------------------------------------------------------------- #
def test_a_stopped_run_is_distinguishable_from_a_finished_one():
    """`stop()` sets "stopping" and the loop then finishes into "done", so the
    terminal state alone cannot tell the two apart — and the counts under a
    stopped run are partial. The flag is display-only; nothing branches on it."""
    from ttr.web.ingest import IngestRunner

    r = IngestRunner()
    assert r.snapshot()["stopped_by_user"] is False

    with r._lock:                      # simulate a live run without starting one
        r._state = "running"
    r.stop()

    assert r.snapshot()["stopped_by_user"] is True
    assert r.snapshot()["state"] == "stopping"


def test_the_page_labels_a_stopped_runs_counts_as_partial(page):
    s = _script(page)

    assert "d.stopped_by_user" in s
    assert "The counts below are partial" in s
    assert "'Stopped'" in s
