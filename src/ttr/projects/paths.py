"""Where projects live, and what a project directory is called.

The projects root is resolved from the environment and the user's home directory
ONLY. It never depends on the process working directory, deliberately: `Settings`
loads its `.env` from a CWD-relative path (`config.py:30`), so running `ttr` from
another directory silently loads no configuration at all. Reproducing that shape
here would be worse - it would silently resolve a DIFFERENT SET OF PROJECTS
depending on where the user happened to be standing.

A relative ``TTR_PROJECTS_ROOT`` is therefore refused rather than resolved: a
relative override is CWD-dependent by construction, and the failure it causes
(one project set from the repository root, another from anywhere else) is exactly
the class of bug this module exists to avoid.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path

from .errors import ProjectError

#: Environment override. Must be absolute.
ROOT_ENV_VAR = "TTR_PROJECTS_ROOT"

#: Directory name under the platform's user-data location.
_WINDOWS_APP_DIR = "TrapTrackerReport"
_XDG_APP_DIR = "traptracker-report"

#: Registry filename, and the sibling holding the project directories.
REGISTRY_NAME = "registry.toml"
PROJECTS_SUBDIR = "projects"

#: Fixed names inside a project directory. Relative to the project dir, so the
#: whole directory can be moved or synced (the `--mode link` exception in Stage 8
#: is the only absolute path a manifest may carry).
MANIFEST_NAME = "project.toml"
DEFAULT_DATABASE = "detections.sqlite"
DEFAULT_IMAGE_STORE = "images"

_SLUG_MAX = 32
_SLUG_FALLBACK = "project"
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def projects_root() -> Path:
    """The projects root, resolved without reference to the working directory.

    ``TTR_PROJECTS_ROOT`` wins when set. Otherwise the per-OS user data
    directory: ``%LOCALAPPDATA%\\TrapTrackerReport`` on Windows,
    ``$XDG_DATA_HOME/traptracker-report`` or
    ``~/.local/share/traptracker-report`` elsewhere.
    """
    override = os.environ.get(ROOT_ENV_VAR)
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise ProjectError(
                f"{ROOT_ENV_VAR} must be an absolute path; got {override!r}. "
                "A relative value would resolve against whatever directory the "
                "command happened to run from, so the same command would find "
                "different projects depending on where it was invoked.")
        return candidate
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / _WINDOWS_APP_DIR
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / _XDG_APP_DIR


def registry_path(root: Path | None = None) -> Path:
    return (root or projects_root()) / REGISTRY_NAME


def projects_dir(root: Path | None = None) -> Path:
    return (root or projects_root()) / PROJECTS_SUBDIR


def new_project_id() -> str:
    """A project's permanent identity. UUID4, lowercase hex with dashes."""
    return str(uuid.uuid4())


def slugify(name: str) -> str:
    """Display name -> directory-safe slug.

    Lowercased, non-alphanumerics collapsed to a single ``-``, trimmed, truncated
    to 32 characters, non-ASCII dropped. A name that leaves nothing behind (only
    punctuation, or a wholly non-Latin script) falls back to ``project`` - the
    slug is a human hint on a directory name, never identity, so an unhelpful one
    costs readability and nothing else. The id keeps the directory unique.
    """
    ascii_only = name.encode("ascii", "ignore").decode("ascii")
    collapsed = _NON_SLUG.sub("-", ascii_only.lower()).strip("-")
    trimmed = collapsed[:_SLUG_MAX].strip("-")
    return trimmed or _SLUG_FALLBACK


def directory_name(slug: str, project_id: str) -> str:
    """``<slug>-<first 8 of id>``.

    The id suffix makes a collision structurally impossible, so two projects may
    share a display name - and therefore a slug - without contending for a
    directory.
    """
    return f"{slug}-{project_id.replace('-', '')[:8]}"
