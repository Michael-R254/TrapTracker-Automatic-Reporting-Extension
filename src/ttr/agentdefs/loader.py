"""Loader for Markdown-defined runtime agent definitions.

An *agent definition* is a version-controlled ``.md`` file holding the
instructions that materially define one LLM-driven component's behaviour. Only
components whose behaviour is defined by prompt text have one: the report
narrative summariser and the VLM image describer. Deterministic code — retrieval,
effort/downtime arithmetic, the BioCLIP corroboration join, charts, HTML, PDF —
has no definition here and never will, because none of it is prompt-driven.

File format
-----------
YAML frontmatter (parsed with ``yaml.safe_load``; PyYAML is already a core
dependency) followed by the instruction body::

    ---
    name: report-narrative
    version: 1
    description: ...
    ---
    <instruction text handed to the model>

    <!-- section: outage-system -->
    <a second, separately-addressable fragment>

Metadata is for this loader and never reaches the model. A body may carry more
than one fragment because a single agent can legitimately need several — a system
prompt and a user prompt, or a conditional suffix — and inventing one file per
fragment would misrepresent how many agents exist. Fragments are separated by an
HTML comment marker, which Markdown does not render and which therefore cannot
leak into the prompt.

Byte-exactness
--------------
The body is model input, so it is treated as bytes, not as prose to be tidied:

* files are read as UTF-8 explicitly (the narrative prompt contains an em dash);
* CRLF/CR are normalised to LF, so a Windows checkout with ``core.autocrlf``
  cannot silently alter what the model receives;
* each fragment is stripped of leading/trailing newlines only — never of internal
  whitespace, and never reflowed.

Consequently the long single-line paragraphs in the definition files must stay on
one line. Re-wrapping them for readability would insert newlines into the prompt
and change model input. ``tests/test_agent_prompt_equivalence.py`` fails if that
happens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping

import yaml

#: Fragment name for the body text before any explicit section marker — the
#: agent's primary instructions.
DEFAULT_SECTION = "instructions"

#: Definitions ship inside the package (not at the repository root) so they are
#: found by the same import machinery as the code, whether the project is run
#: from a checkout, installed into a virtualenv, or installed as a wheel from a
#: different working directory.
_DEFINITIONS_DIR = "definitions"

#: Definition names are identifiers chosen by this codebase, never user input.
#: The pattern excludes ``.``, ``/`` and ``\``, so a name can never escape the
#: definitions directory even if one day it did come from outside.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_SECTION_RE = re.compile(r"^<!--\s*section:\s*([a-z0-9][a-z0-9-]*)\s*-->[ \t]*$", re.MULTILINE)

_REQUIRED_METADATA = ("name", "version", "description")


class AgentDefinitionError(RuntimeError):
    """A definition is missing, malformed, or fails validation.

    Always raised — never swallowed in favour of a default prompt. A silent
    fallback would let the system quietly run on instructions nobody authored,
    which is exactly the failure this refactor exists to make impossible.
    """


@dataclass(frozen=True)
class AgentDefinition:
    """One loaded Markdown agent definition."""

    name: str
    version: str
    description: str
    sections: Mapping[str, str]

    @property
    def instructions(self) -> str:
        """The primary instruction body — the text before any section marker."""
        return self.section(DEFAULT_SECTION)

    def section(self, name: str) -> str:
        """A named fragment of the body. Unknown names raise rather than return
        an empty string, so a typo cannot quietly shorten a prompt."""
        try:
            return self.sections[name]
        except KeyError:
            known = ", ".join(sorted(self.sections)) or "(none)"
            raise AgentDefinitionError(
                f"agent definition {self.name!r} (v{self.version}) has no section "
                f"{name!r}; defined sections: {known}"
            ) from None

    @property
    def label(self) -> str:
        """Short identifier for logs and developer diagnostics, e.g.
        ``report-narrative@1``."""
        return f"{self.name}@{self.version}"


def _read_source(name: str) -> str:
    from importlib.resources import files

    try:
        resource = files(__package__).joinpath(_DEFINITIONS_DIR, f"{name}.md")
        return resource.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AgentDefinitionError(
            f"no agent definition named {name!r}: expected "
            f"{__package__}/{_DEFINITIONS_DIR}/{name}.md. Definitions ship inside "
            f"the package; if this is an installed copy, check that package data "
            f"was included in the distribution."
        ) from None


def _split_sections(body: str) -> dict[str, str]:
    """Split a body into named fragments at ``<!-- section: x -->`` markers.

    Leading/trailing newlines are stripped from each fragment; nothing else is
    altered. A marker with no text after it yields an empty fragment, which
    ``_parse`` rejects.
    """
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        return {DEFAULT_SECTION: body.strip("\n")}

    sections = {DEFAULT_SECTION: body[: matches[0].start()].strip("\n")}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[m.group(1)] = body[m.end(): end].strip("\n")
    return sections


def _parse(name: str, source: str) -> AgentDefinition:
    # Normalise line endings BEFORE anything else: on Windows a checkout may hold
    # CRLF, and a stray \r inside a prompt is a silent change to model input.
    source = source.replace("\r\n", "\n").replace("\r", "\n")

    match = _FRONTMATTER_RE.match(source)
    if match is None:
        raise AgentDefinitionError(
            f"agent definition {name!r} is malformed: it must begin with a YAML "
            f"frontmatter block delimited by '---' lines, followed by the "
            f"instruction body."
        )
    raw_meta, body = match.group(1), match.group(2)

    try:
        meta = yaml.safe_load(raw_meta)
    except yaml.YAMLError as exc:
        raise AgentDefinitionError(
            f"agent definition {name!r} has malformed frontmatter YAML: {exc}"
        ) from exc

    if not isinstance(meta, dict):
        raise AgentDefinitionError(
            f"agent definition {name!r} frontmatter must be a YAML mapping, got "
            f"{type(meta).__name__}."
        )

    missing = [k for k in _REQUIRED_METADATA if meta.get(k) in (None, "")]
    if missing:
        raise AgentDefinitionError(
            f"agent definition {name!r} is missing required metadata: "
            f"{', '.join(missing)} (required: {', '.join(_REQUIRED_METADATA)})."
        )

    if meta["name"] != name:
        raise AgentDefinitionError(
            f"agent definition {name!r} declares name {meta['name']!r}; the "
            f"frontmatter name must match the filename."
        )
    if not isinstance(meta["version"], (int, str)):
        raise AgentDefinitionError(
            f"agent definition {name!r} has a non-scalar version "
            f"({type(meta['version']).__name__}); use an integer or a string."
        )

    sections = _split_sections(body)
    empty = sorted(k for k, v in sections.items() if not v)
    if empty:
        raise AgentDefinitionError(
            f"agent definition {name!r} has empty section(s): {', '.join(empty)}. "
            f"An empty instruction fragment is never intentional."
        )

    return AgentDefinition(
        name=meta["name"],
        version=str(meta["version"]),
        description=str(meta["description"]),
        sections=MappingProxyType(dict(sections)),
    )


@lru_cache(maxsize=None)
def load_agent(name: str) -> AgentDefinition:
    """Load and validate the agent definition called ``name``.

    ``name`` is a definition identifier (e.g. ``report-narrative``), never a
    path: it is validated against ``_NAME_RE`` and resolved through the package's
    own resource loader, so no caller can point it at an arbitrary file.

    Raises ``AgentDefinitionError`` if the name is invalid, the file is absent,
    the frontmatter is malformed, required metadata is missing, or a section is
    empty. There is deliberately no fallback prompt.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise AgentDefinitionError(
            f"invalid agent definition name {name!r}: expected lowercase "
            f"hyphen-separated words, e.g. 'report-narrative'."
        )
    return _parse(name, _read_source(name))


def list_agents() -> list[str]:
    """Every definition name shipped with the package, sorted — for diagnostics."""
    from importlib.resources import files

    directory = files(__package__).joinpath(_DEFINITIONS_DIR)
    return sorted(p.name[:-3] for p in directory.iterdir() if p.name.endswith(".md"))


def loaded_agent_versions() -> dict[str, str]:
    """``{name: version}`` for every shipped definition — the audit/debug hook.

    Deliberately NOT surfaced in any generated report: adding it would change
    published output. It is for logs, tests and developer diagnostics.
    """
    return {name: load_agent(name).version for name in list_agents()}
