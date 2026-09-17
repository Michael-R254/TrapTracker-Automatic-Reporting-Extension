"""Stage 3: the pipeline is built from a ProjectContext, not global config.

Nothing here tests what the pipeline DECIDES — that is unchanged by this stage
and is pinned by test_agreement / test_crosscheck_corpus / test_crop_decision.
These tests are about WHERE it reads configuration from, and that two projects
cannot reach each other's storage.
"""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

import pytest
from pydantic import SecretStr

from ttr.pipeline import build_pipeline_for
from ttr.projects import paths as ppaths
from ttr.projects.context import ProjectContext
from ttr.projects.registry import Registry
from ttr.projects.service import context_for, create_project

from conftest import (FakeDescriptionEnricher, FakeTaxonomicEnricher,
                      mailbox_factory)

UTC = dt.timezone.utc
SENTINEL = "SENT1NEL-pipeline-must-never-hold-this"
GMAIL_SHAPED = "abcd efgh ijkl mnop"


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(r))
    return r


def make_project(name, email, *, root, password=GMAIL_SHAPED):
    manifest, project_dir = create_project(
        name, email, password, root=root,
        mailbox_factory=mailbox_factory(messages=3))
    return context_for(Registry.load(root).get(manifest.id), root), project_dir


class _AppSettings:
    """The machine-level surface `build_pipeline_for` reads off Settings.

    Deliberately a stand-in rather than a real `Settings`: this stage must prove
    the pipeline no longer needs per-deployment values from it, and a real
    Settings would supply them by accident.
    """

    vlm_fallback_to_original_on_missing_boxed = False
    agreement_topk_join = False
    bioclip_model = "test-model"
    bioclip_device = "cpu"
    bioclip_topk = 5
    ollama_model = "test-vlm"
    ollama_endpoint = "http://localhost:0"
    ollama_timeout_seconds = 1.0


@pytest.fixture
def fake_enrichers(monkeypatch):
    """Neither BioCLIP nor Ollama — no weights, no network."""
    import ttr.enrichment.factory as factory
    from ttr.enrichment.base import DescriptionRead, TaxonomicRead

    taxo = TaxonomicRead(provider="bioclip", model_name="test-model",
                         topk=[("Vulpes vulpes", 0.9)],
                         topk_lineages=[{}], ok=True)
    desc = DescriptionRead(provider="ollama", model_name="test-vlm",
                           text="a fox", ok=True)
    monkeypatch.setattr(factory, "build_taxonomic_enricher",
                        lambda s: FakeTaxonomicEnricher(taxo))
    monkeypatch.setattr(factory, "build_description_enricher",
                        lambda s: FakeDescriptionEnricher(desc))


def _event(message_id: str, label: str = "VulpesVulpes"):
    from ttr.sources.base import DetectionEvent

    return DetectionEvent(
        source_type="email",
        source_message_id=message_id,
        ingested_at_utc=dt.datetime(2026, 7, 1, 12, 5, tzinfo=UTC),
        upstream_project="demo",
        upstream_label=label,
        upstream_best_confidence=0.91,
        upstream_image_id=None,
        upstream_event_time_utc=dt.datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        images=[],
        field_provenance={},
    )


# --------------------------------------------------------------------------- #
# Storage isolation
# --------------------------------------------------------------------------- #
def test_a_pipeline_writes_only_to_its_own_project(root, fake_enrichers):
    a_ctx, a_dir = make_project("Alpha", "a@gmail.com", root=root)
    b_ctx, b_dir = make_project("Beta", "b@gmail.com", root=root)

    bundle = build_pipeline_for(a_ctx, app_settings=_AppSettings())
    try:
        bundle.pipeline.process_event(_event("<only-alpha@x>"))
    finally:
        bundle.close()

    a_repo, b_repo = a_ctx.open_repo(), b_ctx.open_repo()
    try:
        assert sum(a_repo.species_counts().values()) == 1
        assert b_repo.species_counts() == {}
    finally:
        a_repo.close()
        b_repo.close()

    # The seen-store followed the database, not the process.
    assert a_ctx.seen_store_path.exists()
    assert "<only-alpha@x>" in a_ctx.seen_store_path.read_text(encoding="utf-8")
    assert not b_ctx.seen_store_path.exists()
    assert a_ctx.seen_store_path.parent == a_dir
    assert b_ctx.seen_store_path.parent == b_dir


