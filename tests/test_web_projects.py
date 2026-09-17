"""Stage 6: the web layer is project-scoped, and the cache leak is closed.

Nothing here calls Ollama or the network: the LLM is a FakeLLM and the ingest
pipeline is a supplied factory.
"""

from __future__ import annotations

import datetime as dt
import html as _html
import threading
import time

import pytest

from ttr.projects import paths as ppaths
from ttr.projects.lock import holder, ingest_lock
from ttr.projects.registry import Registry
from ttr.projects.service import context_for, create_project, set_archived
from ttr.species.aliases import SpeciesAliasMap
from ttr.web.app import IngestStartRequest, app, get_alias_map, get_llm
from ttr.web.app import get_pipeline_factory
from ttr.web.ingest import RUNNER

from conftest import (FakeLLM, mailbox_factory, make_persisted_event,
                      project_client)

UTC = dt.timezone.utc
START, END = "2026-07-01", "2026-07-31"

ALIASES = SpeciesAliasMap({
    "Columba palumbus": {"raw_tokens": ["ColumbaPalumbus"],
                         "common_names": ["wood pigeon"]},
    "Vulpes vulpes": {"raw_tokens": ["VulpesVulpes"], "common_names": ["red fox"]},
    "Meles meles": {"raw_tokens": ["MelesMeles"], "common_names": ["european badger"]},
})
ALPHA_ONLY, BETA_ONLY = "Vulpes vulpes", "Meles meles"


@pytest.fixture(autouse=True)
def _clean():
    app.dependency_overrides.clear()
    import ttr.web.app as web_app
    web_app._BODY_CACHE.clear()
    yield
    app.dependency_overrides.clear()
    web_app._BODY_CACHE.clear()
    # Leave no run adopted by the next test.
    RUNNER._state = "idle"
    RUNNER._project_id = None
    RUNNER._project_name = None


def _seed(ctx, mix, day):
    repo = ctx.open_repo()
    try:
        n = 0
        for binomial, count in mix.items():
            for i in range(count):
                repo.upsert_event(make_persisted_event(
                    f"<{ctx.slug}-{n}@x>", canonical_binomial=binomial,
                    event_time=dt.datetime(2026, 7, day, 9, 0, tzinfo=UTC)
                    + dt.timedelta(days=i % 5),
                    display_common_name=ALIASES.display_common_name(binomial),
                    upstream_confidence=0.9, vlm_ok=True))
                n += 1
    finally:
        repo.close()


@pytest.fixture
def two(tmp_path, monkeypatch):
    root = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    a_m, _ = create_project("Alpha Site", "a@gmail.com", root=root,
                            skip_connection_test=True)
    b_m, _ = create_project("Beta Site", "b@gmail.com", root=root,
                            skip_connection_test=True)
    reg = Registry.load(root)
    a, b = context_for(reg.get(a_m.id), root), context_for(reg.get(b_m.id), root)
    _seed(a, {"Columba palumbus": 12, ALPHA_ONLY: 4}, day=2)
    _seed(b, {"Columba palumbus": 3, BETA_ONLY: 7}, day=12)

    app.dependency_overrides[get_alias_map] = lambda: ALIASES
    app.dependency_overrides[get_llm] = lambda: FakeLLM("A deterministic summary.")
    return a, b, root


def _client(ctx):
    return project_client(app, ctx)


def _text(html: str) -> str:
    """The page as a reader sees it: tags dropped, whitespace collapsed.

    A phrase on screen is not necessarily a contiguous run of characters in the
    source — "798 alert events" is one sentence to a reader and a number, a span
    and a unit to the parser. Asserting on raw HTML made these tests fail when the
    number was given its own styling, which is a fact about the stylesheet and not
    about what the picker says. What the picker SAYS is the thing worth pinning.
    """
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))


def _all_species_params():
    import ttr.web.app as web_app
    return {"species": web_app.ALL_SPECIES, "start": START, "end": END}


