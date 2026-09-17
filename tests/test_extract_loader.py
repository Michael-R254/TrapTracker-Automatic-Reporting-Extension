"""`ttr project import-extract` — the published extract, opened as a project.

The loader's whole claim is that it changes nothing. These pin that: the rows it
writes carry the CSV's own verdicts, the fields it must invent are visibly
invented, and the project it builds has no mailbox behind it.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

import pytest

from ttr.projects import paths as ppaths
from ttr.projects.errors import MailboxNotConfigured, ProjectError
from ttr.projects.extract import load_into, read_extract
from ttr.projects.registry import Registry
from ttr.config import get_settings
from ttr.projects.service import context_for, create_project

#: The real published extract. Using it rather than a synthetic fixture is the
#: point: this test asserts the shipped example builds from the shipped file.
EXTRACT = Path(__file__).resolve().parents[1] / "docs/evaluation/data/detections_20260628-0716.csv"
ALIAS = Path(__file__).resolve().parents[1] / "examples/back-garden/species_aliases.yaml"


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    manifest, project_dir = create_project("Example Site", root=root)
    import shutil
    shutil.copy2(ALIAS, project_dir / "species_aliases.yaml")
    return context_for(Registry.load(root).get(manifest.id), root), root


def test_the_published_extract_is_readable(project):
    rows = read_extract(EXTRACT)
    assert len(rows) == 475, "the shipped extract changed size"


def test_a_project_from_the_extract_has_no_mailbox(project):
    """The extract has no inbox behind it. Inventing a plausible address so one
    could be configured would be offering to poll a mailbox that does not
    exist."""
    ctx, _ = project

    assert ctx.mailbox.address == ""
    assert ctx.mailbox.imap_host == ""
    assert ctx.credential_source() is None


def test_every_row_loads_with_its_stored_verdict_unchanged(project):
    """The loader copies; it does not re-decide.

    If it recomputed `agreement_flag` the project would disagree with the
    evidence file it was built from, which is the one thing an extract loader
    must never do.
    """
    ctx, _ = project
    rows = read_extract(EXTRACT)
    assert load_into(ctx, rows) == 475

    from collections import Counter
    expected = Counter(r["agreement_flag"] for r in rows)

    repo = ctx.open_repo()
    try:
        got = Counter(
            r[0] for r in repo._conn.execute(
                "SELECT agreement_flag FROM detection_events").fetchall())
    finally:
        repo.close()
    assert got == expected, "a verdict changed on the way in"


def test_the_invented_fields_are_visibly_invented(project):
    """Three fields cannot come from the extract. Each must be unmistakable."""
    ctx, _ = project
    load_into(ctx, read_extract(EXTRACT))

    repo = ctx.open_repo()
    try:
        row = repo._conn.execute(
            "SELECT source_message_id, images_present, capture_time_utc, "
            "       event_time_is_capture_time, source_type "
            "FROM detection_events LIMIT 1").fetchone()
    finally:
        repo.close()
    message_id, images, capture, is_capture, source_type = row

    # RFC 2606 reserved — can never resolve, and never looks real.
    assert message_id.endswith("@invalid>")
    assert images == "none", "the extract publishes no image bytes"
    assert capture is None, "the extract carries send time only (Decision 3)"
    assert not is_capture
    assert source_type == "extract", "the row says where it came from"


def test_no_identifier_from_the_deployment_reaches_the_database(project):
    """The extract excludes Message-IDs, addresses and paths. Loading it must not
    reintroduce any of them."""
    ctx, _ = project
    load_into(ctx, read_extract(EXTRACT))

    repo = ctx.open_repo()
    try:
        blob = " ".join(
            str(v) for row in repo._conn.execute(
                "SELECT source_message_id, original_image_path, boxed_image_path, "
                "       source_from, upstream_image_id FROM detection_events"
            ).fetchall() for v in row)
    finally:
        repo.close()

    for forbidden in ("@gmail", "mx.google", "image_store", ".jpg", ".eml"):
        assert forbidden not in blob, f"{forbidden!r} reached the database"


def test_the_example_alias_table_resolves_every_class_in_the_extract(project):
    """G2: without its own table the project falls back to the bundled
    illustrative one, which is not this data's class list."""
    ctx, _ = project

    assert ctx.alias_table_source() == "project"

    amap = ctx.load_alias_map()
    rows = read_extract(EXTRACT)
    tokens = {r["upstream_label"] for r in rows}
    unresolved = [t for t in tokens if amap.canonical_key_for_token(t) is None]
    assert not unresolved, f"the example table misses: {unresolved}"


def test_a_malformed_extract_is_refused_by_name(tmp_path):
    """Half-loading and failing later on a NULL is worse than refusing."""
    bad = tmp_path / "bad.csv"
    bad.write_text("idx,upstream_label\n1,ColumbaPalumbus\n", encoding="utf-8")

    with pytest.raises(ProjectError) as exc:
        read_extract(bad)
    assert "canonical_binomial" in str(exc.value)
    assert "event_time_utc" in str(exc.value)


def test_an_absent_extract_says_so(tmp_path):
    with pytest.raises(ProjectError, match="no such extract"):
        read_extract(tmp_path / "nope.csv")

# --------------------------------------------------------------------------- #
# A project with no mailbox
# --------------------------------------------------------------------------- #
# ADDED 2026-09-13 with `create_project(address=None)`, which the published
# example needs: the extract has no inbox behind it, and giving it a plausible
# Gmail address so it could be created would invent a mailbox and then offer to
# poll it.
#
# "Everything downstream coped" is an observation, not a guarantee. These four
# pin the state as SUPPORTED rather than merely tolerated: it must refuse to
# ingest, say so rather than failing, report its credential state honestly, and
# still produce reports — which is the whole point of the example.