def test_interleaved_writes_stay_in_their_own_projects(root, fake_enrichers):
    a_ctx, _ = make_project("Alpha", "a@gmail.com", root=root)
    b_ctx, _ = make_project("Beta", "b@gmail.com", root=root)

    a = build_pipeline_for(a_ctx, app_settings=_AppSettings())
    b = build_pipeline_for(b_ctx, app_settings=_AppSettings())
    try:
        for i in range(5):
            a.pipeline.process_event(_event(f"<a{i}@x>"))
            b.pipeline.process_event(_event(f"<b{i}@x>"))
            b.pipeline.process_event(_event(f"<b-extra{i}@x>"))
    finally:
        a.close()
        b.close()

    a_repo, b_repo = a_ctx.open_repo(), b_ctx.open_repo()
    try:
        assert sum(a_repo.species_counts().values()) == 5
        assert sum(b_repo.species_counts().values()) == 10
    finally:
        a_repo.close()
        b_repo.close()

    a_seen = a_ctx.seen_store_path.read_text(encoding="utf-8")
    b_seen = b_ctx.seen_store_path.read_text(encoding="utf-8")
    assert "<a0@x>" in a_seen and "<b0@x>" not in a_seen
    assert "<b0@x>" in b_seen and "<a0@x>" not in b_seen


def test_the_bundle_reports_the_projects_own_poll_interval(root, fake_enrichers):
    ctx, project_dir = make_project("Polled", "p@gmail.com", root=root)
    from ttr.projects.manifest import ProjectManifest

    manifest = ProjectManifest.read(project_dir)
    manifest.mailbox.poll_seconds = 111
    manifest.write(project_dir)

    ctx = ProjectContext.load(project_dir)
    bundle = build_pipeline_for(ctx, app_settings=_AppSettings())
    try:
        assert bundle.poll_seconds == 111
    finally:
        bundle.close()


# --------------------------------------------------------------------------- #
# Per-project alias tables, proven through the pipeline
# --------------------------------------------------------------------------- #
def test_a_project_local_alias_table_is_used_by_that_project_alone(
        root, fake_enrichers):
    """Stage 1 proved this in isolation. This proves it end to end: the stored
    canonical_binomial is derived at write time, so the table in force decides
    what every row is queryable as."""
    a_ctx, a_dir = make_project("Alpha", "a@gmail.com", root=root)
    b_ctx, _ = make_project("Beta", "b@gmail.com", root=root)

    # A token the bundled table does not know, mapped only for project A.
    (a_dir / "species_aliases.yaml").write_text(
        'Vulpes vulpes:\n'
        '  raw_tokens: ["HouseholdFoxUnit"]\n'
        '  common_names: ["reynard"]\n',
        encoding="utf-8")
    a_ctx = ProjectContext.load(a_dir)

    assert a_ctx.alias_table_source() == "project"
    assert b_ctx.alias_table_source() == "bundled"

    a = build_pipeline_for(a_ctx, app_settings=_AppSettings())
    b = build_pipeline_for(b_ctx, app_settings=_AppSettings())
    try:
        a.pipeline.process_event(_event("<a@x>", label="HouseholdFoxUnit"))
        b.pipeline.process_event(_event("<b@x>", label="HouseholdFoxUnit"))
    finally:
        a.close()
        b.close()

    a_repo, b_repo = a_ctx.open_repo(), b_ctx.open_repo()
    try:
        # A resolved it to a real binomial...
        assert a_repo.species_counts() == {"Vulpes vulpes": 1}
        # ...B could not, and says so with a reserved key rather than NULL.
        b_counts = b_repo.species_counts()
        assert list(b_counts) == ["unmapped:HouseholdFoxUnit"]
    finally:
        a_repo.close()
        b_repo.close()


