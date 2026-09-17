"""``ttr project ...`` - create and manage projects (Phase 2 Stage 1).

A separate module from ``cli.py`` so this stage adds a sub-app without editing a
single existing command. Nothing here changes what any other command does: no
existing command reads a project yet.

Imports are deferred into the handlers, matching ``cli.py``'s own convention, so
``ttr --help`` stays fast.
"""

from __future__ import annotations

import os
from typing import Optional

import typer

app = typer.Typer(
    name="project",
    help="Create and manage projects (each has its own mailbox, database and images).",
    no_args_is_help=True,
)


def _fail(message: str, code: int = 2) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=code)


def _human_bytes(n: int) -> str:
    step = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if step < 1024 or unit == "GB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} GB"


def _root():
    from .projects.paths import projects_root

    return projects_root()


def _read_password(prompt: str = "Mailbox app password") -> str:
    """Read a password from the environment, or prompt for it without echoing.

    There is deliberately NO `--password` flag. A secret on a command line is
    written to shell history and is visible in the process list to every other
    user on the machine, which would undo the point of a design whose whole claim
    is that the password reaches the OS credential store and nothing else.

    The two honest routes are both supported here. Interactively, `getpass` (via
    Typer's `hide_input`) reads it without echo. For scripting and headless hosts,
    `TTR_IMAP_PASSWORD` is read from the environment - the same variable the
    resolver already accepts at connect time, so a CI job or a systemd unit uses
    one mechanism rather than two.
    """
    from .projects.credentials import ENV_UNSCOPED

    from_env = os.environ.get(ENV_UNSCOPED)
    if from_env:
        typer.echo(f"using the app password from {ENV_UNSCOPED}", err=True)
        return from_env
    # The paste hint belongs to the prompt, not to the command: a scripted run
    # that supplied the variable should not be told how to paste something it is
    # never asked for.
    typer.echo("Gmail app password (16 characters; paste it with or without "
               "the spaces).", err=True)
    return typer.prompt(prompt, hide_input=True)


@app.command()
def create(
    name: str = typer.Option(None, "--name", help="Display name for the project."),
    email: str = typer.Option(None, "--email",
                              help="The inbox TrapTracker forwards its alerts to."),
    skip_connection_test: bool = typer.Option(
        False, "--skip-connection-test",
        help="Create without verifying the mailbox (offline setup)."),
) -> None:
    """Create a project: its own mailbox, database and image store.

    Asks three things - a name, the address TrapTracker forwards detection alerts
    to, and that mailbox's app password.

    The address is validated, then the mailbox is actually connected to, and only
    then is anything written. A failure at either step leaves nothing behind:
    no directory, no registry entry, no stored credential.

    Gmail only. Another provider is refused with the reason rather than
    configured on a guessed IMAP host.
    """
    from .projects.errors import ProjectError
    from .projects.service import create_project

    from .projects.providers import preset_for

    if not name:
        name = typer.prompt("Project name")
    if not email:
        email = typer.prompt("Alert inbox address (the Gmail address alerts arrive at)")

    # Validate the domain BEFORE asking for a password. Otherwise a user with an
    # outlook.com address types a 16-character secret and only then learns the
    # provider cannot work at all.
    try:
        preset_for(email)
    except ProjectError as exc:
        _fail(f"error: {exc}")

    password = None
    if not skip_connection_test:
        password = _read_password()

    try:
        manifest, project_dir = create_project(
            name, email, password, root=_root(),
            skip_connection_test=skip_connection_test)
    except ProjectError as exc:
        _fail(f"error: {exc}")

    typer.echo(f"created project {manifest.name!r}")
    typer.echo(f"  id        {manifest.id}")
    typer.echo(f"  directory {project_dir}")
    typer.echo(f"  mailbox   {manifest.mailbox.address} via {manifest.mailbox.imap_host}"
               f" ({manifest.mailbox.imap_folder})")
    typer.echo(f"  database  {manifest.db_path(project_dir)}")
    typer.echo(f"  images    {manifest.image_store_dir(project_dir)}")
    typer.echo("")
    if skip_connection_test:
        typer.echo("WARNING: the mailbox was NOT verified and no credential is "
                   "stored.")
        typer.echo("Nothing has confirmed that this address, password or folder "
                   "is usable - ingestion will be the first thing to find out.")
        from .projects.credentials import supply_step

        typer.echo(f"Next, {supply_step(manifest.id)}, when you are online.")
    else:
        typer.echo(f"  credential stored ({manifest.mailbox.credential_ref})")
    typer.echo("Site coordinates are unset; `ttr project set-site` adds them "
               "(weather enrichment refuses to run without them rather than "
               "guessing a location).")


