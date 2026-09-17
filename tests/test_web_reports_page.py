"""The monitoring-reports page, Phase 1: the page before a report exists.

Covers the controls card, the window presets, the summary strip and the waiting
state. Nothing here generates a report or touches the generator — that is the
point of Phase 1, and `test_the_page_computes_no_figures_of_its_own` asserts the
boundary rather than trusting it.
"""

from __future__ import annotations

import datetime as dt
import html as _html
import json
import re

import pytest

from ttr.projects import paths as ppaths
from ttr.projects.registry import Registry
from ttr.projects.service import context_for, create_project, set_site
from ttr.storage.repository import PersistedEvent
from ttr.web import app as web_app
from ttr.web.picker import PROJECT_SLOT, banner

UTC = dt.timezone.utc


def _render(ctx) -> str:
    """Exactly the two substitutions the route makes."""
    return (web_app._PAGE
            .replace(PROJECT_SLOT, banner(ctx.name, ctx.id))
            .replace(web_app._WINDOW_SLOT, web_app._window_payload(ctx.db_path)))


def _seed(ctx, days, per_day=4, binomial="Columba palumbus"):
    repo = ctx.open_repo()
    try:
        for day in days:
            for i in range(per_day):
                repo.upsert_event(PersistedEvent(
                    source_type="email",
                    source_message_id=f"<{binomial}-{day}-{i}@x>",
                    ingested_at_utc=dt.datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
                    canonical_binomial=binomial,
                    canonical_is_binomial=not binomial.startswith("nonbio:"),
                    event_time_utc=dt.datetime(2026, 7, day, 9, 0, tzinfo=UTC),
                    images_present="both", field_provenance={}))
    finally:
        repo.close()


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with a short, gappy, historical corpus."""
    root = tmp_path / "root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    m, _ = create_project("Example Site", "a@gmail.com", root=root,
                          skip_connection_test=True)
    set_site(m.id, name="Example Village, UK", root=root)
    ctx = context_for(Registry.load(root).get(m.id), root)
    _seed(ctx, days=(1, 2, 3, 6, 7))          # 4 and 5 never delivered
    return ctx


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A project with nothing stored at all."""
    root = tmp_path / "bare-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    m, _ = create_project("Fresh Deployment", "b@gmail.com", root=root,
                          skip_connection_test=True)
    return context_for(Registry.load(root).get(m.id), root)


def _payload(html: str):
    m = re.search(r'<script type="application/json" id="window-data">(.*?)</script>',
                  html, re.S)
    assert m, "the window payload block is missing"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------- #
# The window payload
# --------------------------------------------------------------------------- #
def test_the_window_payload_is_read_only(project):
    """The strip's read must not be able to write.

    `get_repo` opens the project read-write because generating a report may
    write; this read has no business being able to, and a separate `mode=ro`
    handle is the enforceable version of that intention.
    """
    import sqlite3

    opened, real = [], sqlite3.connect

    def spy(database, *args, **kwargs):
        opened.append(str(database))
        return real(database, *args, **kwargs)

    before = project.db_path.read_bytes()
    sqlite3.connect = spy
    try:
        web_app._window_payload(project.db_path)
    finally:
        sqlite3.connect = real

    assert opened, "the payload did not open the database"
    assert all("mode=ro" in c for c in opened), opened
    assert project.db_path.read_bytes() == before


def test_the_payload_holds_days_with_rows_and_invents_no_zeroes(project):
    """Days 4 and 5 had no delivery. They are ABSENT, not zero — the same rule
    the projects-page chart follows, for the same reason: the email source cannot
    tell "no wildlife" from "nothing delivered"."""
    days = [d for d, _b, _n in _payload(_render(project))]

    assert days == ["2026-07-01", "2026-07-02", "2026-07-03",
                    "2026-07-06", "2026-07-07"]
    assert "2026-07-04" not in days and "2026-07-05" not in days


def test_a_project_with_nothing_stored_yields_an_empty_payload(bare):
    """Empty, not null: the database was readable and holds no rows. The page
    distinguishes the two — see the next test."""
    assert _payload(_render(bare)) == []


def test_an_unreadable_corpus_yields_null_so_the_strip_can_hide(project):
    """An unreadable database is not an empty one, and a strip reading
    "0 alert events" would assert the second. `null` makes the page hide it."""
    project.db_path.unlink()

    assert web_app._window_payload(project.db_path) == "null"
    assert _payload(_render(project)) is None


# --------------------------------------------------------------------------- #
# The controls
# --------------------------------------------------------------------------- #
def test_the_presets_drive_the_existing_parameters_and_add_none(project):
    """Four presets, as real radios, writing to #start and #end and nothing else.

    The report request model has exactly four fields; presets are new UI over two
    of them. A new parameter name here would mean a new endpoint contract, which
    Phase 1 does not have.
    """
    body = _render(project)

    for value in ("7", "30", "all", "custom"):
        assert f'<input type="radio" name="preset" value="{value}"' in body
    assert 'role="radiogroup"' in body
    assert 'value="all" checked' in body, "All records is the default preset"

    # The presets only ever assign to these two fields.
    assert "$('start').value" in body and "$('end').value" in body
    # And the request model is unchanged.
    assert set(web_app.ReportRequest.model_fields) == {
        "species", "start", "end", "include_crosscheck"}


