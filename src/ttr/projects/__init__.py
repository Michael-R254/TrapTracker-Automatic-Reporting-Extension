"""Project registry, manifest and lifecycle (Phase 2 Stage 1).

Purely additive. Nothing outside this package reads it yet: `Settings`, the
existing CLI commands, the web app, the pipeline and the storage layer are all
untouched. The composition roots are pointed at `ProjectContext` in Stages 3-6.

What is deliberately NOT here:
  - credentials and the connection test (Stage 2);
  - `project_meta` stamping and the refuse-on-mismatch check (Stage 7);
  - the `--mode link` importer (Stage 8) - the manifest READS an external path,
    but nothing in this stage writes one.
"""

from __future__ import annotations

from .context import ProjectContext
from .errors import (AmbiguousProject, ProjectError, ProjectNotFound,
                     RegistryError, UnsupportedProvider)
from .manifest import Mailbox, ProjectManifest, Site, Species, Storage
from .paths import (directory_name, new_project_id, projects_dir, projects_root,
                    registry_path, slugify)
from .registry import Registry, RegistryEntry

__all__ = [
    "AmbiguousProject",
    "Mailbox",
    "ProjectContext",
    "ProjectError",
    "ProjectManifest",
    "ProjectNotFound",
    "Registry",
    "RegistryEntry",
    "RegistryError",
    "Site",
    "Species",
    "Storage",
    "UnsupportedProvider",
    "directory_name",
    "new_project_id",
    "projects_dir",
    "projects_root",
    "registry_path",
    "slugify",
]
