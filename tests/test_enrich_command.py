"""`ttr enrich` with no project, and the loader tolerance that makes it work.

The command's own docstring promises it: "with no project it falls back to the
bundled tables with a notice". It did not. The per-deployment fields left
`Settings` for projects, and the fallback still handed that object to
`load_alias_map`, which read `settings.species_alias_path` directly - so the one
path that was meant to work everywhere died with

    AttributeError: 'Settings' object has no attribute 'species_alias_path'

on any machine with no project, a fresh container included. `load_target_taxonomy`
had always read its attribute with a default; the alias loader now does the same.

No model weights are needed here: the taxonomic enricher is a fake, so what is
under test is the species-table plumbing rather than BioCLIP.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from ttr.cli import app
from ttr.enrichment.base import TaxonomicRead
from ttr.projects import paths as ppaths
from ttr.projects.service import create_project
from ttr.species.aliases import bundled_example_alias_text, load_alias_map

from conftest import FakeTaxonomicEnricher

runner = CliRunner()

#: Top-1 is the fox, so `--label VulpesVulpes` is an agreement whichever way the
#: join is configured. The lineage is supplied because a disagreement's distance
#: is measured from it; an agreement does not need it, and a read without one is
#: its own case in test_agreement.
FOX_LINEAGE = {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
               "order": "Carnivora", "family": "Canidae", "genus": "Vulpes",
               "species_epithet": "vulpes"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(r))
    return r


@pytest.fixture
def image(tmp_path):
    """Any bytes: the enricher is faked, so the content is never read."""
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"\xff\xd8\xff\xd9")
    return path


@pytest.fixture
def fake_bioclip(monkeypatch):
    import ttr.enrichment.factory as factory

    read = TaxonomicRead(provider="bioclip", model_name="test-model",
                         topk=[("Vulpes vulpes", 0.91), ("Vulpes lagopus", 0.04)],
                         topk_lineages=[FOX_LINEAGE, FOX_LINEAGE],
                         embedding=[0.1, 0.2, 0.3], ok=True)
    monkeypatch.setattr(factory, "build_taxonomic_enricher",
                        lambda settings: FakeTaxonomicEnricher(read))
    return read


# --------------------------------------------------------------------------- #
# The command, with nothing configured at all.
# --------------------------------------------------------------------------- #
def test_enrich_with_no_project_falls_back_to_the_bundled_tables(root, image,
                                                                 fake_bioclip):
    """The regression: this raised AttributeError instead of falling back."""
    result = runner.invoke(app, ["enrich", "--image", str(image),
                                 "--label", "VulpesVulpes"])

    assert result.exit_code == 0, result.output
    assert "AttributeError" not in result.output
    assert "no project selected" in result.output
    assert "bundled" in result.output
    # It still did the job it exists for.
    assert "Vulpes vulpes" in result.output
    assert "cross-check(VulpesVulpes)" in result.output


def test_enrich_with_no_project_and_no_label_still_classifies(root, image,
                                                              fake_bioclip):
    """Without --label no table is needed, so this path never crashed. Pinned so
    the fix cannot be mistaken for the only thing that works."""
    result = runner.invoke(app, ["enrich", "--image", str(image)])

    assert result.exit_code == 0, result.output
    assert "Vulpes vulpes" in result.output
    assert "cross-check" not in result.output


def test_enrich_uses_a_projects_own_tables_when_one_resolves(root, image,
                                                             fake_bioclip):
    create_project("Alpha", "a@gmail.com", root=root, skip_connection_test=True)

    result = runner.invoke(app, ["enrich", "--image", str(image),
                                 "--label", "VulpesVulpes"])

    assert result.exit_code == 0, result.output
    assert 'species tables from project "Alpha"' in result.output
    assert "no project selected" not in result.output


# --------------------------------------------------------------------------- #
# The loader underneath it.
# --------------------------------------------------------------------------- #
def test_the_alias_loader_accepts_nothing_configured():
    notices = []

    amap = load_alias_map(notify=notices.append)

    assert amap.canonical_key_for_token("VulpesVulpes") == "Vulpes vulpes"
    assert amap.content_sha256, "the bundled table is hashed like any other"
    assert notices and "no alias table configured" in notices[0]


def test_the_alias_loader_accepts_a_settings_object_with_no_path():
    class _NoPath:
        species_alias_path = None

    notices = []
    amap = load_alias_map(_NoPath(), notify=notices.append)

    assert amap.canonical_key_for_token("VulpesVulpes") == "Vulpes vulpes"
    assert notices and "no alias table configured" in notices[0]


def test_a_configured_table_that_is_missing_is_still_reported_as_missing(tmp_path):
    """Distinct from nothing configured: a path that was asked for and is not
    there is a different thing to be told about."""
    class _Configured:
        species_alias_path = tmp_path / "gone.yaml"

    notices = []
    amap = load_alias_map(_Configured(), notify=notices.append)

    assert amap.content_sha256
    assert notices and "not found" in notices[0]
    assert "gone.yaml" in notices[0]


def test_the_fallback_table_is_the_bundled_one_byte_for_byte():
    from ttr.species.aliases import SpeciesAliasMap

    assert (load_alias_map().content_sha256
            == SpeciesAliasMap.from_text(bundled_example_alias_text()).content_sha256)