def test_the_cross_check_keeps_its_id_and_gains_a_described_by(project):
    """It is a switch now, not a checkbox with a label. Same control, same id —
    the page's own script and every existing test reach it by that id."""
    body = _render(project)

    assert 'type="checkbox" id="crosscheck"' in body
    assert 'aria-describedby="xcheck-help"' in body
    assert "An independent taxonomic read that\n             never sees the upstream label." in body \
        or "never sees the upstream label" in body


def test_every_control_is_a_real_control_not_a_styled_span(project):
    """The mockup draws the segmented control as static spans. Spans do not take
    focus, do not answer the arrow keys and do not submit."""
    body = _render(project)
    segmented = re.search(r'<div class="segmented".*?</div>', body, re.S).group(0)

    assert segmented.count("<input type=\"radio\"") == 4
    assert "<button" not in segmented
    # And the focus ring is drawn for them.
    assert ".segmented input:focus-visible + span" in body


# --------------------------------------------------------------------------- #
# The waiting state and the standing statements
# --------------------------------------------------------------------------- #
def test_the_standing_statements_are_imported_not_retyped(project):
    """The three claims appear verbatim, from the report stage's own constants.

    This is the test that would have caught the mockup's copy. Its
    "times are when the alert was sent, not when the capture happened" is the
    PRE-amendment Decision 3 claim; `_SEND_TIME_CLAIM` records the amendment —
    events are dated by capture time where the filename decoded, by send time
    only as a flagged fallback. A retyped statement drifts silently; an imported
    one cannot.
    """
    from ttr.agents.report import (COUNT_CAVEAT, _DECISION7_CLAIM,
                                   _SEND_TIME_CLAIM)

    body = _render(project)
    text = _html.unescape(re.sub(r"<[^>]+>", "", body))

    for claim in (COUNT_CAVEAT, _SEND_TIME_CLAIM, _DECISION7_CLAIM):
        assert claim in text, f"missing verbatim: {claim[:60]}…"

    # The superseded wording must not be anywhere on the page.
    assert "times are when the alert was sent rather than when the capture" not in text


def test_the_waiting_state_carries_the_pipeline_and_the_fence(project):
    body = _render(project)

    for head in ("Stored rows", "Figures", "Report"):
        assert f">{head}</p>" in body
    assert "no figure changes past this line" in body
    assert "Every report states, unchanged" in body


def test_the_waiting_panel_keeps_the_id_the_script_toggles(project):
    """It is `.waiting` now rather than `.placeholder`, but the id is what the
    page's own script hides when a report renders and restores when one fails."""
    body = _render(project)

    assert 'class="waiting" id="placeholder"' in body
    assert "$('placeholder').hidden = true" in body
    assert "$('placeholder').hidden = false" in body


# --------------------------------------------------------------------------- #
# The boundary Phase 1 must not cross
# --------------------------------------------------------------------------- #
def test_the_page_computes_no_figures_of_its_own(project):
    """The summary strip is a PREVIEW of the window, labelled as such, and
    nothing that generates a report reads it.

    The strip is a query; a report's figures come from the generator. The two
    must never be confusable, so the label is asserted here and the payload is
    asserted to be read only by the strip.
    """
    body = _render(project)

    assert "not report output" in body
    # The payload variable is read by the preview and by the preset defaults —
    # never by the submit path.
    submit = body[body.index("$('f').addEventListener"):] if "$('f').addEventListener" in body \
        else body[body.index("addEventListener('submit'"):]
    assert "WINDOW_DATA" not in submit, (
        "the generate path reads the preview payload; figures must come from the "
        "generator")


def test_the_page_asks_the_network_for_nothing(project):
    """No font CDN, no chart library, no icons package — same guarantee the
    projects page carries."""
    body = _render(project)

    for attr in ("src", "href", "action", "poster"):
        for value in re.findall(rf'{attr}="([^"]*)"', body):
            assert not value.startswith(("http://", "https://", "//")), value
    for forbidden in ("fonts.googleapis", "fonts.gstatic", "cdn.", "unpkg",
                      "jsdelivr", "@import"):
        assert forbidden not in body, forbidden


def test_the_project_is_named_on_the_page(project):
    """Which project's records these are, visible in the header — the Stage 6
    cache leak served one project's report under another's URL."""
    body = _render(project)

    assert "Example Site" in body
    assert project.id[:8] in body
    assert 'class="allproj" href="/"' in body


def test_a_hidden_button_is_actually_hidden(project):
    """Regression: Download PDF was offered before there was anything to download.

    `theme.BASE` sets `display: inline-block` on `button`, and a type selector
    outranks the user agent's `[hidden] { display: none }`. So `<button hidden>`
    stayed on screen — a control that does nothing, which is worse than no
    control. The markup and the rule that makes the markup mean something are
    asserted together, because either without the other is the bug.

    Same defect as `.drawer[hidden]` on the projects page. Both are here so that
    fixing one and not the other cannot pass.
    """
    from ttr.web import picker, theme

    body = _render(project)

    assert 'id="pdf" class="secondary" hidden' in body
    assert "button[hidden], .btn[hidden] { display: none !important; }" in theme.BASE
    assert ".drawer[hidden] { display: none; }" in picker._STYLE
