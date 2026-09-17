"""Which project a command operates on - decided in ONE place.

Eleven commands need this answer. Deriving it eleven times would mean eleven
chances for the rules to drift, which is the exact failure ``load_alias_map``'s
docstring names: *"two copies of the fallback rule can disagree."* So the
precedence lives here as code, and every command calls the same function.

Resolution never consults the working directory. ``Settings`` loads its ``.env``
from a CWD-relative path (``config.py:30``), so running ``ttr`` from elsewhere
silently loads nothing; resolving a PROJECT that way would be worse, because the
same command would rewrite a different deployment's database depending on where
it was invoked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..logging import get_logger
from .context import ProjectContext
from .errors import ProjectError
from .registry import Registry, RegistryEntry

logger = get_logger(__name__)

#: Env var naming the project, one step below an explicit flag.
PROJECT_ENV_VAR = "TTR_PROJECT"

#: Provenance labels, printed in the banner. A wrong-project run must be VISIBLE,
#: and "which of the four rules chose this" is the part a user cannot infer.
FROM_FLAG = "from --project"
FROM_ENV = f"from {PROJECT_ENV_VAR}"
FROM_ACTIVE = "active project"
FROM_ONLY = "only project"

#: The two sources that count as the user having SAID which project. Bulk writes
#: accept nothing less once there is more than one project to confuse.
_EXPLICIT = (FROM_FLAG, FROM_ENV)


class NoProjectSelected(ProjectError):
    """No project could be chosen, and guessing is not an option."""


class ProjectNotExplicit(ProjectError):
    """A bulk write defaulted to a project instead of being told one."""


@dataclass(frozen=True)
class Resolution:
    ctx: ProjectContext
    entry: RegistryEntry
    source: str
    #: Non-archived projects in the registry when this resolved.
    visible_count: int = 1

    @property
    def explicit(self) -> bool:
        return self.source in _EXPLICIT

    @property
    def credential_scope(self) -> int:
        """How many projects an unscoped ``TTR_IMAP_PASSWORD`` could be meant for.

        One when the user SAID which project (the same two sources a bulk write
        requires), or when only one project exists. Otherwise it is every
        non-archived project. A defaulted project is exactly the case where a
        password exported for one mailbox reaches another's.
        """
        if self.explicit:
            return 1
        return max(1, self.visible_count)


_NO_PROJECTS = (
    "No projects exist yet.\n"
    "\n"
    "Create one:\n"
    "    ttr project create\n"
    "It asks for a name, the inbox TrapTracker forwards alerts to, and that\n"
    "mailbox's app password."
)

_AMBIGUOUS = (
    "{count} projects exist and none was selected.\n"
    "\n"
    "{listing}\n"
    "Name one - silently picking would run this command against a database you\n"
    "did not choose:\n"
    "    ttr --project <name-or-id> {command}\n"
    "or set a default once:\n"
    "    ttr project use <name-or-id>"
)

_NOT_EXPLICIT = (
    "This command rewrites stored rows, so it will not use a defaulted project.\n"
    "\n"
    "It resolved {name!r} ({source}), but {count} projects exist. A bulk\n"
    "recompute against the wrong one is not obvious afterwards - recompute-crops\n"
    "alone rewrites every event it sees.\n"
    "\n"
    "Say which:\n"
    "    ttr --project <name-or-id> {command}"
)


def _listing(registry: Registry) -> str:
    lines = []
    for e in registry.visible():
        mark = "  (active)" if e.id == registry.active_project else ""
        lines.append(f"  {e.id[:8]}  {e.name}{mark}")
    return "\n".join(lines)


def resolve_project(term: Optional[str] = None, *, root: Optional[Path] = None,
                    command: str = "<command>",
                    require_explicit: bool = False,
                    app_settings=None) -> Resolution:
    """Choose the project this command runs against.

    Precedence, highest first:

      1. ``term`` - the ``--project`` flag.
      2. ``TTR_PROJECT``.
      3. ``active_project`` in the registry.
      4. Exactly one NON-ARCHIVED project exists.
      5. Otherwise: refuse, and list what there is.

    ``require_explicit`` adds one rule for bulk writes: once more than one
    non-archived project exists, rules 3 and 4 are not good enough. Reads may
    default; commands that rewrite hundreds of rows may not.
    """
    registry = Registry.load(root)

    if not registry.entries:
        raise NoProjectSelected(_NO_PROJECTS)

    if term:
        entry, source = registry.resolve(term), FROM_FLAG
    else:
        env_term = os.environ.get(PROJECT_ENV_VAR)
        if env_term:
            entry, source = registry.resolve(env_term), FROM_ENV
        elif registry.active_project:
            entry, source = registry.get(registry.active_project), FROM_ACTIVE
        else:
            visible = registry.visible()
            if len(visible) == 1:
                entry, source = visible[0], FROM_ONLY
            else:
                raise NoProjectSelected(_AMBIGUOUS.format(
                    count=len(visible), listing=_listing(registry),
                    command=command))

    visible_count = len(registry.visible())
    if require_explicit and visible_count > 1 and source not in _EXPLICIT:
        raise ProjectNotExplicit(_NOT_EXPLICIT.format(
            name=entry.name, source=source, count=visible_count, command=command))

    from .service import project_dir_for

    ctx = ProjectContext.load(project_dir_for(entry, root), app=app_settings)
    logger.info("project_resolved",
                extra={"project": entry.id, "source": source, "command": command})
    return Resolution(ctx=ctx, entry=entry, source=source, visible_count=visible_count)


#: How each alias-table source reads in the banner. "bundled example" is named in
#: full deliberately: it is an ILLUSTRATIVE table, not the operator's class list,
#: so a project running on it resolves species it may not detect.
_ALIAS_LABEL = {
    "manifest": "manifest path",
    "project": "project table",
    "bundled": "bundled example",
}


def banner_lines(resolution: Resolution) -> list[str]:
    """The three lines every DB-touching command prints before doing anything.

    The alias line is here, not only in ``project list``, because that is where
    it matters: species resolution is applied at write time and baked into every
    stored row, so which table was in force is a property of the run.
    """
    ctx = resolution.ctx
    return [
        f'using project "{ctx.name}" ({ctx.id[:8]}...) [{resolution.source}]',
        f"  db: {ctx.db_path}",
        f"  aliases: {_ALIAS_LABEL[ctx.alias_table_source()]}",
    ]


def completion(resolution: Resolution, detail: str = "") -> str:
    """The closing line: which project was written to, and where."""
    ctx = resolution.ctx
    tail = f" - {detail}" if detail else ""
    return f'written to project "{ctx.name}" ({ctx.id[:8]}...): {ctx.db_path}{tail}'
