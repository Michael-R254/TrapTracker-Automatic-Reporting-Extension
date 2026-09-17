"""How many projects an unscoped TTR_IMAP_PASSWORD could be meant for.

`credentials.resolve` refuses the unscoped variable when more than one project is
in scope. Until 2026-09-15 nothing ever said more than one: the fetcher called
`ctx.imap_password()` with no arguments, so the count was always the default of
1, and one exported password was tried against whichever project was ingesting.
These tests pin who counts what. The web server's count is pinned in
test_web_projects.py and the pipeline's in test_pipeline_project.py.
"""

from __future__ import annotations

import inspect

import pytest
from typer.testing import CliRunner

from ttr.cli import app
from ttr.projects import credentials
from ttr.projects import paths as ppaths
from ttr.projects.context import ProjectContext
from ttr.projects.registry import Registry
from ttr.projects.resolve import PROJECT_ENV_VAR, resolve_project
from ttr.projects.service import create_project

runner = CliRunner()


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(r))
    monkeypatch.delenv(PROJECT_ENV_VAR, raising=False)
    return r


def _make(name, root):
    manifest, _ = create_project(name, f"{name.lower()}@gmail.com", root=root,
                                 skip_connection_test=True)
    return manifest


def _archive(root, project_id):
    registry = Registry.load(root)
    registry.update(project_id, archived=True)
    registry.save()


# --------------------------------------------------------------------------- #
# What a resolution puts in scope.
# --------------------------------------------------------------------------- #
def test_a_project_named_with_the_flag_is_one_project_in_scope(root):
    a = _make("Alpha", root)
    _make("Beta", root)

    assert resolve_project(a.id, root=root).credential_scope == 1


def test_a_project_named_by_ttr_project_is_one_project_in_scope(root, monkeypatch):
    a = _make("Alpha", root)
    _make("Beta", root)
    monkeypatch.setenv(PROJECT_ENV_VAR, a.id)

    assert resolve_project(root=root).credential_scope == 1


def test_a_defaulted_project_puts_every_visible_project_in_scope(root):
    _make("Alpha", root)
    _make("Beta", root)
    _make("Gamma", root)

    resolution = resolve_project(root=root)       # the active project, not named
    assert not resolution.explicit
    assert resolution.credential_scope == 3


def test_the_only_project_is_one_project_in_scope(root):
    _make("Solo", root)

    assert resolve_project(root=root).credential_scope == 1


def test_archived_projects_are_not_in_scope(root):
    _make("Alpha", root)
    beta = _make("Beta", root)
    _archive(root, beta.id)

    assert resolve_project(root=root).credential_scope == 1


def test_the_password_cannot_be_resolved_without_stating_the_scope():
    """The default of 1 is what switched the refusal off. It must not come back."""
    parameter = inspect.signature(ProjectContext.imap_password).parameters["projects_in_scope"]
    assert parameter.default is inspect.Parameter.empty


# --------------------------------------------------------------------------- #
# The CLI states the scope it resolved.
# --------------------------------------------------------------------------- #
class _Bundle:
    poll_seconds = 0

    class pipeline:
        @staticmethod
        def run_once():
            return 0

    def close(self):
        pass


def _capture_pipeline_scope(monkeypatch):
    seen = {}

    def fake_build(ctx, **kwargs):
        seen["projects_in_scope"] = kwargs.get("projects_in_scope")
        return _Bundle()

    monkeypatch.setattr("ttr.pipeline.build_pipeline_for", fake_build)
    return seen


def test_ttr_run_on_a_defaulted_project_counts_every_visible_project(root, monkeypatch):
    _make("Alpha", root)
    _make("Beta", root)
    seen = _capture_pipeline_scope(monkeypatch)

    result = runner.invoke(app, ["run", "--once"])

    assert result.exit_code == 0, result.output
    assert seen["projects_in_scope"] == 2


def test_ttr_run_on_a_named_project_is_one_project_in_scope(root, monkeypatch):
    alpha = _make("Alpha", root)
    _make("Beta", root)
    seen = _capture_pipeline_scope(monkeypatch)

    result = runner.invoke(app, ["--project", alpha.id, "run", "--once"])

    assert result.exit_code == 0, result.output
    assert seen["projects_in_scope"] == 1


def test_ttr_ingest_refuses_an_unscoped_password_for_a_defaulted_project(root, monkeypatch):
    _make("Alpha", root)
    _make("Beta", root)
    monkeypatch.setenv(credentials.ENV_UNSCOPED, "exported-for-one-mailbox")
    captured = {}

    class _Fetcher:
        def __init__(self, **kwargs):
            captured["password"] = kwargs["password"]

        def __getattr__(self, name):
            return lambda *a, **k: iter(())

    monkeypatch.setattr("ttr.sources.email_fetcher.EmailFetcher", _Fetcher)

    runner.invoke(app, ["ingest"])

    with pytest.raises(credentials.AmbiguousCredentialScope):
        captured["password"]()