@app.command(name="import")
def import_cmd(
    name: str = typer.Option(..., "--name", help="Display name for the project."),
    db: str = typer.Option(..., "--db", help="The database to adopt."),
    images: str = typer.Option(..., "--images", help="The image store to adopt."),
    seen: str = typer.Option(None, "--seen",
                             help="The seen-store. Defaults to seen_message_ids.txt "
                                  "beside the database."),
    email: str = typer.Option(None, "--email",
                              help="Alert inbox for the imported project. Without "
                                   "one the project can report but not ingest."),
    from_env: bool = typer.Option(False, "--from-env",
                                  help="Read .env for SUGGESTED mailbox values."),
    alias_table: str = typer.Option(None, "--alias-table",
                                    help="The alias table these rows were resolved "
                                         "under. Strongly recommended."),
    mode: str = typer.Option("copy", "--mode",
                             help="copy (default), move, or link."),
    backfill_alias_hash: bool = typer.Option(
        False, "--backfill-alias-hash",
        help="Stamp existing rows with the supplied table's hash. An ASSERTION "
             "by you, not a recovered fact."),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Print the plan and write nothing."),
) -> None:
    """Adopt an existing database and image store as a project.

    Copies by default and leaves the originals exactly where they are, so they
    remain the backup. The database is copied WITH its -wal/-shm sidecars and the
    COPY is checkpointed; the source is opened read-only and never written.

    Image paths are rewritten to be image-store-relative, which is what makes the
    project self-contained: afterwards its rows resolve from the project
    directory whatever the working directory happens to be.

    The verdict digest is verified after the move and the import FAILS if it
    changed. A migration moves bytes; it does not re-decide anything.
    """
    from pathlib import Path

    from .projects.errors import ProjectError
    from .projects.importer import SEEN_STORE_NAME, build_plan, run_import
    from .projects.manifest import Mailbox, Site

    db_path = Path(db)
    seen_path = Path(seen) if seen else db_path.parent / SEEN_STORE_NAME

    mailbox = Mailbox()
    site = Site()
    missing_from_env: list[str] = []
    if from_env:
        # SUGGESTIONS only. Those keys are no longer read by anything, so this is
        # a convenience for the one moment they are still the best surviving
        # record of what the mailbox was.
        #
        # Reads the FILE as well as the process environment: `.env` values are
        # not exported, so an os.environ-only lookup finds nothing in exactly the
        # case this flag exists for.
        suggested = _read_env_file()
        suggested.update({k: v for k, v in os.environ.items() if v})
        mailbox.address = mailbox.imap_user = suggested.get("IMAP_USER", "") or ""
        mailbox.imap_host = suggested.get("IMAP_HOST", "") or ""
        folder = suggested.get("IMAP_FOLDER")
        if folder:
            mailbox.imap_folder = folder
        # The SITE is on the same removed list as the mailbox, so `.env` is
        # equally the last place it survives. Suggested here for the same reason,
        # and its ABSENCE reported rather than left to look like a deliberate
        # "unset" -- the same distinction the deprecation notice draws.
        if not mailbox.imap_user:
            missing_from_env.append("IMAP_USER")
        if not mailbox.imap_host:
            missing_from_env.append("IMAP_HOST")
        site.name = suggested.get("SITE_NAME") or None
        if not site.name:
            missing_from_env.append("SITE_NAME")
        for key, attr in (("WEATHER_LATITUDE", "latitude"),
                          ("WEATHER_LONGITUDE", "longitude")):
            raw = suggested.get(key)
            if not raw:
                missing_from_env.append(key)
                continue
            try:
                setattr(site, attr, float(raw))
            except ValueError:
                missing_from_env.append(f"{key} (not a number: {raw!r})")
        # Coordinates are set together or not at all: half a pair is not a
        # location, and weather enrichment would have to guess the other half.
        if (site.latitude is None) != (site.longitude is None):
            site.latitude = site.longitude = None
            missing_from_env.append(
                "(only one coordinate found - both are needed, so neither is used)")
    if email:
        mailbox.address = mailbox.imap_user = email
        if not mailbox.imap_host:
            from .projects.providers import preset_for

            try:
                mailbox.imap_host = preset_for(email).imap_host
            except ProjectError as exc:
                _fail(f"error: {exc}")

    try:
        plan = build_plan(
            name=name, db_source=db_path, images_source=Path(images),
            seen_source=seen_path if seen_path.is_file() else None,
            alias_source=Path(alias_table) if alias_table else None,
            mode=mode, root=_root())
    except ProjectError as exc:
        _fail(f"error: {exc}")

    _print_plan(plan, mailbox, site, missing_from_env,
                backfill_alias_hash=backfill_alias_hash, from_env=from_env)

    if plan.collisions:
        _fail("\nerror: refusing to import - see the collision report above.")

    if dry_run:
        typer.echo("")
        typer.echo("dry run - nothing written")
        return

    try:
        manifest, plan = run_import(
            plan, root=_root(), mailbox=mailbox, site=site,
            backfill_alias_hash=backfill_alias_hash,
            notify=lambda m: typer.echo(f"  {m}"))
    except ProjectError as exc:
        _fail(f"error: {exc}", code=1)

    typer.echo("")
    typer.echo(f"imported {manifest.name!r} ({manifest.id})")
    typer.echo(f"  {plan.project_dir}")
    typer.echo("")
    typer.echo(f"originals left untouched at {plan.db_source.parent} - this is your backup")
    if not plan.alias_table_supplied:
        typer.echo("")
        _alias_warning()
    if not mailbox.imap_user:
        typer.echo("")
        typer.echo("No mailbox is configured, so this project can report on the "
                   "imported data but cannot ingest.")
        from .projects.credentials import supply_step

        typer.echo(f"Edit [mailbox] in its project.toml, then {supply_step(manifest.id)}.")