# --------------------------------------------------------------------------- #
# The leak, tested on the path that actually has it
# --------------------------------------------------------------------------- #
def test_screen_in_one_project_then_print_in_another_does_not_leak(two):
    """The real mechanism, per Stage 4: `/api/report` only WRITES the cache;
    `/print` is the sole reader (reuse_rendered). So the exposure runs
    screen -> print, and produces a downloadable PDF of the wrong site."""
    a, b, _ = two
    params = _all_species_params()

    # A renders on screen — this is what populated the shared entry before.
    a_body = _client(a).post("/api/report", json=params).text
    assert ALPHA_ONLY in a_body and BETA_ONLY not in a_body

    # B prints. It must get B's report.
    printed = _client(b).get("/print", params=params).text
    assert BETA_ONLY in printed
    assert ALPHA_ONLY not in printed


def test_the_cache_holds_one_entry_per_project(two):
    """Asserted on the KEY, not on a symptom, so it survives any future change
    to which paths read the cache."""
    import ttr.web.app as web_app

    a, b, _ = two
    params = _all_species_params()
    _client(a).post("/api/report", json=params)
    _client(b).post("/api/report", json=params)

    assert len(web_app._BODY_CACHE) == 2
    project_ids = {key[0] for key in web_app._BODY_CACHE}
    assert project_ids == {a.id, b.id}


def test_the_print_url_the_server_builds_carries_the_project(two, monkeypatch):
    """The PDF route navigates a headless browser to a URL it builds itself. The
    project has to be in that URL or the browser prints the wrong report."""
    import ttr.web.pdf as pdf_module

    a, _, _ = two
    seen = {}

    def fake_render(url, **kw):
        seen["url"] = url
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(pdf_module, "render_url_to_pdf", fake_render)
    r = _client(a).get("/api/report.pdf",
                       params={"species": "wood pigeon", "start": START, "end": END})

    assert r.status_code == 200
    assert f"/p/{a.id}/print?" in seen["url"]
    assert seen["url"].startswith("http://127.0.0.1:")     # still pinned to us


# --------------------------------------------------------------------------- #
# 404s
# --------------------------------------------------------------------------- #
def test_an_unknown_project_id_is_404(two):
    a, _, _ = two
    client = _client(a)
    for path in ("//p/nope/", "//p/nope/api/species",
                 "//p/nope/api/ingest/preflight"):
        assert client.get(path).status_code == 404, path


def test_an_api_client_still_gets_json_for_a_404(two):
    """The page below must not change what a program receives.

    Every client that did not ASK for HTML — the test client, curl, anything
    fetching the API — keeps the JSON body it always had.
    """
    a, _, _ = two
    r = _client(a).get("//p/nope/api/species")

    assert r.status_code == 404
    assert r.json()["detail"] == "no such project: nope"
    assert "<html" not in r.text


def test_a_browser_gets_a_readable_page_for_a_404(two):
    """A mistyped project id in the address bar used to render as a raw JSON
    object: proof the server works, and no hint about what to do next."""
    a, _, _ = two
    r = _client(a).get("//p/nope/", headers={"accept": "text/html"})

    assert r.status_code == 404
    assert "<html" in r.text
    assert "no such project: nope" in r.text       # the same detail, not a vaguer one
    assert 'href="/"' in r.text                    # and the way back


def test_the_error_page_escapes_the_id_it_echoes(two):
    """The id comes from the URL. It is echoed into the page, so it is escaped —
    not because a route could currently deliver markup here, but because that is
    exactly the assumption that quietly stops being true."""
    a, _, _ = two
    r = _client(a).get("//p/%3Cimg%20src=x%20onerror=alert(1)%3E/",
                       headers={"accept": "text/html"})

    assert r.status_code == 404
    assert "<img" not in r.text
    assert "onerror" not in r.text or "&lt;img" in r.text


def test_an_archived_project_is_404_not_a_fallback(two):
    """Archiving says a project should not be reachable by ordinary selection.
    Quietly serving it anyway would make that meaningless."""
    a, b, root = two
    assert _client(b).get("/api/species").status_code == 200

    set_archived(b.id, True, root=root)

    r = _client(b).get("/api/species")
    assert r.status_code == 404
    assert "archived" in r.json()["detail"]
    # ...and the other project is unaffected.
    assert _client(a).get("/api/species").status_code == 200