@pytest.fixture
def mailboxless(tmp_path, monkeypatch):
    """A project created with no mailbox at all."""
    root = tmp_path / "nomail"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    manifest, project_dir = create_project("Example Site", root=root)
    return context_for(Registry.load(root).get(manifest.id), root), root


def test_a_mailbox_less_project_records_no_mailbox(mailboxless):
    """Empty, not a placeholder. A plausible-looking address in the manifest
    would be a mailbox this project does not have."""
    ctx, _ = mailboxless

    assert ctx.mailbox.address == ""
    assert ctx.mailbox.imap_host == ""
    assert ctx.mailbox.imap_user == ""
    assert ctx.mailbox.credential_ref is None
    assert ctx.credential_source() is None


def test_it_refuses_to_ingest_rather_than_failing_mid_run(mailboxless, monkeypatch):
    """Building the pipeline raises rather than polling an empty host.

    The refusal is `MailboxNotConfigured`, raised by
    `ProjectContext.require_mailbox()` before a repository, a seen-store or a
    model is built. It names the project, the `project.toml` whose [mailbox] to
    edit, and the next step for this machine — `ttr project set-password`, or the
    per-project environment variable where there is no credential store.

    It used to be `EmailFetcher needs host, user and a password callable`: the
    three constructor arguments the fetcher was missing, which named the missing
    things but read as an internal assertion rather than an answer. This
    docstring recorded that as worth improving, and it was — one raise site now
    serves every ingest entry point, so the CLI answers as well as the page does.
    The assertions below pin both halves of that. The exception and what its
    message names — and that NOTHING was built before it: the repository, the
    seen-store and the enricher factories are patched to fail if they are
    reached, so a refusal that moved later would fail this test rather than pass
    it quietly.

    The web path still never reaches it: the ingest page disables its fetch
    controls the moment `has_credential` is false (the test below).
    """
    import ttr.enrichment.factory as enrichment_factory
    import ttr.sources.seen_store as seen_store
    import ttr.storage.repository as repository
    from ttr.pipeline import build_pipeline_for
    from ttr.projects.credentials import supply_step

    ctx, _ = mailboxless

    def _never(*args, **kwargs):
        raise AssertionError("something was built before the refusal")

    # `build_pipeline_for` resolves these by attribute at call time, so patching
    # the modules is enough to catch a refusal that arrives too late.
    monkeypatch.setattr(repository, "DetectionRepository", _never)
    monkeypatch.setattr(seen_store, "SeenStore", _never)
    monkeypatch.setattr(enrichment_factory, "build_taxonomic_enricher", _never)
    monkeypatch.setattr(enrichment_factory, "build_description_enricher", _never)

    with pytest.raises(MailboxNotConfigured) as exc:
        build_pipeline_for(ctx, app_settings=get_settings())

    message = str(exc.value)
    assert "Example Site" in message, "the refusal must say WHICH project"
    assert "cannot ingest" in message
    # What to edit, and the step that follows - computed from the same helper, so
    # a machine with no credential store is expected to be told the variable
    # rather than `set-password`.
    assert str(ctx.dir / "project.toml") in message
    assert supply_step(ctx.id) in message
    assert "EmailFetcher" not in message, \
        "an internal assertion has no place in a message a person reads"


def test_the_ingest_page_says_so_instead_of_offering_a_dead_button(mailboxless):
    """§5 of the ingest spec: the fetch controls are disabled and the line names
    the CLI command that stores a credential."""
    ctx, _ = mailboxless
    from ttr.web.ingest_page import INGEST_PAGE

    # The preflight is what the page reads; assert on the payload it would get.
    from ttr.web.app import ingest_preflight
    data = ingest_preflight(ctx=ctx)

    assert data["has_credential"] is False
    assert data["credential_source"] is None
    assert data["imap_host"] == ""

    # And the page turns that into a disabled control with a reason, rather than
    # a button that fails when pressed.
    assert "|| !hasCredential" in INGEST_PAGE
    assert "No mailbox credential stored" in INGEST_PAGE
    assert "ttr project set-password" in INGEST_PAGE


def test_the_projects_page_reports_the_credential_state_honestly(mailboxless):
    """No credential is a fact about the project, not a blank."""
    ctx, root = mailboxless
    from ttr.web import picker

    html = picker.render(Registry.load(root), root)

    assert "no mailbox credential" in html
    assert "mailbox credential stored" not in html.replace(
        "no mailbox credential", "")


def test_it_still_generates_a_report(mailboxless):
    """The point of the example. A project that cannot ingest can still report
    on rows it already holds."""
    import datetime as dt

    from ttr.agents.report import ReportGeneratorAgent
    from ttr.agents.retrieval import RetrievalAgent
    from ttr.agents.window import TimeWindow

    ctx, _ = mailboxless
    load_into(ctx, read_extract(EXTRACT))

    repo = ctx.open_repo()
    try:
        window = TimeWindow.from_dates(dt.date(2026, 6, 28), dt.date(2026, 7, 16))
        agent = ReportGeneratorAgent(RetrievalAgent(repo, ctx.load_alias_map()),
                                     _QuietLLM())
        markdown = agent.generate_all_species(window)
    finally:
        repo.close()

    assert len(markdown) > 5000
    assert "Columba palumbus" in markdown
    # The standing caveats do not depend on there being a mailbox.
    assert "alert events, not animals" in markdown


class _QuietLLM:
    """A stand-in narrator. The vocabulary gate withholds its output, which is
    the correct behaviour for text that does not name what it counts — and it
    leaves the figures, which are what this test is about, untouched."""

    def generate(self, prompt, *, system=None, images=None):
        return "A placeholder narrative."