def _read_env_file(path: str = ".env") -> dict:
    """Bare KEY=VALUE pairs from a .env, for --from-env's suggestions.

    Parsed directly rather than through pydantic-settings: `Settings` no longer
    declares these keys, so it would discard them (`extra="ignore"`) - which is
    the whole reason the values need recovering here.
    """
    import pathlib

    found: dict = {}
    file = pathlib.Path(path)
    if not file.is_file():
        return found
    try:
        for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip("\"'")
            if value:
                found[key.strip().upper()] = value
    except OSError:
        pass
    return found


def _alias_warning() -> None:
    typer.echo("WARNING: no --alias-table was given.")
    typer.echo("  The imported rows' canonical_binomial values were resolved under")
    typer.echo("  SOME alias table, and this project does not have it. It will fall")
    typer.echo("  back to the bundled illustrative example, so NEWLY ingested events")
    typer.echo("  may resolve to different species from the historical ones -")
    typer.echo("  silently, inside one corpus.")
    typer.echo("  Re-run with --alias-table <path>, or copy the table to")
    typer.echo("  <project>/species_aliases.yaml.")


def _print_plan(plan, mailbox, site=None, missing_from_env=(), *,
                backfill_alias_hash: bool, from_env: bool = False) -> None:
    typer.echo(f"project      {plan.name}")
    typer.echo(f"id           {plan.project_id}")
    typer.echo(f"directory    {plan.project_dir}")
    typer.echo(f"mode         {plan.mode}")
    typer.echo("")
    typer.echo("DATABASE")
    typer.echo(f"  from       {plan.db_source}  ({_human_bytes(plan.db_bytes)})")
    typer.echo(f"  sidecars   {', '.join(plan.sidecars) or '(none)'}"
               f"   -> copied, then the COPY is checkpointed")
    typer.echo(f"  to         {plan.db_dest}")
    typer.echo(f"  rows       {plan.row_count} detection event(s)")
    typer.echo(f"  digest     {plan.source_digest}")
    typer.echo("             (the migrated copy must reproduce this exactly)")
    typer.echo("  ownership  unstamped with rows -> adopted with a notice")
    typer.echo("")
    typer.echo("IMAGES")
    typer.echo(f"  from       {plan.images_source}")
    typer.echo(f"  to         {plan.images_dest}")
    for root_name, count in sorted(plan.roots_seen.items()):
        typer.echo(f"    {count:5d} path(s) under {root_name!r}")
    typer.echo("")
    typer.echo("  every directory in the store falls in exactly one row below,")
    typer.echo("  and the total reconciles to the measured size:")
    typer.echo(f"    COPY  referenced             {len(plan.referenced):5d} dir  "
               f"{_human_bytes(plan.referenced_bytes):>10}")
    typer.echo(f"    DROP  orphan (top level)     {len(plan.orphans):5d} dir  "
               f"{_human_bytes(plan.orphan_bytes):>10}   referenced by no row")
    typer.echo(f"    DROP  orphan (inside a root) {len(plan.nested_orphans):5d} dir  "
               f"{_human_bytes(plan.nested_orphan_bytes):>10}   referenced by no row")
    typer.echo(f"    DROP  loose files            {len(plan.loose_files):5d} file "
               f"{_human_bytes(plan.loose_bytes):>10}   not inside any message-id dir")
    typer.echo(f"    {'':5s}{'-' * 58}")
    typer.echo(f"    {'':5s}accounted                    "
               f"{_human_bytes(plan.accounted_bytes):>10}")
    typer.echo(f"    {'':5s}measured store size          "
               f"{_human_bytes(plan.store_bytes):>10}")
    if plan.unaccounted_bytes:
        typer.echo(f"    {'':5s}UNACCOUNTED                  "
                   f"{_human_bytes(plan.unaccounted_bytes):>10}  <-- investigate")
    else:
        typer.echo(f"    {'':5s}unaccounted                        0 B")
    typer.echo("")
    typer.echo("  Sizes are the sum of file bytes. A disk-usage figure (du -sh) reads")
    typer.echo("  higher - each file and directory is rounded up to a whole cluster.")
    typer.echo("")
    typer.echo("  paths      rewritten to <message-id>/<file>, forward slashes,")
    typer.echo("             resolved against the project's own images/")
    if plan.collisions:
        typer.echo("")
        typer.echo(f"  COLLISION  {len(plan.collisions)} message-id(s) referenced under "
                   f"BOTH roots:")
        for mid in plan.collisions[:20]:
            typer.echo(f"               {mid}")
        typer.echo("             Flattening would make one silently overwrite the")
        typer.echo("             other. Import refuses.")
    else:
        typer.echo("  collision  none - the two roots share no referenced message-id")
    typer.echo("")
    typer.echo("SEEN-STORE")
    if plan.seen_source:
        lines = sum(1 for line in plan.seen_source.read_text(
            encoding="utf-8", errors="replace").splitlines() if line.strip())
        typer.echo(f"  from       {plan.seen_source}  ({lines} message-id(s))")
        typer.echo(f"  to         {plan.seen_dest}")
        typer.echo("             Without this the first `ttr run` re-downloads and")
        typer.echo("             re-processes the whole mailbox - harmless but long,")
        typer.echo("             and it re-persists images.")
    else:
        typer.echo("  NOT FOUND - the first `ttr run` will re-download and")
        typer.echo("  re-process the entire mailbox. Pass --seen <path>.")
    typer.echo("")
    typer.echo("ALIAS TABLE")
    if plan.alias_source:
        typer.echo(f"  from       {plan.alias_source}")
        typer.echo(f"  to         {plan.alias_dest}  (+ alter_tables snapshot)"
                   .replace("alter_tables", "alias_tables"))
        typer.echo(f"  backfill   {'yes - an assertion by you' if backfill_alias_hash else 'no - existing rows stay NULL'}")
    else:
        typer.echo("  (none given)")
        _alias_warning()
    typer.echo("")
    typer.echo("MANIFEST")
    typer.echo(f"  mailbox    {mailbox.imap_user or '(none configured)'}"
               f"{'  via ' + mailbox.imap_host if mailbox.imap_host else ''}")
    typer.echo("  credential <from keyring>")
    site_name = (site.name if site and site.name else plan.name)
    typer.echo(f"  site       {site_name}")
    if site and site.latitude is not None:
        typer.echo(f"  coords     {site.latitude}, {site.longitude}")
    else:
        typer.echo("  coords     (unset - `ttr backfill-weather` will refuse to run)")
    if from_env and missing_from_env:
        typer.echo("")
        typer.echo("  --from-env could NOT find these in .env or the environment:")
        for key in missing_from_env:
            typer.echo(f"    {key}")
        typer.echo("  Absent is reported rather than left looking like a deliberate")
        typer.echo("  'unset'. Supply them explicitly, or set them afterwards with")
        typer.echo("  `ttr project set-site`.")
    typer.echo("")
    typer.echo(f"originals at {plan.db_source.parent} are NOT modified - they are "
               f"your backup")