def test_the_root_lists_projects(two):
    """Was `..._and_offers_no_creation` until 2026-09-11.

    It asserted the picker had no form, because a mailbox app password must not be
    POSTed to a server any local process could reach. That exclusion was
    CONDITIONAL on there being no authentication, and the condition was met: the
    UI is now behind a session token (`test_ui_auth.py`), so the form exists and
    `test_web_create.py` covers it.

    The listing half of the assertion is unchanged and is what this test is now
    for. Renamed rather than deleted so the change of rule is visible in the
    history rather than looking like coverage that quietly evaporated.
    """
    a, b, _ = two
    body = _client(a).get("//").text

    assert f"/p/{a.id}/" in body and f"/p/{b.id}/" in body
    assert "Alpha Site" in body and "Beta Site" in body


def test_a_project_url_without_a_trailing_slash_redirects_to_one(two):
    """The pages use RELATIVE urls (`api/species`, `href="ingest"`), which only
    resolve correctly from `/p/{id}/`. From `/p/{id}` they would resolve against
    `/p/`, and every fetch would 404. FastAPI redirects by default; verified here
    rather than assumed, because the whole frontend depends on it."""
    a, _, _ = two
    client = _client(a)

    r = client.get(f"//p/{a.id}", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith(f"/p/{a.id}/")

    followed = client.get(f"//p/{a.id}", follow_redirects=True)
    assert followed.status_code == 200
    assert followed.request.url.path.endswith("/")


def test_the_root_hides_archived_projects_behind_a_toggle(two):
    """Hidden, not gone. An archived project still refuses to open (see
    `test_an_archived_project_is_404_not_a_fallback`); the picker's job is to
    keep it out of the way without pretending it does not exist."""
    a, b, root = two
    set_archived(b.id, True, root=root)
    body = _client(a).get("//").text

    assert "Alpha Site" in body
    main, _, folded = body.partition('<details class="archived">')
    assert "Beta Site" not in main                    # not in the list proper
    assert "Beta Site" in folded                      # but reachable
    assert "Show 1 archived project</summary>" in folded


# --------------------------------------------------------------------------- #
# Ingest scoping
# --------------------------------------------------------------------------- #
def test_the_start_request_has_no_project_field():
    """The project arrives as a PATH parameter. A body field would be a second,
    forgeable source for the same fact — asserted on the model so the rule is
    enforced rather than remembered."""
    fields = set(IngestStartRequest.model_fields)
    assert fields == {"mode", "level"}
    for forbidden in ("project", "project_id", "db_path", "db", "database"):
        assert forbidden not in fields


def test_ingest_state_409s_for_another_projects_run(two):
    """Otherwise B's page renders A's live log and image strip as its own."""
    a, b, _ = two

    RUNNER._state = "running"
    RUNNER._project_id = a.id
    RUNNER._project_name = a.name
    try:
        assert _client(a).get("/api/ingest/state").status_code == 200

        r = _client(b).get("/api/ingest/state")
        assert r.status_code == 409
        body = r.json()
        assert body["running_project_id"] == a.id
        assert "Alpha Site" in body["error"]
        assert body["events"] == []            # never another project's events
    finally:
        RUNNER._state = "idle"
        RUNNER._project_id = None


def test_ingest_stop_409s_for_another_projects_run(two):
    a, b, _ = two
    RUNNER._state = "running"
    RUNNER._project_id = a.id
    RUNNER._project_name = a.name
    try:
        assert _client(b).post("/api/ingest/stop").status_code == 409
    finally:
        RUNNER._state = "idle"
        RUNNER._project_id = None


def test_the_snapshot_names_its_project(two):
    a, _, _ = two
    RUNNER._project_id, RUNNER._project_name = a.id, a.name
    snap = RUNNER.snapshot()
    assert snap["project_id"] == a.id
    assert snap["project_name"] == a.name


# --------------------------------------------------------------------------- #
# One lock, shared with the CLI
# --------------------------------------------------------------------------- #
def test_the_web_trigger_refuses_while_the_cli_holds_the_lock(two):
    """The in-process IngestBusy guard cannot see `ttr run` at all. The on-disk
    lock is what makes the CLI and the web contend — the case that produced 476
    orphan image directories."""
    a, _, _ = two

    def _factory():                       # never reached; the lock refuses first
        def _f(on_event):
            raise AssertionError("the job should not have started")
        return _f

    app.dependency_overrides[get_pipeline_factory] = _factory

    with ingest_lock(a.dir):              # exactly what `ttr run` holds
        r = _client(a).post("/api/ingest/start", json={"mode": "once"})

    assert r.status_code == 409
    error = r.json()["error"]
    assert "an ingest is already running for this project" in error
    assert "pid " in error


def test_the_web_run_releases_the_lock_when_the_job_finishes(two):
    a, _, _ = two
    done = threading.Event()

    class _Bundle:
        poll_seconds = 1

        class pipeline:
            @staticmethod
            def run_once():
                done.set()
                return 0

        def close(self):
            pass

    def _factory():
        def _f(on_event):
            return _Bundle()
        return _f

    app.dependency_overrides[get_pipeline_factory] = _factory
    assert _client(a).post("/api/ingest/start",
                           json={"mode": "once"}).status_code == 202
    assert done.wait(timeout=20)

    # The lock FILE is permanent (`lock.py`); what the job's finally releases is
    # the OS lock on it.
    for _ in range(100):                  # let the job's finally run
        if holder(a.dir) is None:
            break
        time.sleep(0.05)
    assert holder(a.dir) is None
    # ...and a `ttr run` could now take it.
    with ingest_lock(a.dir):
        pass


def _archive(root, project_id):
    registry = Registry.load(root)
    registry.update(project_id, archived=True)
    registry.save()


def test_the_web_ingest_puts_every_visible_project_in_credential_scope(two, monkeypatch):
    """One server reaches every project, and the project arrives in the URL, not
    with the password. An unscoped password is therefore in scope for all of them."""
    a, b, root = two
    seen = {}

    def fake_build(ctx, **kwargs):
        seen["projects_in_scope"] = kwargs.get("projects_in_scope")

    monkeypatch.setattr("ttr.pipeline.build_pipeline_for", fake_build)

    get_pipeline_factory(a)(None)
    assert seen["projects_in_scope"] == 2

    _archive(root, b.id)
    get_pipeline_factory(a)(None)
    assert seen["projects_in_scope"] == 1


def test_preflight_does_not_offer_a_password_the_run_would_refuse(two, monkeypatch):
    a, b, root = two
    monkeypatch.setenv("TTR_IMAP_PASSWORD", "exported-for-one-mailbox")

    data = _client(a).get("/api/ingest/preflight").json()
    assert data["credential_source"] is None
    assert data["has_credential"] is False

    _archive(root, b.id)
    data = _client(a).get("/api/ingest/preflight").json()
    assert data["credential_source"] == "environment (TTR_IMAP_PASSWORD)"


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
def test_preflight_answers_for_the_project_in_the_url(two):
    a, b, _ = two
    a_data = _client(a).get("/api/ingest/preflight").json()
    b_data = _client(b).get("/api/ingest/preflight").json()

    assert a_data["project_id"] == a.id and a_data["project_name"] == "Alpha Site"
    assert b_data["project_id"] == b.id and b_data["project_name"] == "Beta Site"
    # Each reports its own row count — 16 vs 10.
    assert a_data["db_rows"] == 16
    assert b_data["db_rows"] == 10
    # Still basenames, still no credential value.
    assert a_data["db_path"] == "detections.sqlite"
    assert a_data["has_credential"] is False
    assert a.mailbox.imap_user not in str(a_data)


# --------------------------------------------------------------------------- #
# The picker
# --------------------------------------------------------------------------- #
def test_the_picker_shows_what_distinguishes_one_project_from_another(two):
    """Name, site, how much is stored, over what dates, whether a credential
    exists and which species table is in force — the facts you would otherwise
    have to open each project to discover."""
    a, b, root = two
    body = _client(a).get("//").text

    assert "Alpha Site" in body and "Beta Site" in body
    assert f"/p/{a.id}/" in body and f"/p/{b.id}/" in body
    assert a.id[:8] in body and b.id[:8] in body

    assert "16 alert events" in _text(body)          # Alpha: 12 + 4
    assert "10 alert events" in _text(body)          # Beta:   3 + 7
    assert "2026-07-02 to 2026-07-06" in body         # Alpha's stored span
    assert "2026-07-12 to 2026-07-16" in body         # Beta's, and they differ

    assert "no mailbox credential" in body            # neither has one stored
    assert "alias table: bundled example" in body     # and neither has a table


def test_the_picker_opens_no_project_database_for_writing(two):
    """Listing a project must never migrate or stamp it. The picker reads with
    `mode=ro`, so a database it has never seen is left exactly as it was."""
    import sqlite3

    a, b, _ = two
    before = b.db_path.read_bytes()

    connected = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        connected.append(str(database))
        return real(database, *args, **kwargs)

    sqlite3.connect = spy
    try:
        assert _client(a).get("//").status_code == 200
    finally:
        sqlite3.connect = real

    opened = [c for c in connected if "detections.sqlite" in c]
    assert opened, "the picker did read the databases"
    assert all("mode=ro" in c for c in opened), opened
    assert b.db_path.read_bytes() == before


def test_a_project_the_picker_cannot_read_still_appears(two):
    """A broken project that vanishes from the list is a project nobody can
    diagnose. It appears, marked, and the others are unaffected."""
    a, b, _ = two
    b.db_path.unlink()

    body = _client(a).get("//").text

    assert "Beta Site" in body
    assert "database not readable from here" in body
    assert "16 alert events" in _text(body)           # Alpha still reported


def test_the_picker_offers_creation_but_still_no_editing_of_an_existing_project(two):
    """RETIRED AND REPLACED, 2026-09-11. Formerly
    `test_the_picker_still_offers_no_creation_no_credential_no_site_editing`.

    That test asserted the picker had no form at all, on the Stage 6 rule that a
    mailbox app password must not be POSTed to a server with no authentication.
    The rule was conditional, and the condition has been met — `ttr serve` mints a
    session token and every route refuses without it — so creation is now offered
    and `test_web_create.py` covers it.

    What did NOT change is the rest of the old assertion, which is why this is a
    replacement rather than a deletion. There is still no way to edit an EXISTING
    project from this page: no site editing, no credential rotation, no rename.
    Creating a project is a deliberate act with a verification step; editing one
    in place is a different surface that was never argued for, and it should not
    arrive by accident just because a form now exists on the page.
    """
    a, _, _ = two
    body = _client(a).get("//").text.lower()

    # Creation: present.
    assert 'id="create-form"' in body
    assert 'type="password"' in body

    # Editing an existing project: still nowhere on this page.
    assert "set-site" not in body.replace("ttr project set-site", "")
    for forbidden in ("api/site", "api/rename", "api/password", "api/archive"):
        assert forbidden not in body, f"{forbidden} appeared — editing crept in"


# --------------------------------------------------------------------------- #
# Whose data is on screen
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("page", ["", "ingest"])
def test_a_page_inside_a_project_says_which_project_it_is(two, page):
    """The same failure mode as the cache leak: a report looks identical
    whichever project produced it."""
    a, b, _ = two

    body = _client(a).get(f"//p/{a.id}/{page}").text
    assert "Alpha Site" in body
    assert a.id[:8] in body
    assert 'href="/"' in body                         # and a way back to the picker
    assert "Beta Site" not in body

    other = _client(b).get(f"//p/{b.id}/{page}").text
    assert "Beta Site" in other and "Alpha Site" not in other


def test_the_project_banner_is_filled_in_not_left_as_a_placeholder(two):
    a, _, _ = two
    for page in ("", "ingest"):
        body = _client(a).get(f"//p/{a.id}/{page}").text
        assert "<!--PROJECT-->" not in body


# --------------------------------------------------------------------------- #
# The redesigned page: what it must not be able to lose
# --------------------------------------------------------------------------- #
def test_the_standing_caption_is_the_reports_own_words_not_a_copy(two):
    """The Decision 4 caption under the count is IMPORTED from the report stage.

    Its whole value is that it is byte-identical wherever it appears — the same
    sentence in a PDF, in a markdown report and on this page. A second copy is
    how that quietly stops being true, so this asserts the page prints the
    constant rather than a string that merely looks like it, and that the report
    still renders it in its own bolded markdown form.
    """
    from ttr.agents.report import (COUNT_CAVEAT, COUNT_CAVEAT_CLAIM,
                                   COUNT_CAVEAT_REST)

    a, _, _ = two
    body = _client(a).get("//").text

    assert COUNT_CAVEAT in body
    # And the report's own form is reassembled from the same two halves.
    assert (f"- **{COUNT_CAVEAT_CLAIM}**{COUNT_CAVEAT_REST}"
            == "- **Counts are alert events, not animals** — not abundance "
               "and not a biodiversity value.")


def test_the_caption_appears_even_when_there_is_nothing_to_caption(two):
    """It states what the number MEANS. A project with no events still has the
    unit explained, because the reader still has to know what would be counted."""
    a, b, _ = two
    from ttr.agents.report import COUNT_CAVEAT

    b.open_repo().close()
    b.db_path.unlink()                       # total unavailable, not merely zero
    body = _client(a).get("//").text

    assert "stored total unavailable" in body
    assert body.count(COUNT_CAVEAT) == 2, "every card carries it, broken or not"


def test_a_project_name_is_data_not_markup(two, tmp_path, monkeypatch):
    """Names, site labels and ids are externally supplied. They render escaped —
    and the card puts the name in three places now (heading, link, map label), so
    the escaping has to hold in all of them."""
    from ttr.projects import paths as ppaths
    from ttr.projects.registry import Registry
    from ttr.projects.service import create_project, set_site

    root = tmp_path / "xss-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    m, _ = create_project('<img src=x onerror="alert(1)">', "x@gmail.com",
                          root=root, skip_connection_test=True)
    set_site(m.id, name='</svg><script>alert(2)</script>',
             latitude=1.5, longitude=-2.5, root=root)

    from ttr.web import picker
    body = picker.render(Registry.load(root), root)

    assert "<script>alert(2)</script>" not in body
    assert '<img src=x onerror=' not in body
    assert "&lt;img src=x onerror=" in body
    assert "&lt;/svg&gt;&lt;script&gt;" in body


