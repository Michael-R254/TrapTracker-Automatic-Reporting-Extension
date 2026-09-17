"""Installed-package resource resolution.

Everything the runtime reads must come from PACKAGE DATA, not from a path walked
up from ``__file__`` into the repository. A source checkout hides the difference:
``parents[3] / "config" / ...`` resolves fine in a clone and fails only after
``pip install``, which is the one environment the test suite never exercised.

These tests pin the mechanism rather than the layout, so the failure mode cannot
come back the next time a data file is added.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import ttr
from ttr.species.aliases import (
    SpeciesAliasMap,
    bundled_example_alias_path,
    bundled_example_alias_text,
    load_alias_map,
)

PACKAGE_ROOT = Path(ttr.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]          # src/ttr -> src -> repo root


class _Settings:
    """Minimal stand-in: load_alias_map only reads this one attribute."""

    def __init__(self, species_alias_path):
        self.species_alias_path = species_alias_path


# --------------------------------------------------------------------------- #
# The alias table is package data.
# --------------------------------------------------------------------------- #
def test_bundled_alias_table_lives_inside_the_package():
    """Not in the repo's config/ directory, which does not survive an install."""
    with bundled_example_alias_path() as path:
        assert Path(path).is_file()
        assert PACKAGE_ROOT in Path(path).resolve().parents


def test_bundled_alias_table_is_readable_as_package_data():
    text = bundled_example_alias_text()
    amap = SpeciesAliasMap.from_text(text)
    # A real entry from the supervisor's 31-class list, not just "it parsed".
    assert amap.canonical_key_for_token("VulpesVulpes") == "Vulpes vulpes"
    assert amap.canonical_key_for_token("Person") == "nonbio:Person"


def test_fallback_does_not_walk_out_of_the_package(tmp_path):
    """The failing case in the wild: configured table absent. It must fall back to
    package data — resolution is not optional, since a NULL canonical key drops the
    row out of every query."""
    notices = []
    amap = load_alias_map(_Settings(tmp_path / "nope.yaml"), notify=notices.append)

    assert amap.canonical_key_for_token("VulpesVulpes") == "Vulpes vulpes"
    assert notices and "bundled example" in notices[0]


def test_configured_table_still_wins_when_present(tmp_path):
    table = tmp_path / "aliases.yaml"
    table.write_text(
        "Lutra lutra:\n  raw_tokens: [LutraLutra]\n  common_names: [otter]\n",
        encoding="utf-8")
    amap = load_alias_map(_Settings(table))

    assert amap.canonical_key_for_token("LutraLutra") == "Lutra lutra"
    assert amap.canonical_key_for_token("VulpesVulpes") is None   # not the example


# --------------------------------------------------------------------------- #
# No module may reach out of the package for runtime data.
# --------------------------------------------------------------------------- #
def test_no_runtime_module_walks_up_out_of_the_package():
    """``Path(__file__)...parents[N]`` that escapes ``src/ttr`` is the bug class.

    ``cli.py`` is exempt at one site: ``store --from-fixtures`` deliberately points
    at the repo's test fixtures, guards for their absence, and offers ``--path``.
    Test fixtures are not runtime data and are not shipped.
    """
    pattern = re.compile(r"parents\[(\d+)\]")
    offenders = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "__file__" not in line and "parents[" not in line:
                continue
            for depth in (int(m.group(1)) for m in pattern.finditer(line)):
                # src/ttr/<pkg>/<mod>.py: parents[0]=<pkg>, [1]=ttr. Beyond that is
                # outside the installed package.
                if depth >= 2 and path.name != "cli.py":
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT)}:{n}")
    assert offenders == []


@pytest.mark.parametrize("glob", ["agentdefs/definitions/*.md", "species/data/*.yaml"])
def test_declared_package_data_actually_exists(glob):
    """Every package-data glob in pyproject matches real files. A typo here is
    invisible in a checkout and only bites the installed user."""
    import tomllib

    declared = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["tool"]["setuptools"]["package-data"]["ttr"]

    assert glob in declared, f"{glob} is not declared in pyproject package-data"
    assert list(PACKAGE_ROOT.glob(glob)), f"{glob} matches no files under the package"