@app.command(name="set-password")
def set_password_cmd(
    term: str = typer.Argument(..., help="Project name or id."),
    skip_connection_test: bool = typer.Option(
        False, "--skip-connection-test",
        help="Store without verifying it against the mailbox."),
) -> None:
    """Store or rotate this project's mailbox app password.

    The password is verified against the mailbox before it is stored, so a
    password that has just been shown not to work is never saved.

    It goes into the operating system's credential store. The project's
    project.toml records only a reference to it, never the value.
    """
    from .projects import credentials
    from .projects.errors import ProjectError
    from .projects.service import set_password

    # Before the prompt: asking for a secret there is nowhere to keep is worse
    # than refusing straight away.
    if not credentials.backend_available():
        _fail("error: this machine has no OS credential store, so there is nowhere to "
              "keep a password, and there is deliberately no file-based fallback.\n"
              "Supply it through the environment instead. "
              "`ttr project test-connection <project>` names the exact variable.")

    password = _read_password()

    try:
        manifest, result = set_password(
            term, password, root=_root(),
            skip_connection_test=skip_connection_test)
    except ProjectError as exc:
        _fail(f"error: {exc}")

    if result is not None:
        typer.echo(result.message)
        typer.echo("")
    typer.echo(f"stored credential for {manifest.name!r}")
    typer.echo(f"  reference {manifest.mailbox.credential_ref}")
    typer.echo("  the password itself is in the OS credential store, not in the "
               "project directory")
    if skip_connection_test:
        typer.echo("WARNING: stored without verifying it against the mailbox.")