def test_a_day_with_no_delivery_is_not_drawn_as_a_zero(tmp_path, monkeypatch):
    """The email source cannot tell "no wildlife" from "nothing delivered". A gap
    day therefore gets a baseline tick that says `delivered`, never a zero-height
    bar and never a claim about animals."""
    import datetime as _d

    from ttr.projects import paths as ppaths
    from ttr.projects.registry import Registry
    from ttr.projects.service import context_for, create_project
    from ttr.web import picker

    root = tmp_path / "gap-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    m, _ = create_project("Gappy", "g@gmail.com", root=root,
                          skip_connection_test=True)
    ctx = context_for(Registry.load(root).get(m.id), root)
    repo = ctx.open_repo()
    try:
        for day in (1, 2, 5):                       # 3 and 4 are never delivered
            repo.upsert_event(make_persisted_event(
                f"<gap-{day}@x>", canonical_binomial="Columba palumbus",
                event_time=_d.datetime(2026, 7, day, 9, 0, tzinfo=UTC)))
    finally:
        repo.close()

    body = picker.render(Registry.load(root), root)

    assert body.count('class="bar"') == 3
    assert body.count('class="gap"') == 2
    assert "2026-07-03 &mdash; no alert events delivered" in body
    assert "2026-07-04 &mdash; no alert events delivered" in body
    # The words that would overclaim, on a page that cannot support them.
    for overclaim in ("no wildlife", "0 alert events", "no animals"):
        assert overclaim not in body


