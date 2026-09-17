"""Stage-5 verification for the live ingest page (`/ingest`).

Runs whole ingest jobs with NO live inbox, NO model weights and NO Ollama: the
pipeline is real, its source is a fixture-backed fetcher and its enrichers are the
fakes from conftest. What is under test is the job runner, the log capture, the
progress payload and the render boundary — not the pipeline, which
`test_pipeline.py` already covers.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from ttr.enrichment.base import DescriptionRead, TaxonomicRead
from ttr.pipeline import Pipeline, PipelineBundle
from ttr.sources.email_source import EmailSource
from ttr.sources.seen_store import SeenStore
from ttr.species.aliases import SpeciesAliasMap
from ttr.storage.repository import DetectionRepository
from ttr.web import app as app_module
from ttr.web.app import app, get_pipeline_factory
from ttr.web.ingest import IngestRunner
from ttr.web.ingest_page import INGEST_PAGE

from conftest import FakeDescriptionEnricher, FakeTaxonomicEnricher, load_eml

from conftest import EXAMPLE_ALIAS_PATH as ALIAS_PATH


class FakeFetcher:
    """The offline stand-in for IMAP, as in test_pipeline.py."""

    def __init__(self, names):
        self._names = names

    def fetch_unseen(self):
        for name in self._names:
            msg = load_eml(name)
            yield (msg["Message-ID"] or "").strip(), msg


@pytest.fixture(autouse=True)
def _fresh_runner(monkeypatch):
    """A per-test IngestRunner.

    The production runner is a module-level singleton holding a thread; sharing it
    across tests would let one test's job bleed into the next one's assertions.
    """
    runner = IngestRunner()
    monkeypatch.setattr(app_module, "RUNNER", runner)
    yield runner
    runner.stop()
    thread = runner._thread
    if thread is not None:
        thread.join(timeout=10)


@pytest.fixture
def env(monkeypatch, tmp_path):
    from ttr.config import get_settings

    monkeypatch.setenv("IMAP_HOST", "imap.example.test")
    monkeypatch.setenv("IMAP_USER", "alerts@example.test")
    monkeypatch.setenv("IMAP_PASSWORD", "s3cret-do-not-leak")
    monkeypatch.setenv("SPECIES_ALIAS_PATH", ALIAS_PATH)
    get_settings.cache_clear()
    from conftest import make_web_project

    # A real project: the ingest routes live under /p/{id}/ and resolve one.
    yield make_web_project(tmp_path, monkeypatch, name="Ingest Test Site")
    get_settings.cache_clear()


@pytest.fixture
def client(env):
    from conftest import project_client

    with project_client(app, env) as c:
        yield c
    app.dependency_overrides.clear()


def _bundle_factory(tmp_path, names, *, taxo=None, poll_seconds=1):
    """A dependency override standing in for the real wiring."""
    alias_map = SpeciesAliasMap.from_yaml(ALIAS_PATH)

    def _provider():
        def _factory(on_event):
            repo = DetectionRepository(tmp_path / "job.db")
            pipeline = Pipeline(
                source=EmailSource(FakeFetcher(names), SeenStore(tmp_path / "seen.txt")),
                taxonomic=taxo or FakeTaxonomicEnricher(TaxonomicRead(
                    provider="bioclip", model_name="fake-bioclip",
                    topk=[("Meles meles", 0.91)], ok=True)),
                description=FakeDescriptionEnricher(DescriptionRead(
                    provider="ollama", model_name="fake-vlm", text="A badger.", ok=True)),
                repo=repo, alias_map=alias_map,
                image_store_dir=tmp_path / "images", on_event=on_event)
            return PipelineBundle(pipeline=pipeline, repo=repo, poll_seconds=poll_seconds)
        return _factory
    return _provider


def _drain(client, timeout=20.0):
    """Poll until the job reaches a terminal state; return the full snapshot."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get("/api/ingest/state?cursor=0").json()
        if data["state"] not in ("running", "stopping"):
            return data
        time.sleep(0.02)
    pytest.fail(f"ingest job did not finish within {timeout}s")


def _lines(snapshot):
    return [e["payload"]["line"] for e in snapshot["events"] if e["kind"] == "log"]


def _cards(snapshot):
    return [e["payload"] for e in snapshot["events"] if e["kind"] == "image"]