@app.command(name="test-connection")
def test_connection_cmd(
    term: str = typer.Argument(..., help="Project name or id."),
) -> None:
    """Connect to this project's mailbox and report what is there.

    Reports which credential source was used (the source, never the value), the
    outcome, and on success how many messages the configured folder holds.
    """
    from .projects.connection import check_project
    from .projects.errors import ProjectError
    from .projects.registry import Registry
    from .projects.service import context_for

    root = _root()
    try:
        entry = Registry.load(root).resolve(term)
        ctx = context_for(entry, root)
    except ProjectError as exc:
        _fail(f"error: {exc}")

    typer.echo(f"project {ctx.name} ({ctx.id})")
    typer.echo(f"mailbox {ctx.mailbox.imap_user} via {ctx.mailbox.imap_host} "
               f"folder {ctx.mailbox.imap_folder!r}")
    result = check_project(ctx)
    if result.credential_source:
        typer.echo(f"credential: {result.credential_source}")
    typer.echo("")
    typer.echo(result.message)
    if not result.ok:
        raise typer.Exit(code=1)


@app.command(name="list")
def list_projects(
    show_all: bool = typer.Option(False, "--all",
                                  help="Include archived projects."),
) -> None:
    """List projects. Archived ones are hidden unless --all."""
    from .projects.context import ProjectContext
    from .projects.errors import ProjectError
    from .projects.manifest import ProjectManifest
    from .projects.registry import Registry
    from .projects.service import project_dir_for

    #: What each alias-table source means, spelled out. "bundled" in particular
    #: is worth naming: it is an ILLUSTRATIVE table, not the operator's class
    #: list, so a project running on it resolves species it may not actually
    #: detect and fails to resolve ones it does.
    alias_note = {
        "manifest": "aliases: manifest path",
        "project": "aliases: project table",
        "bundled": "aliases: bundled example",
    }

    root = _root()
    try:
        registry = Registry.load(root)
    except ProjectError as exc:
        _fail(f"error: {exc}")

    entries = registry.entries if show_all else registry.visible()
    if not entries:
        hidden = len(registry.entries) - len(entries)
        note = f" ({hidden} archived - use --all)" if hidden else ""
        typer.echo(f"no projects in {root}{note}")
        return

    for e in entries:
        marks = []
        if e.id == registry.active_project:
            marks.append("active")
        if e.archived:
            marks.append("archived")
        detail = ""
        try:
            project_dir = project_dir_for(e, root)
            manifest = ProjectManifest.read(project_dir)
            if manifest.is_linked:
                marks.append("linked")
            ctx = ProjectContext.from_manifest(manifest, project_dir)
            detail = alias_note[ctx.alias_table_source()]
            if ctx.mailbox.credential_ref is None:
                marks.append("no credential")
        except ProjectError:
            marks.append("UNREADABLE MANIFEST")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        typer.echo(f"{e.id[:8]}  {e.name}{suffix}")
        typer.echo(f"          {e.directory}" + (f"   {detail}" if detail else ""))