def test_the_rate_line_divides_by_the_span_not_by_the_delivering_days(tmp_path,
                                                                     monkeypatch):
    """6 events over a 5-day span is "1.2 a day", not "2 a day" over the 3 days
    that happened to deliver. Dividing by the bar count reports a flattering
    number for a different question."""
    import datetime as _d

    from ttr.projects import paths as ppaths
    from ttr.projects.registry import Registry
    from ttr.projects.service import context_for, create_project
    from ttr.web import picker

    root = tmp_path / "rate-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    m, _ = create_project("Ratey", "r@gmail.com", root=root,
                          skip_connection_test=True)
    ctx = context_for(Registry.load(root).get(m.id), root)
    repo = ctx.open_repo()
    try:
        n = 0
        for day in (1, 2, 5):
            for _ in range(2):
                repo.upsert_event(make_persisted_event(
                    f"<rate-{n}@x>", canonical_binomial="Columba palumbus",
                    event_time=_d.datetime(2026, 7, day, 9, 0, tzinfo=UTC)))
                n += 1
    finally:
        repo.close()

    body = picker.render(Registry.load(root), root)

    assert "1.2 a day over 5 days" in _text(body)
    assert "2 a day over 3 days" not in _text(body)


def test_the_page_asks_the_network_for_nothing(two):
    """No font CDN, no map tiles, no chart library, no icon package. The server
    is local and may be offline; a page that degrades when it is, is a page that
    was not built for where it runs."""
    import re

    a, _, _ = two
    body = _client(a).get("//").text

    for attr in ("src", "href", "action", "data", "poster"):
        for value in re.findall(rf'{attr}="([^"]*)"', body):
            assert not value.startswith(("http://", "https://", "//")), value
    for forbidden in ("fonts.googleapis", "fonts.gstatic", "cdn.", "unpkg",
                      "jsdelivr", "tile.openstreetmap", "@import"):
        assert forbidden not in body, forbidden