# --------------------------------------------------------------- the happy path
def test_ingest_job_emits_log_lines_and_image_cards(client, env):
    app.dependency_overrides[get_pipeline_factory] = _bundle_factory(
        env.dir, ["both_attachments.eml"])
    assert client.post("/api/ingest/start", json={"mode": "once"}).status_code == 202

    snap = _drain(client)
    assert snap["state"] == "done"
    assert snap["stored"] == 1

    joined = "\n".join(_lines(snap))
    assert "msg=parsed_event" in joined
    assert "msg=pipeline_event_stored" in joined

    cards = _cards(snap)
    assert len(cards) == 1
    card = cards[0]
    assert card["ok"] is True
    assert card["upstream_label"] == "MelesMeles"
    assert card["bioclip_top1"] == "Meles meles"
    assert card["agreement"] == "agree"
    assert card["event_id"] is not None


def test_card_never_carries_a_filesystem_path(client, env):
    """The report generator promises not to leak paths; so does this."""
    app.dependency_overrides[get_pipeline_factory] = _bundle_factory(
        env.dir, ["both_attachments.eml"])
    client.post("/api/ingest/start", json={"mode": "once"})
    card = _cards(_drain(client))[0]
    assert "original_image_path" not in card
    assert "boxed_image_path" not in card
    assert not any("path" in key for key in card)


def test_a_disagreement_is_reported_as_a_disagreement(client, env):
    """The point of the strip: BioCLIP contradicting the upstream token, visibly."""
    app.dependency_overrides[get_pipeline_factory] = _bundle_factory(
        env.dir, ["missing_boxed.eml"])       # roe deer upstream, badger from the fake
    client.post("/api/ingest/start", json={"mode": "once"})
    card = _cards(_drain(client))[0]
    assert card["upstream_label"] == "CapreolusCapreolus"
    assert card["agreement"] == "disagree"


def test_log_lines_use_the_shared_key_value_format(client, env):
    """Proves _KeyValueFormatter is REUSED, not restated — the console shows the
    same line the CLI prints."""
    app.dependency_overrides[get_pipeline_factory] = _bundle_factory(
        env.dir, ["both_attachments.eml"])
    client.post("/api/ingest/start", json={"mode": "once"})
    lines = _lines(_drain(client))
    assert all(line.startswith("ts=") for line in lines)
    assert any("level=INFO logger=ttr.pipeline msg=" in line for line in lines)


def test_captures_info_records_without_configure_logging(client, env):
    """The uvicorn process never calls configure_logging(), so root can sit at
    WARNING and drop INFO before any handler sees it. The job raises the level for
    its own lifetime; without that this console would be empty."""
    logging.getLogger().setLevel(logging.WARNING)
    app.dependency_overrides[get_pipeline_factory] = _bundle_factory(
        env.dir, ["both_attachments.eml"])
    client.post("/api/ingest/start", json={"mode": "once"})
    snap = _drain(client)
    assert any("level=INFO" in line for line in _lines(snap))


def test_restores_the_root_logger_afterwards(client, env):
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    before = list(root.handlers)
    app.dependency_overrides[get_pipeline_factory] = _bundle_factory(
        env.dir, ["both_attachments.eml"])
    client.post("/api/ingest/start", json={"mode": "once"})
    _drain(client)
    assert root.level == logging.WARNING
    assert list(root.handlers) == before


def test_does_not_capture_other_threads(client, env, _fresh_runner):
    """A concurrent /api/report request must never leak into the ingest console."""
    release = threading.Event()
    alias_map = SpeciesAliasMap.from_yaml(ALIAS_PATH)

    class BlockingFetcher:
        def fetch_unseen(self):
            release.wait(10)
            return iter(())

    def _provider():
        def _factory(on_event):
            repo = DetectionRepository(env.dir / "job.db")
            pipeline = Pipeline(
                source=EmailSource(BlockingFetcher(), SeenStore(env.dir / "seen.txt")),
                taxonomic=FakeTaxonomicEnricher(TaxonomicRead(
                    provider="bioclip", model_name="f", ok=True)),
                description=FakeDescriptionEnricher(DescriptionRead(
                    provider="ollama", model_name="f", ok=True)),
                repo=repo, alias_map=alias_map,
                image_store_dir=env.dir / "images", on_event=on_event)
            return PipelineBundle(pipeline=pipeline, repo=repo, poll_seconds=1)
        return _factory

    app.dependency_overrides[get_pipeline_factory] = _provider
    client.post("/api/ingest/start", json={"mode": "once"})
    # Emit from the TEST thread while the job thread is parked in the fetcher.
    logging.getLogger("ttr.notthejob").warning("leak_probe_marker")
    release.set()

    snap = _drain(client)
    assert "leak_probe_marker" not in "\n".join(_lines(snap))