@app.command()
def path(term: str = typer.Argument(..., help="Project name or id.")) -> None:
    """Print a project's directory. For scripting."""
    from .projects.errors import ProjectError
    from .projects.registry import Registry
    from .projects.service import project_dir_for

    root = _root()
    try:
        entry = Registry.load(root).resolve(term)
    except ProjectError as exc:
        _fail(f"error: {exc}")
    typer.echo(str(project_dir_for(entry, root)))


@app.command()
def use(term: str = typer.Argument(..., help="Project name or id.")) -> None:
    """Make a project active."""
    from .projects.errors import ProjectError
    from .projects.service import use_project

    try:
        entry = use_project(term, root=_root())
    except ProjectError as exc:
        _fail(f"error: {exc}")
    typer.echo(f"active project: {entry.name} ({entry.id})")


@app.command()
def rename(
    term: str = typer.Argument(..., help="Project name or id."),
    new_name: str = typer.Argument(..., help="New display name."),
) -> None:
    """Change a project's display name. Nothing moves on disk."""
    from .projects.errors import ProjectError
    from .projects.service import rename_project

    try:
        entry, previous = rename_project(term, new_name, root=_root())
    except ProjectError as exc:
        _fail(f"error: {exc}")
    typer.echo(f"renamed {previous!r} -> {entry.name!r}")
    typer.echo(f"  directory and id are unchanged ({entry.directory})")
    typer.echo(f"  scripts that reference this project by name should use its id "
               f"instead: {entry.id}")