# --------------------------------------------------------------------------- #
# The fetcher holds no password
# --------------------------------------------------------------------------- #
def test_the_fetcher_never_holds_the_password(root):
    from ttr.sources.email_fetcher import EmailFetcher

    calls = []

    def resolve():
        calls.append(1)
        return SecretStr(SENTINEL)

    fetcher = EmailFetcher(host="imap.gmail.com", user="u@gmail.com",
                           password=resolve, folder="INBOX", use_ssl=True)

    # Not resolved at construction — only at login.
    assert calls == []
    assert SENTINEL not in repr(fetcher)
    assert SENTINEL not in str(fetcher)
    for value in fetcher.__dict__.values():
        assert SENTINEL not in repr(value)
    assert SENTINEL not in repr(fetcher.__dict__)

    # The repr names the mailbox, which is the useful part.
    assert "imap.gmail.com" in repr(fetcher)
    assert "u@gmail.com" in repr(fetcher)


def test_the_fetcher_resolves_the_password_at_login_not_construction(root):
    from ttr.sources.email_fetcher import EmailFetcher

    calls = []
    record = []

    def resolve():
        calls.append(1)
        return SecretStr("abcdefghijklmnop")

    fetcher = EmailFetcher(host="h", user="u", password=resolve,
                           folder="INBOX", use_ssl=True)
    fetcher._mailbox_cls = lambda: mailbox_factory(record=record)(True)

    assert calls == []
    list(fetcher.fetch_unseen())
    assert calls == [1]                                # resolved exactly once
    assert record[0]["password"] == "abcdefghijklmnop"


def test_a_project_pipelines_fetcher_uses_the_projects_credential(root, fake_enrichers):
    ctx, _ = make_project("Credentialled", "c@gmail.com", root=root)
    bundle = build_pipeline_for(ctx, app_settings=_AppSettings(), projects_in_scope=1)
    try:
        fetcher = bundle.pipeline._source._fetcher
        record = []
        fetcher._mailbox_cls = lambda: mailbox_factory(record=record)(True)
        list(fetcher.fetch_unseen())
    finally:
        bundle.close()

    assert record[0]["user"] == "c@gmail.com"
    assert record[0]["host"] == "imap.gmail.com"
    assert record[0]["password"] == "abcdefghijklmnop"


def test_the_pipeline_refuses_an_unscoped_password_with_several_projects_in_scope(
        root, fake_enrichers, monkeypatch):
    from ttr.projects import credentials

    ctx, _ = make_project("Credentialled", "c@gmail.com", root=root)
    monkeypatch.setenv(credentials.ENV_UNSCOPED, SENTINEL)
    bundle = build_pipeline_for(ctx, app_settings=_AppSettings(), projects_in_scope=2)
    try:
        with pytest.raises(credentials.AmbiguousCredentialScope):
            bundle.pipeline._source._fetcher._password()
    finally:
        bundle.close()


def test_a_pipeline_built_without_a_stated_scope_refuses_at_login(root, fake_enrichers):
    """It still BUILDS, so a construction-only caller is unaffected; it cannot log in."""
    from ttr.projects import credentials

    ctx, _ = make_project("Credentialled", "c@gmail.com", root=root)
    bundle = build_pipeline_for(ctx, app_settings=_AppSettings())
    try:
        with pytest.raises(credentials.CredentialScopeNotStated):
            bundle.pipeline._source._fetcher._password()
    finally:
        bundle.close()


def test_the_fetcher_refuses_to_be_built_without_a_credential_source():
    from ttr.sources.email_fetcher import EmailFetcher

    with pytest.raises(TypeError):
        EmailFetcher(host="h", user="u")          # password is required


# --------------------------------------------------------------------------- #
# Thread binding
# --------------------------------------------------------------------------- #
def test_a_bundle_built_in_a_worker_thread_can_write_from_it(root, fake_enrichers):
    """sqlite connections are thread-bound (migrations.connect leaves
    check_same_thread=True), so the background ingest job must build its own
    bundle. This is the property Stage 6 depends on."""
    ctx, _ = make_project("Threaded", "t@gmail.com", root=root)
    errors: list[BaseException] = []

    def worker():
        try:
            bundle = build_pipeline_for(ctx, app_settings=_AppSettings())
            try:
                bundle.pipeline.process_event(_event("<from-a-thread@x>"))
            finally:
                bundle.close()
        except BaseException as exc:                    # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=30)

    assert errors == []
    repo = ctx.open_repo()
    try:
        assert sum(repo.species_counts().values()) == 1
    finally:
        repo.close()


