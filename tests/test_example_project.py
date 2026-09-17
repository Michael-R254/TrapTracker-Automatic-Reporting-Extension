"""The example project a fresh install opens on.

`ttr serve` builds it from data bundled with the package, once per projects root,
and only into an empty one. These pin each half of that: it appears on a fresh
root, it never lands among someone's real projects, and deleting it is final.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ttr.cli import app
from ttr.projects import example
from ttr.projects import paths as ppaths
from ttr.projects.registry import Registry
from ttr.projects.service import create_project, delete_project

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DATA = REPO_ROOT / "src/ttr/projects/example_data"

runner = CliRunner()


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    monkeypatch.delenv(example.SKIP_ENV_VAR, raising=False)
    return root


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.mark.parametrize("bundled, published", [
    (example.EXTRACT_RESOURCE, "docs/evaluation/data/detections_20260628-0716.csv"),
    (example.ALIAS_RESOURCE, "examples/back-garden/species_aliases.yaml"),
])
def test_the_bundled_copy_matches_the_published_file(bundled, published):
    """The package carries a copy so an install without docs/ still has it. A copy
    that drifted would build a project that disagreed with the published evidence."""
    assert _read(PACKAGE_DATA / bundled) == _read(REPO_ROOT / published)


def test_a_fresh_root_gets_the_example(root):
    assert example.ensure_example_project() is True

    registry = Registry.load(root)
    [entry] = registry.entries
    assert entry.name == example.EXAMPLE_NAME
    assert registry.active_project == entry.id

    from ttr.projects.service import context_for

    ctx = context_for(entry, root)
    assert ctx.alias_table_source() == "project"
    assert ctx.site.latitude == example.EXAMPLE_LATITUDE
    repo = ctx.open_repo()
    try:
        assert sum(repo.species_counts().values()) == 475
    finally:
        repo.close()


def test_it_is_built_only_once(root):
    assert example.ensure_example_project() is True
    assert example.ensure_example_project() is False
    assert len(Registry.load(root).entries) == 1


def test_deleting_it_is_final(root):
    example.ensure_example_project()
    # Also pins that the project's own alias table does not make delete need --force.
    delete_project(example.EXAMPLE_NAME, root=root)

    assert example.ensure_example_project() is False
    assert Registry.load(root).entries == []


def test_it_never_joins_existing_projects(root):
    create_project("North Field Camera", root=root)

    assert example.ensure_example_project() is False
    assert [e.name for e in Registry.load(root).entries] == ["North Field Camera"]
    # Emptying the root later does not bring it back either.
    delete_project("North Field Camera", root=root)
    assert example.ensure_example_project() is False


def test_the_environment_can_turn_it_off(root, monkeypatch):
    monkeypatch.setenv(example.SKIP_ENV_VAR, "1")

    assert example.ensure_example_project() is False
    assert not (root / example.MARKER_NAME).exists(), \
        "skipping must not use up the one automatic build"


def test_a_failed_build_leaves_nothing_and_is_retried(root, monkeypatch):
    import ttr.projects.extract as extract

    def broken(ctx, rows, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(extract, "load_into", broken)
    messages = []
    assert example.ensure_example_project(notify=messages.append) is False
    assert "disk full" in messages[-1]
    assert Registry.load(root).entries == []
    assert not list((root / ppaths.PROJECTS_SUBDIR).iterdir())

    monkeypatch.undo()
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    monkeypatch.delenv(example.SKIP_ENV_VAR, raising=False)
    assert example.ensure_example_project() is True


def test_serve_builds_it_before_starting(root, monkeypatch):
    uvicorn = pytest.importorskip("uvicorn")
    seen = []
    monkeypatch.setattr(uvicorn, "run",
                        lambda *a, **k: seen.append(len(Registry.load(root).entries)))
    monkeypatch.delenv("TTR_UI_TOKEN", raising=False)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert seen == [1], "the server started before the example existed"
    assert "475 alert events loaded" in result.output + (result.stderr or "")
    from ttr.web import auth
    auth.set_ui_token(None)


def test_load_example_builds_it_on_request(root):
    create_project("North Field Camera", root=root)

    result = runner.invoke(app, ["project", "load-example"])

    assert result.exit_code == 0, result.output
    assert "475 rows loaded" in result.output
    assert sorted(e.name for e in Registry.load(root).entries) == \
        [example.EXAMPLE_NAME, "North Field Camera"]