@app.command()
def archive(term: str = typer.Argument(..., help="Project name or id.")) -> None:
    """Hide a project from listings and from project selection.

    Keeps every byte. This is the reversible alternative to `delete`.
    """
    from .projects.errors import ProjectError
    from .projects.service import set_archived

    try:
        entry = set_archived(term, True, root=_root())
    except ProjectError as exc:
        _fail(f"error: {exc}")
    typer.echo(f"archived {entry.name!r} - data kept; `ttr project list --all` "
               f"still shows it, and it stays resolvable by id.")


@app.command()
def unarchive(term: str = typer.Argument(..., help="Project name or id.")) -> None:
    """Return an archived project to listings and selection."""
    from .projects.errors import ProjectError
    from .projects.service import set_archived

    try:
        entry = set_archived(term, False, root=_root())
    except ProjectError as exc:
        _fail(f"error: {exc}")
    typer.echo(f"unarchived {entry.name!r}")


@app.command(name="set-site")
def set_site_cmd(
    term: str = typer.Argument(..., help="Project name or id."),
    name: str = typer.Option(None, "--name", help="Site name, printed in reports."),
    latitude: float = typer.Option(None, "--latitude", help="Camera latitude."),
    longitude: float = typer.Option(None, "--longitude", help="Camera longitude."),
) -> None:
    """Set the site name and/or the camera's coordinates.

    The location is the CAMERA's, never the animal's. Coordinates must be given
    together.
    """
    from .projects.errors import ProjectError
    from .projects.service import set_site

    if name is None and latitude is None and longitude is None:
        _fail("error: nothing to set - pass --name and/or --latitude with --longitude")
    try:
        manifest = set_site(term, name=name, latitude=latitude,
                            longitude=longitude, root=_root())
    except ProjectError as exc:
        _fail(f"error: {exc}")
    typer.echo(f"site name: {manifest.site.name or '(unset)'}")
    if manifest.site.latitude is None:
        typer.echo("coordinates: (unset) - weather enrichment will refuse to run")
    else:
        typer.echo(f"coordinates: {manifest.site.latitude}, {manifest.site.longitude}")


@app.command()
def delete(
    term: str = typer.Argument(..., help="Project name or id."),
    yes: bool = typer.Option(False, "--yes", help="Skip the typed confirmation."),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Print the plan and stop."),
    force: bool = typer.Option(False, "--force",
                               help="Delete even if the directory holds "
                                    "unaccounted files."),
) -> None:
    """Delete a project: its directory, its database, and its registry entry.

    Prints exactly what will go, then asks for the project's name typed back.
    A linked (`--mode link`) database or image store is never removed - the
    project does not own it.

    `--dry-run` exists but is not the default: a destructive command that
    usually does nothing trains a reflexive `--yes`, which is worse than no
    flag at all.
    """
    from .projects.errors import ProjectError
    from .projects.service import delete_project, removal_plan

    root = _root()
    try:
        plan = removal_plan(term, root=root)
    except ProjectError as exc:
        _fail(f"error: {exc}")

    typer.echo(f"project   {plan.entry.name}  ({plan.entry.id})")
    typer.echo(f"directory {plan.project_dir}")
    typer.echo(f"contents  {plan.file_count} files, {_human_bytes(plan.total_bytes)}")
    if plan.foreign_database_owner:
        typer.echo(f"database  BELONGS TO ANOTHER PROJECT "
                   f"({plan.foreign_database_owner[:8]}...) - row count withheld")
        typer.echo("          this project's [storage] database path points at a "
                   "database")
        typer.echo("          stamped for a different project. Check the manifest "
                   "before")
        typer.echo("          deleting anything.")
    elif plan.row_count is not None:
        typer.echo(f"database  {plan.row_count} detection events")
    else:
        typer.echo("database  (unreadable or absent - no row count)")
    for target in plan.external:
        typer.echo(f"left in place (external): {target}")
    if plan.unaccounted:
        typer.echo("unaccounted files (not described by the manifest):")
        for p in plan.unaccounted:
            typer.echo(f"  {p.name}")

    if dry_run:
        typer.echo("dry run - nothing deleted")
        return

    if not yes:
        typed = typer.prompt(f"Type the project name to confirm deletion")
        if typed.strip() != plan.entry.name:
            _fail("name did not match - nothing deleted", code=1)

    try:
        plan = delete_project(term, root=root, force=force)
    except ProjectError as exc:
        _fail(f"error: {exc}")

    typer.echo(f"deleted {plan.entry.name!r}")
    for target in plan.external:
        typer.echo(f"left in place (external): {target}")
    if plan.credential_error:
        typer.echo(f"WARNING: the stored mailbox credential was NOT removed: "
                   f"{plan.credential_error}")
        typer.echo(f"         it is still in your credential store under "
                   f"{plan.entry.id}; remove it by hand if that matters to you.")
    elif plan.credential_removed:
        typer.echo("stored mailbox credential removed")


