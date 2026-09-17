"""Markdown-defined runtime agent definitions and their loader.

A neutral package on purpose. Both an *agent* (``agents/report.py``) and an
*enricher* (``enrichment/ollama_enricher.py``) are prompt-driven and need their
instructions loaded, but the layering rule (no stage imports another) keeps them
decoupled. Putting the loader in either one would create exactly the cross-stage
import the rule forbids, so it lives beside them instead and both depend only on
this.
"""

from .loader import (
    DEFAULT_SECTION,
    AgentDefinition,
    AgentDefinitionError,
    list_agents,
    load_agent,
    loaded_agent_versions,
)

__all__ = [
    "AgentDefinition",
    "AgentDefinitionError",
    "DEFAULT_SECTION",
    "list_agents",
    "load_agent",
    "loaded_agent_versions",
]