# ------------------------------------------------------------------ lifecycle
def test_a_second_concurrent_job_is_refused_with_a_usable_snapshot(client, env):
    release = threading.Event()
    alias_map = SpeciesAliasMap.from_yaml(ALIAS_PATH)

    class BlockingFetcher:
        def fetch_unseen(self):
            release.wait(10)
            return iter(())

    def _provider():
        def _factory(on_event):
            repo = DetectionRepository(env.dir / "job.db")
            return PipelineBundle(
                pipeline=Pipeline(
                    source=EmailSource(BlockingFetcher(), SeenStore(env.dir / "seen.txt")),
                    taxonomic=FakeTaxonomicEnricher(TaxonomicRead(provider="b", model_name="f")),
                    description=FakeDescriptionEnricher(DescriptionRead(provider="o", model_name="f")),
                    repo=repo, alias_map=alias_map, image_store_dir=env.dir / "img",
                    on_event=on_event),
                repo=repo, poll_seconds=1)
        return _factory

    app.dependency_overrides[get_pipeline_factory] = _provider
    assert client.post("/api/ingest/start", json={"mode": "once"}).status_code == 202
    second = client.post("/api/ingest/start", json={"mode": "once"})
    assert second.status_code == 409
    body = second.json()
    assert body["state"] in ("running", "stopping")
    assert body["error"]
    release.set()
    _drain(client)


def test_stop_interrupts_the_poll_sleep(client, env):
    """Continuous mode sleeps on Event.wait, not time.sleep, so Stop lands at once.

    With time.sleep(poll_seconds) this test would take the full 30s and fail —
    which is exactly its job.
    """
    app.dependency_overrides[get_pipeline_factory] = _bundle_factory(
        env.dir, [], poll_seconds=30)
    client.post("/api/ingest/start", json={"mode": "continuous"})
    # Let it get past the first poll and into the idle wait.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get("/api/ingest/state?cursor=0").json()["polls"] >= 1:
            break
        time.sleep(0.02)

    started = time.monotonic()
    assert client.post("/api/ingest/stop").status_code == 200
    snap = _drain(client, timeout=10)
    assert snap["state"] == "done"
    assert time.monotonic() - started < 10, "Stop waited on the full poll interval"


def test_cursor_returns_only_new_entries_and_zero_replays_everything(client, env):
    app.dependency_overrides[get_pipeline_factory] = _bundle_factory(
        env.dir, ["both_attachments.eml"])
    client.post("/api/ingest/start", json={"mode": "once"})
    full = _drain(client)
    assert full["events"]

    tip = client.get(f"/api/ingest/state?cursor={full['cursor']}").json()
    assert tip["events"] == []                      # nothing repeats
    again = client.get("/api/ingest/state?cursor=0").json()
    assert len(again["events"]) == len(full["events"])   # a refresh replays


def test_a_new_run_gets_a_new_run_id_and_clears_the_buffer(client, env):
    app.dependency_overrides[get_pipeline_factory] = _bundle_factory(
        env.dir, ["both_attachments.eml"])
    client.post("/api/ingest/start", json={"mode": "once"})
    first = _drain(client)
    client.post("/api/ingest/start", json={"mode": "once"})
    second = _drain(client)
    assert second["run_id"] != first["run_id"]
    # Second run re-reads the same fixture but the seen-store suppresses it.
    assert second["stored"] == 0


# -------------------------------------------------------------------- failure
@pytest.mark.parametrize("exc, expected", [
    (ImportError("No module named 'torch'"), "enrich"),
    (RuntimeError("something unexpected"), "RuntimeError"),
])
def test_a_failing_build_reaches_the_console_not_a_500(client, env, exc, expected):
    def _provider():
        def _factory(on_event):
            raise exc
        return _factory

    app.dependency_overrides[get_pipeline_factory] = _provider
    assert client.post("/api/ingest/start", json={"mode": "once"}).status_code == 202
    snap = _drain(client)
    assert snap["state"] == "failed"
    assert expected in snap["error"]
    assert expected in "\n".join(_lines(snap))


