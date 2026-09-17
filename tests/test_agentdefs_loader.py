"""Agent-definition loader: parsing, validation, path safety, byte-exactness.

These test the loader in isolation — no model, no database, no report. The
separate prompt-equivalence suite covers whether the loaded text still matches
what the system used to send.
"""

from __future__ import annotations

import os

import pytest

from ttr.agentdefs import (AgentDefinitionError, list_agents, load_agent,
                           loaded_agent_versions)
from ttr.agentdefs.loader import DEFAULT_SECTION, _parse

_GOOD = (
    "---\n"
    "name: demo-agent\n"
    "version: 3\n"
    "description: A demo.\n"
    "---\n"
    "Primary instructions.\n"
    "\n"
    "<!-- section: extra -->\n"
    "Second fragment.\n"
)


# ---------------------------------------------------------------- happy path
def test_parses_metadata_and_body():
    d = _parse("demo-agent", _GOOD)
    assert (d.name, d.version, d.description) == ("demo-agent", "3", "A demo.")
    assert d.instructions == "Primary instructions."
    assert d.section("extra") == "Second fragment."
    assert d.label == "demo-agent@3"


def test_metadata_never_appears_in_the_instruction_body():
    """Frontmatter is for the application, not the model."""
    d = _parse("demo-agent", _GOOD)
    for text in d.sections.values():
        assert "name:" not in text
        assert "version:" not in text
        assert "description:" not in text
        assert "---" not in text
        assert "<!-- section:" not in text


def test_body_without_markers_is_a_single_default_section():
    d = _parse("demo-agent",
               "---\nname: demo-agent\nversion: 1\ndescription: d\n---\nOnly this.\n")
    assert list(d.sections) == [DEFAULT_SECTION]
    assert d.instructions == "Only this."


def test_internal_newlines_and_spacing_are_preserved_exactly():
    """Only leading/trailing newlines are stripped; the body is never reflowed."""
    body = "Line one.\nLine two.\n\nLine four with  two spaces."
    d = _parse("demo-agent",
               f"---\nname: demo-agent\nversion: 1\ndescription: d\n---\n{body}\n")
    assert d.instructions == body


def test_crlf_is_normalised_so_a_windows_checkout_cannot_alter_the_prompt():
    d = _parse("demo-agent", _GOOD.replace("\n", "\r\n"))
    assert "\r" not in d.instructions
    assert d.instructions == "Primary instructions."
    assert d.section("extra") == "Second fragment."


def test_unicode_survives_the_round_trip():
    """The real narrative prompt contains an em dash; UTF-8 is explicit."""
    d = _parse("demo-agent",
               "---\nname: demo-agent\nversion: 1\ndescription: d\n---\nA — dash.\n")
    assert d.instructions == "A — dash."


# ------------------------------------------------------------ malformed input
def test_missing_frontmatter_is_an_error():
    with pytest.raises(AgentDefinitionError, match="frontmatter"):
        _parse("demo-agent", "Just a body, no metadata.\n")


def test_malformed_frontmatter_yaml_is_an_error():
    with pytest.raises(AgentDefinitionError, match="malformed frontmatter YAML"):
        _parse("demo-agent",
               "---\nname: [unclosed\nversion: 1\n---\nBody.\n")


def test_non_mapping_frontmatter_is_an_error():
    with pytest.raises(AgentDefinitionError, match="must be a YAML mapping"):
        _parse("demo-agent", "---\n- a\n- b\n---\nBody.\n")


@pytest.mark.parametrize("missing", ["name", "version", "description"])
def test_missing_required_metadata_is_an_error(missing):
    meta = {"name": "demo-agent", "version": "1", "description": "d"}
    del meta[missing]
    src = "---\n" + "".join(f"{k}: {v}\n" for k, v in meta.items()) + "---\nBody.\n"
    with pytest.raises(AgentDefinitionError, match=f"missing required metadata.*{missing}"):
        _parse("demo-agent", src)


def test_frontmatter_name_must_match_the_filename():
    with pytest.raises(AgentDefinitionError, match="must match the filename"):
        _parse("demo-agent",
               "---\nname: other-agent\nversion: 1\ndescription: d\n---\nBody.\n")


def test_empty_section_is_an_error():
    with pytest.raises(AgentDefinitionError, match="empty section"):
        _parse("demo-agent",
               "---\nname: demo-agent\nversion: 1\ndescription: d\n---\n"
               "Body.\n\n<!-- section: blank -->\n")


def test_unknown_section_raises_rather_than_returning_empty_text():
    """A typo must never silently shorten a prompt."""
    d = _parse("demo-agent", _GOOD)
    with pytest.raises(AgentDefinitionError, match="has no section 'nope'"):
        d.section("nope")


# ------------------------------------------------------------- missing / unsafe
def test_missing_definition_gives_a_clear_error_and_no_fallback():
    with pytest.raises(AgentDefinitionError, match="no agent definition named"):
        load_agent("definitely-not-an-agent")


@pytest.mark.parametrize("name", [
    "../secrets", "a/b", "a\\b", "/etc/passwd", "..", ".", "",
    "Report-Narrative", "report_narrative", "report narrative", "report-narrative.md",
])
def test_unsafe_or_malformed_names_are_rejected_before_any_file_access(name):
    with pytest.raises(AgentDefinitionError, match="invalid agent definition name"):
        load_agent(name)


def test_non_string_name_is_rejected():
    with pytest.raises(AgentDefinitionError, match="invalid agent definition name"):
        load_agent(None)


# ------------------------------------------------------- shipped definitions
def test_the_shipped_definitions_are_exactly_the_two_genuine_agents():
    assert list_agents() == ["report-narrative", "vlm-image-describer"]


def test_every_shipped_definition_loads_and_validates():
    for name in list_agents():
        d = load_agent(name)
        assert d.name == name
        assert d.version and d.description
        assert d.instructions.strip()


def test_versions_are_reportable_for_audit_and_diagnostics():
    versions = loaded_agent_versions()
    assert versions == {"report-narrative": "1", "vlm-image-describer": "1"}
    assert all(isinstance(v, str) for v in versions.values())


def test_definitions_resolve_independently_of_the_working_directory(tmp_path):
    """Loading uses package resources, not a path relative to the repo root, so an
    installed copy run from anywhere still finds its instructions."""
    load_agent.cache_clear()
    original = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert load_agent("report-narrative").instructions.strip()
        assert list_agents() == ["report-narrative", "vlm-image-describer"]
    finally:
        os.chdir(original)


def test_definitions_are_cached_so_report_generation_does_not_reread_disk():
    assert load_agent("report-narrative") is load_agent("report-narrative")