@app.command(name="import-extract")
def import_extract_cmd(
    csv_path: str = typer.Option(..., "--csv", metavar="PATH",
                                 help="A published evaluation extract (CSV)."),
    name: str = typer.Option(..., "--name", help="Display name for the project."),
    alias_table: str = typer.Option(
        None, "--alias-table", metavar="PATH",
        help="The alias table these rows were resolved under. Strongly "
             "recommended: without it the project falls back to the bundled "
             "illustrative table, which is not this data's class list."),
    site_name: str = typer.Option(None, "--site-name", help="Optional site label."),
    latitude: float = typer.Option(None, "--latitude", help="Optional, with --longitude."),
    longitude: float = typer.Option(None, "--longitude", help="Optional, with --latitude."),
) -> None:
    """Create a project from a published evaluation extract.

    An extract is the image-free, identifier-free CSV this project publishes so
    its findings can be reproduced without redistributing camera imagery or
    transport metadata. This opens one AS A PROJECT, so the reports the study
    describes can be regenerated rather than only read about.

    The project has NO MAILBOX and therefore cannot ingest: the extract has no
    inbox behind it, and inventing a plausible address so one could be configured
    would be offering to poll a mailbox that does not exist. `ttr project
    set-password` adds one later if you want to point it at a real inbox.

    Rows are written exactly as the CSV records them. Nothing is re-enriched and
    no verdict is recomputed -- a loader that re-decided `agreement_flag` would
    produce a project that disagreed with the evidence file it came from.
    """
    from pathlib import Path

    from .projects.errors import ProjectError
    from .projects.extract import create_from_extract

    source = Path(csv_path)
    try:
        manifest, project_dir, ctx, stored = create_from_extract(
            source, name,
            alias_table=Path(alias_table) if alias_table else None,
            site_name=site_name, latitude=latitude, longitude=longitude,
            root=_root())
    except ProjectError as exc:
        _fail(f"error: {exc}")

    typer.echo(f"Created {manifest.name!r} from {source.name}")
    typer.echo(f"  {stored} rows loaded  -  alias table: {ctx.alias_table_source()}")
    typer.echo(f"  {project_dir}")
    typer.echo("")
    typer.echo("  No mailbox is configured, so this project can report but not "
               "ingest.")
    typer.echo(f"  Open it with:  ttr serve   (then pick {manifest.name!r})")


@app.command(name="load-example")
def load_example_cmd() -> None:
    """Build the worked example project from the data bundled with the package.

    `ttr serve` does this by itself the first time it starts against an empty
    projects root. This builds it again on request, for instance after deleting
    it, or alongside projects that already exist.
    """
    from .projects.errors import ProjectError
    from .projects.example import build_example_project

    try:
        manifest, project_dir, ctx, stored = build_example_project(_root())
    except ProjectError as exc:
        _fail(f"error: {exc}")

    typer.echo(f"Created {manifest.name!r} from the published example data")
    typer.echo(f"  {stored} rows loaded  -  alias table: {ctx.alias_table_source()}")
    typer.echo(f"  {project_dir}")
    typer.echo(f"  Open it with:  ttr serve   (then pick {manifest.name!r})")