def test_the_current_project_is_not_labelled_active(two):
    """`registry.active_project` is the project the CLI acts on by default. It
    says nothing about whether a mailbox is still delivering, and ACTIVE beside a
    green dot is read as exactly that claim."""
    a, _, root = two
    from ttr.projects.service import use_project

    use_project(a.id, root=root)
    body = _client(a).get("//").text

    assert ">Current<" in body
    assert ">Active<" not in body


def test_the_create_panel_starts_closed(two):
    """It is a slide-over now, not an inline block, and it must be shut until
    someone asks for it.

    Regression: `.drawer { display: flex }` is a class selector and outranks the
    user agent's `[hidden] { display: none }`, so the `hidden` attribute alone
    left the panel open over the page on every load. The markup and the rule that
    makes the markup mean something are asserted together, because either one
    without the other is the bug.
    """
    a, _, _ = two
    body = _client(a).get("//").text

    assert '<div class="drawer" id="create" hidden>' in body
    assert ".drawer[hidden] { display: none; }" in body


def _eyebrow(html: str) -> str:
    """The hero status line's own text.

    Scoped deliberately: `_text` does not strip <style>, so a whole-page search
    for a word like "current" finds `currentColor` in the stylesheet and a search
    for "archived" finds the `.pill-archived` rule. The assertion is about one
    line of copy, so it reads that line.
    """
    import re
    m = re.search(r'<p class="eyebrow">(.*?)</p>', html, re.S)
    assert m, "the hero has no status line"
    # Entities resolved: the assertion is about the words a reader sees,
    # not about whether the separator is spelled &middot; or a literal dot.
    return _html.unescape(_text(m.group(1))).strip()


def test_the_hero_counts_projects_and_never_liveness(two):
    """The status line counts the list and nothing else.

    It used to read "N projects - 1 current". `registry.active_project` is
    whichever project `ttr project use` selected, so that figure was only ever 0
    or 1, and next to a word like "current" it reads as a count of mailboxes
    still delivering — which nothing on this page knows. The pill on the card
    carries "current" instead, attached to the project it describes.
    """
    a, _, _ = two
    line = _eyebrow(_client(a).get("//").text)

    assert line == "2 projects"
    for absent in ("current", "active", "live"):
        assert absent not in line.lower()


def test_archived_is_counted_in_the_hero_only_when_there_are_some(two):
    """"- 0 archived" is a count of nothing. It appears only when it says
    something."""
    from ttr.projects.registry import Registry
    from ttr.projects.service import set_archived
    from ttr.web import picker

    _, b, root = two

    assert _eyebrow(picker.render(Registry.load(root), root)) == "2 projects"

    set_archived(b.id, True, root=root)
    assert _eyebrow(picker.render(Registry.load(root), root)) == "1 project · 1 archived"