def test_a_failure_never_leaks_the_imap_password(client, env):
    """friendly_error must never render a Settings object or a credential."""
    def _provider():
        def _factory(on_event):
            from ttr.config import get_settings
            settings = get_settings()
            raise RuntimeError(f"connection refused to {settings.imap_host}")
        return _factory

    app.dependency_overrides[get_pipeline_factory] = _provider
    client.post("/api/ingest/start", json={"mode": "once"})
    snap = _drain(client)
    assert snap["state"] == "failed"
    assert "s3cret-do-not-leak" not in str(snap)


def test_a_missing_credential_is_reported_as_an_actionable_line():
    """This used to check a ValidationError for a missing IMAP_* setting. Those
    keys are no longer read, so that advice would send someone to edit a file
    nothing consults. The modern equivalent is a credential problem, and each
    kind arrives already naming its own fix."""
    from ttr.projects.credentials import (NoCredentialBackend, NoCredentialStored,
                                          env_var_for)
    from ttr.web.ingest import friendly_error

    pid = "4fbc9958-08a6-4f35-bbfc-1e9ee80732e6"

    no_entry = friendly_error(NoCredentialStored(
        f"No mailbox credential is stored for this project.\n"
        f"    ttr project set-password {pid}"))
    assert "set-password" in no_entry
    assert pid in no_entry

    no_backend = friendly_error(NoCredentialBackend(
        f"No OS credential store is available on this machine.\n"
        f"    {env_var_for(pid)}=<app password>"))
    assert env_var_for(pid) in no_backend

    # The two stay distinguishable — they need different things done about them.
    assert no_entry != no_backend
    assert "s3cret" not in no_entry and "s3cret" not in no_backend


def test_a_failing_progress_sink_is_reported_but_not_fatal(client, env):
    """Degrade gracefully (§4): a display failure must not affect ingestion."""
    alias_map = SpeciesAliasMap.from_yaml(ALIAS_PATH)

    def _provider():
        def _factory(on_event):
            repo = DetectionRepository(env.dir / "job.db")

            def hostile(_payload):
                raise ValueError("sink exploded")

            pipeline = Pipeline(
                source=EmailSource(FakeFetcher(["both_attachments.eml"]),
                                   SeenStore(env.dir / "seen.txt")),
                taxonomic=FakeTaxonomicEnricher(TaxonomicRead(
                    provider="bioclip", model_name="f", topk=[("Meles meles", 0.9)], ok=True)),
                description=FakeDescriptionEnricher(DescriptionRead(
                    provider="ollama", model_name="f", ok=True)),
                repo=repo, alias_map=alias_map, image_store_dir=env.dir / "images",
                on_event=hostile)
            return PipelineBundle(pipeline=pipeline, repo=repo, poll_seconds=1)
        return _factory

    app.dependency_overrides[get_pipeline_factory] = _provider
    client.post("/api/ingest/start", json={"mode": "once"})
    snap = _drain(client)
    assert snap["state"] == "done"
    assert snap["stored"] == 1                      # the row was still stored
    assert "pipeline_progress_callback_failed" in "\n".join(_lines(snap))


# --------------------------------------------------------- the render boundary
def _page_code() -> str:
    """The page with comment lines removed.

    Asserting against the raw page would let a comment that merely NAMES a banned
    sink satisfy — or, as here, break — the check. The guarantee is about executable
    code, so the comments are stripped before it is tested.
    """
    kept = []
    for line in INGEST_PAGE.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
            continue
        kept.append(line)
    return "\n".join(kept)


def test_the_page_never_renders_untrusted_text_as_markup():
    """The structural XSS guarantee. The report page sanitises server-side; this
    page's defence is that it never parses the payload as HTML at all."""
    code = _page_code()
    assert "innerHTML" not in code
    assert "insertAdjacentHTML" not in code
    assert "document.write" not in code
    assert "outerHTML" not in code
    assert "textContent" in code