def test_open_repo_stays_a_method_so_nothing_is_shared_across_threads(root):
    ctx, _ = make_project("Method", "m@gmail.com", root=root)
    assert callable(ctx.open_repo)
    assert "open_repo" not in vars(ctx)


# --------------------------------------------------------------------------- #
# The Settings shim is GONE (Stage 6)
#
# `build_pipeline(settings, ...)` and `context_from_settings` existed only to keep
# `ttr run` and the web ingest trigger working while the composition roots were
# threaded onto project contexts. Both callers now build a ProjectContext, so both
# shims were deleted. These assert they stay deleted: re-adding one would put a
# second path to a database back into the tree, which is the shape the whole
# refactor removes.
# --------------------------------------------------------------------------- #
def test_the_settings_shims_are_gone():
    import ttr.pipeline as pipeline
    import ttr.projects.context as context

    assert not hasattr(pipeline, "build_pipeline"), (
        "build_pipeline was re-added; every caller should use build_pipeline_for")
    assert not hasattr(context, "context_from_settings")
    assert not hasattr(context, "_SettingsBackedContext")


def test_the_fetcher_no_longer_accepts_a_settings_object():
    """Stage 3 kept `EmailFetcher(settings, ...)` alive because `cli.py`'s
    `ingest` still used it. Stage 5 moved that command onto a project context,
    so the legacy form is gone -- mailbox configuration is per-deployment and no
    longer has any business arriving from process-level Settings."""
    from ttr.sources.email_fetcher import EmailFetcher

    with pytest.raises(TypeError):
        EmailFetcher(object())                    # positional form removed


# --------------------------------------------------------------------------- #
# Alias-table visibility (§6)
# --------------------------------------------------------------------------- #
def test_alias_table_source_reports_all_three_cases(root, tmp_path):
    ctx, project_dir = make_project("Sourced", "s@gmail.com", root=root)
    assert ctx.alias_table_source() == "bundled"

    (project_dir / "species_aliases.yaml").write_text("Vulpes vulpes: {}\n",
                                                      encoding="utf-8")
    assert ProjectContext.load(project_dir).alias_table_source() == "project"

    from ttr.projects.manifest import ProjectManifest

    manifest = ProjectManifest.read(project_dir)
    manifest.species.alias_path = str(tmp_path / "elsewhere.yaml")
    manifest.write(project_dir)
    assert ProjectContext.load(project_dir).alias_table_source() == "manifest"


def test_the_fallback_notice_does_not_fire_on_every_load(root, capsys):
    """`load_alias_map` only emits its notice when a `notify` callback is passed,
    and the project path passes none — so a project on the bundled table is not
    a per-load stderr line. That silence is why `list` reports it instead."""
    ctx, _ = make_project("Quiet", "q@gmail.com", root=root)
    capsys.readouterr()                     # discard creation's own log lines
    for _ in range(3):
        ctx.load_alias_map()
    captured = capsys.readouterr()
    # Asserted on the NOTICE, not on total silence: structured log lines are a
    # separate stream with a separate purpose, and asserting no output at all
    # would make this test fail the next time anything logs.
    assert "using bundled example" not in captured.err
    assert "not found" not in captured.err
    assert captured.out == ""


def test_cli_list_shows_which_alias_table_is_in_force(root, tmp_path):
    from typer.testing import CliRunner

    from ttr.cli import app

    runner = CliRunner()
    _, a_dir = make_project("Bundled One", "a@gmail.com", root=root)
    _, b_dir = make_project("Own Table", "b@gmail.com", root=root)
    (b_dir / "species_aliases.yaml").write_text("Vulpes vulpes: {}\n",
                                                encoding="utf-8")

    result = runner.invoke(app, ["project", "list"],
                           env={ppaths.ROOT_ENV_VAR: str(root)})
    assert result.exit_code == 0
    assert "aliases: bundled example" in result.output
    assert "aliases: project table" in result.output