def test_a_hostile_label_round_trips_verbatim_as_json(client, env, tmp_path):
    """JSON needs no escaping — the string must survive intact, and the response
    must be JSON, so a browser can never be tricked into parsing it as a document."""
    from ttr.web.ingest import IngestRunner

    hostile = "<script>alert('xss')</script>"
    runner = IngestRunner()
    runner._on_progress({"ok": True, "upstream_label": hostile,
                         "parse_warnings": ['"><img src=x onerror=alert(1)>']})
    card = _cards(runner.snapshot(0))[0]
    assert card["upstream_label"] == hostile

    response = client.get("/api/ingest/state?cursor=0")
    assert response.headers["content-type"].startswith("application/json")


def test_thumbnail_absent_still_yields_a_captioned_card(monkeypatch):
    """A core-only install has no Pillow, so thumb_data_uri returns None."""
    import ttr.web.ingest as ingest_module
    from ttr.web.ingest import IngestRunner

    monkeypatch.setattr(ingest_module, "thumb_data_uri", lambda *a, **k: None)
    runner = IngestRunner()
    runner._on_progress({"ok": True, "upstream_label": "Vulpes", "agreement": "agree"})
    card = _cards(runner.snapshot(0))[0]
    assert card["thumb"] is None
    assert card["upstream_label"] == "Vulpes"


# ------------------------------------------------------------------- the pages
def test_ingest_page_is_served_and_linked_from_the_report_page(client):
    assert client.get("/ingest").status_code == 200
    assert 'href="ingest"' in client.get("/").text


def test_preflight_reports_what_a_run_would_touch(client, env):
    data = client.get("/api/ingest/preflight").json()
    assert data["config_error"] is None

    # The PROJECT is now the most important part of "what would this touch".
    assert data["project_id"] == env.id
    assert data["project_name"] == env.name

    # Mailbox settings come from the project's manifest, not from Settings.
    assert data["imap_host"] == env.mailbox.imap_host
    assert data["imap_folder"] == env.mailbox.imap_folder
    assert data["db_path"] == "detections.sqlite"
    assert data["db_rows"] == 0
    assert "seen_count" in data
    assert data["poll_seconds"] == env.mailbox.poll_seconds
    # No credential was stored for this project, and the panel says so without
    # ever reaching for a value.
    assert data["has_credential"] is False

    # Paths stay BASENAMES: which database, not the operator's directory layout.
    assert "/" not in data["db_path"] and "\\" not in data["db_path"]

    # The host is reported so the operator knows which mailbox; the credentials
    # are not, and must never be.
    assert "s3cret-do-not-leak" not in str(data)
    assert "alerts@example.test" not in str(data)
    assert env.mailbox.imap_user not in str(data)


#: Everything on the report page that is CHROME rather than report. None of it
#: may print: a PDF of a report must be the report, not a screenshot of the tool
#: that made it. Listed by selector so a new piece of furniture has to be added
#: here deliberately — which is how `header.app` and `.placeholder` arrived.
#: The reports-page redesign (Phase 1) added the last five. Each is listed even
#: where an ancestor already hides it — `.controls` IS the form, `.ingest-cta`
#: sits inside `.titlerow` — because this list is the deliberate record of what
#: counts as furniture, and an element that only prints correctly by inheritance
#: stops doing so the moment it is moved.
#:
#: `.placeholder` was REMOVED in the same pass, and that is also deliberate: the
#: waiting panel now carries `class="waiting"` (it keeps the id, which the page's
#: own script still uses to hide and restore it), so no element bears that class
#: any more and the selector guarded nothing. It is listed here as a deletion
#: rather than silently dropped, because a shrinking guard deserves as much
#: notice as a growing one.
_CHROME_SELECTORS = ["h1", ".note", "form", "#status", "#pdf", "nav.whose",
                     "header.app",
                     ".titlerow", ".ingest-cta", ".controls", ".summary",
                     ".waiting"]


def test_the_report_page_prints_the_report_and_none_of_the_chrome(client):
    """Asserted as a property, not as one exact rule.

    This used to compare the whole `display: none` line character for character,
    which meant ADDING a selector to it — strengthening the guarantee — failed the
    test. A guard that breaks when the thing it guards gets stronger teaches people
    to edit the guard, so it now checks each selector is hidden however the rule is
    punctuated.
    """
    page = client.get("/").text
    assert "@media print" in page

    print_block = page[page.index("@media print"):]
    hidden = print_block[:print_block.index("display: none !important;")]
    for selector in _CHROME_SELECTORS:
        assert selector in hidden, f"{selector} would print with the report"
