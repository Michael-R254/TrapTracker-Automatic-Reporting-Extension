"""Typer CLI entry point (console script: ``ttr``).

Every subcommand is implemented; ``ttr --help`` is the command surface. Handlers
import their dependencies lazily so that ``--help``, and commands that need
neither models nor a mailbox, stay fast and do not require the optional extras.
"""

from __future__ import annotations

import typer

from .cli_project import app as _project_app
from .logging import configure_logging

app = typer.Typer(
    name="ttr",
    help="Reporting & enrichment layer for TrapTracker RT alert emails.",
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(_project_app, name="project")


@app.callback()
def _main(
    tctx: typer.Context,
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Which project to operate on. Overrides TTR_PROJECT and the active "
             "project."),
) -> None:
    """Configure logging, and record which project was asked for.

    ``--project`` is a GLOBAL option, declared once here rather than on each of
    eleven commands, so ``ttr --project X <anything>`` works uniformly and the
    precedence rules have exactly one implementation (see
    ``projects/resolve.py``). Resolution itself is deferred to the commands that
    need it, so ``--help`` and the project-management sub-app stay free of it.
    """
    configure_logging("DEBUG" if verbose else "INFO")
    tctx.obj = {"project_term": project}


def _echo_document(text: str) -> None:
    """Print a rendered report to stdout without letting the console mangle it.

    A report is a DOCUMENT - the same markdown goes to file, web and PDF - and it
    legitimately contains typography (em dashes, arrows, degree signs) that a
    Windows console under cp1252 cannot encode. Python raises
    ``UnicodeEncodeError`` and the command dies AFTER all the work is done, which
    is the worst possible place to fail.

    Fixed at the STREAM, never in the document. Flattening the typography to suit
    one terminal would damage the artefact for every other surface in order to fix
    the one surface that matters least. So stdout is asked for UTF-8, and told to
    replace what it still cannot render rather than raise: a report that prints
    with a substituted glyph is readable, and one that raises is not.

    ``--out`` does not come through here; it writes UTF-8 to the file unchanged.
    """
    import sys

    stream = getattr(sys, "stdout", None)
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):        # detached, or a stream that refuses
            pass
    try:
        typer.echo(text)
    except UnicodeEncodeError:
        # A stream that could not be reconfigured (a strict wrapper in a test, an
        # exotic redirect). Substitute rather than lose a generated report.
        enc = getattr(stream, "encoding", None) or "utf-8"
        typer.echo(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _resolve(tctx: typer.Context, local_project: str = None, *, command: str,
             require_explicit: bool = False, banner: bool = True,
             app_settings=None):
    """Resolve the project for a command, print the banner, return the context.

    ``--project`` may be given globally (``ttr --project X report``) or on the
    command itself (``ttr report --project X``). Both forms arrive here, so there
    is still ONE implementation of the precedence rules - the second form is a
    spelling, not a second rule.

    Giving BOTH, with different values, is an error rather than a silent
    precedence. Two spellings that disagree is exactly the case where guessing
    runs a command against a database the user did not choose, and no rule for
    picking between them would be memorable enough to rely on.

    ``banner=False`` is for commands that touch no database (``enrich``), where
    announcing a project would overstate what is being used.
    """
    from .projects.errors import ProjectError
    from .projects.resolve import banner_lines, resolve_project

    global_term = (tctx.obj or {}).get("project_term")
    if global_term and local_project and global_term != local_project:
        typer.echo(
            f"error: --project was given twice with different values "
            f"({global_term!r} before the command, {local_project!r} after it). "
            f"Give it once.", err=True)
        raise typer.Exit(code=2)
    term = local_project or global_term
    try:
        resolution = resolve_project(term, command=command,
                                     require_explicit=require_explicit,
                                     app_settings=app_settings)
    except ProjectError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    if banner:
        for line in banner_lines(resolution):
            typer.echo(line, err=True)
    # NOTE: commands call `ctx.load_alias_map()` WITHOUT a notify callback. The
    # banner's `aliases:` line already says which table is in force, and routing
    # the loader's own fallback notice to stderr as well would print it on every
    # command for every project without a local table — background noise nobody
    # reads, which is worse than the silence it replaced.
    return resolution


def _done(resolution, detail: str = "", *, dry_run: bool = False) -> str:
    """The closing line. Keeps the existing "written to <db>" shape and prepends
    the project, so a completed run says which deployment it changed."""
    from .projects.resolve import completion

    if dry_run:
        return "dry run - nothing written"
    return completion(resolution, detail)


@app.command()
def ingest(
    tctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Project to operate on. Same as the global --project; give one "
             "form or the other, not both."),
) -> None:
    """Poll the alert mailbox and print what parses, without storing anything.

    Connects to this project's mailbox and prints a one-line summary per parsed
    event. This proves the ingestion path; it does NOT persist.

    Because it stores nothing, it also retires nothing: the seen-store is only
    advanced by the consumer that durably stores an event (see
    ``DetectionSource.mark_processed``). Running this never causes a later
    ``ttr run`` to skip mail - which it used to.

    It still takes the ingest lock: it downloads the same mail a real run would,
    and running it beside one wastes the bandwidth twice over.
    """
    import functools

    from .projects.errors import ProjectError
    from .projects.lock import ingest_lock
    from .sources.email_fetcher import EmailFetcher
    from .sources.email_source import EmailSource
    from .sources.seen_store import SeenStore

    resolution = _resolve(tctx, project, command="ingest")
    ctx = resolution.ctx

    # This command builds its own fetcher rather than a pipeline, so it asks for
    # itself. `ProjectError` covers both refusals below: a held lock and a
    # project with no inbox both carry their own actionable message.
    try:
        ctx.require_mailbox()
    except ProjectError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    # The seen-store is built first so the fetcher can consult it and skip
    # DOWNLOADING mail already handled — see EmailFetcher.fetch_unseen.
    seen = SeenStore(ctx.seen_store_path)
    fetcher = EmailFetcher(
        host=ctx.mailbox.imap_host, user=ctx.mailbox.imap_user,
        password=functools.partial(ctx.imap_password,
                                   projects_in_scope=resolution.credential_scope),
        folder=ctx.mailbox.imap_folder,
        use_ssl=ctx.mailbox.imap_use_ssl,
        lookback_days=ctx.mailbox.imap_lookback_days,
        is_known=seen.__contains__)
    source = EmailSource(fetcher, seen)

    try:
        with ingest_lock(ctx.dir):
            count = 0
            for ev in source.poll():
                count += 1
                warn = f" warnings={len(ev.parse_warnings)}" if ev.parse_warnings else ""
                typer.echo(
                    f"{ev.upstream_event_time_utc} {ev.upstream_label} "
                    f"conf={ev.upstream_best_confidence} images={len(ev.images)}{warn}"
                )
    except ProjectError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    typer.echo(f'ingested {count} new event(s) for project "{ctx.name}"', err=True)


@app.command()
def store(
    tctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Project to operate on. Same as the global --project; give one "
             "form or the other, not both."),
    from_fixtures: bool = typer.Option(
        False, "--from-fixtures", help="Parse .eml files and persist them."
    ),
    path: str = typer.Option(
        None, "--path",
        help="Directory of .eml files to read. Defaults to the repository's "
             "tests/fixtures/emails, which exists only in a source checkout.",
    ),
) -> None:
    """Parse a directory of alert emails and persist them to the database.

    ``--from-fixtures`` runs the full parse->resolve->persist path over a
    directory of alert emails, so storage is demonstrable without a live inbox.
    Species are resolved to canonical keys via the alias table (so the rows are
    queryable by the retrieval agent); it does not write image files (that is the
    ``run`` pipeline's job).

    The default directory lives in the source tree, not in the installed package
    - test fixtures are not runtime data and are not shipped. Installed users
    pass ``--path`` to a directory of their own ``.eml`` files.
    """
    if not from_fixtures:
        typer.echo("nothing to do - pass --from-fixtures, or use `ttr run` for the live loop", err=True)
        raise typer.Exit(code=1)

    from pathlib import Path

    from .sources.base import NotAnAlertEmail
    from .sources.email_parser import parse_alert_email
    from .storage.mapping import persisted_from_detection_event

    import email
    from email.policy import default as default_policy

    resolution = _resolve(tctx, project, command="store")
    ctx = resolution.ctx
    alias_map = ctx.load_alias_map()

    # parents[2] reaches the repo root ONLY from a source checkout. Installed into
    # site-packages it points somewhere unrelated, so the absence is reported as an
    # actionable message instead of an empty run or a traceback.
    emails_dir = (Path(path) if path
                  else Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "emails")
    if not emails_dir.is_dir():
        typer.echo(
            f"error: no such directory: {emails_dir}\n"
            "The bundled fixtures ship with the SOURCE checkout, not with the "
            "installed package. Pass --path <dir> to point at a directory of .eml "
            "files, or run this from a clone of the repository.", err=True)
        raise typer.Exit(code=2)

    repo = ctx.open_repo()
    stored = skipped = 0
    try:
        for eml in sorted(emails_dir.rglob("*.eml")):
            msg = email.message_from_bytes(eml.read_bytes(), policy=default_policy)
            try:
                ev = parse_alert_email(msg)
            except NotAnAlertEmail:
                skipped += 1
                continue
            repo.upsert_event(persisted_from_detection_event(ev, alias_map=alias_map))
            stored += 1
    finally:
        repo.close()
    typer.echo(_done(resolution,
                     f"stored {stored} event(s), skipped {skipped} non-alert(s)"),
               err=True)


@app.command(name="backfill-capture-time")
def backfill_capture_time(
    tctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Project to operate on. Same as the global --project; give one "
             "form or the other, not both."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would change, write nothing."),
) -> None:
    """Re-derive capture times from stored image filenames.

    Reads each row's ``original_image_path``, decodes the Reolink filename block
    to a UTC capture time, and writes ONLY the capture_time_* columns - the alert
    send time is never overwritten, so the divergence between the two survives as
    evidence. Idempotent: re-running recomputes the same values. Needs no network
    and no image files on disk (the filename alone carries the timestamp).
    """
    from collections import Counter

    from .sources.capture_time import parse_capture_time

    # A bulk write: it rewrites the capture_time_* columns of every row it sees,
    # so it will not run against a project it merely defaulted to.
    resolution = _resolve(tctx, project, command="backfill-capture-time",
                          require_explicit=not dry_run)
    repo = resolution.ctx.open_repo()
    try:
        rows = repo.iter_for_backfill()
        stats: Counter = Counter()
        moved = 0
        for event_id, path, send in rows:
            read = parse_capture_time(path, send_time_utc=send)
            stats[read.source] += 1
            if not read.tz_validated and read.capture_time_utc is not None:
                stats["tz_unvalidated"] += 1
            if (read.from_filename and send is not None
                    and read.capture_time_utc.date() != send.date()):
                moved += 1
            if not dry_run:
                repo.set_capture_time(event_id, read)
        typer.echo(f"rows examined:            {len(rows)}", err=True)
        typer.echo(f"capture time from filename: {stats['filename_block_d']}", err=True)
        typer.echo(f"fell back to send time:     {stats['send_time_fallback']}", err=True)
        typer.echo(f"no time at all:             {stats['none']}", err=True)
        typer.echo(f"tz outside validated BST:   {stats['tz_unvalidated']}", err=True)
        typer.echo(f"events that CHANGE DAY:     {moved}", err=True)
        typer.echo(_done(resolution, f"{len(rows)} row(s) examined",
                         dry_run=dry_run), err=True)
    finally:
        repo.close()


@app.command(name="backfill-weather")
def backfill_weather(
    tctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Project to operate on. Same as the global --project; give one "
             "form or the other, not both."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would change, write nothing."),
) -> None:
    """Enrich stored detections with historical weather (Open-Meteo archive).

    For each detection, matches its UTC CAPTURE time (falling back to send time,
    flagged) to the nearest hourly weather record at the camera's location, and
    writes the weather_* columns. Responses are cached per-date so repeated runs
    make no API calls. Additive: no existing column is touched.
    """
    from collections import Counter
    from datetime import timezone

    from .config import get_settings
    from .enrichment.weather import WeatherClient, enrich_weather, weather_time_basis

    resolution = _resolve(tctx, project, command="backfill-weather",
                          require_explicit=not dry_run)
    ctx = resolution.ctx
    settings = get_settings()          # machine-level: endpoint and timeout only

    # No coordinates means no site. Matching a detection against weather for an
    # unconfigured location would invent context, so refuse rather than guess.
    # Now per PROJECT: each deployment carries its own camera location, and one
    # project having coordinates says nothing about another.
    if ctx.site.latitude is None or ctx.site.longitude is None:
        typer.echo(
            f'error: project "{ctx.name}" has no camera location configured.\n'
            f"Set one:\n"
            f"    ttr project set-site {ctx.id[:8]} --latitude <lat> --longitude <lon>\n"
            "Coordinates have no default: weather matched to the wrong place "
            "would be fabricated context.",
            err=True)
        raise typer.Exit(code=2)

    repo = ctx.open_repo()
    try:
        events = repo.iter_for_weather_backfill()
        # Prefetch the cache over the whole span of match instants, in one API call.
        instants = [weather_time_basis(e)[0] for e in events]
        instants = [t.astimezone(timezone.utc) for t in instants if t is not None]
        client = WeatherClient(
            latitude=ctx.site.latitude, longitude=ctx.site.longitude,
            endpoint=settings.weather_endpoint, cache=repo,
            timeout_seconds=settings.weather_timeout_seconds)
        if instants:
            lo, hi = min(instants).date(), max(instants).date()
            typer.echo(f"ensuring weather cache for {lo} .. {hi} ...", err=True)
            client.ensure_range(lo, hi)

        stats: Counter = Counter()
        for ev in events:
            read = enrich_weather(ev, client)
            stats[read.category] += 1
            if read.used_send_fallback:
                stats["_send_fallback"] += 1
            if not dry_run and ev.id is not None:
                repo.set_weather(ev.id, read)
        typer.echo(f"detections examined: {len(events)}", err=True)
        for cat in ("sunny", "cloudy", "rainy", "unknown", "unavailable"):
            typer.echo(f"  {cat:<12} {stats.get(cat, 0)}", err=True)
        typer.echo(f"  matched to send time (no capture time): {stats.get('_send_fallback', 0)}", err=True)
        typer.echo(_done(resolution, f"{len(events)} detection(s) examined",
                         dry_run=dry_run), err=True)
    finally:
        repo.close()


@app.command(name="recompute-crosscheck")
def recompute_crosscheck(
    tctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Project to operate on. Same as the global --project; give one "
             "form or the other, not both."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would change, write nothing."),
    backfill_lineage: bool = typer.Option(
        True, "--backfill-lineage/--no-backfill-lineage",
        help="Recover each top-1's full taxonomy from BioCLIP's own label table "
             "(needs the [enrich] extra). Without it, rows stored before hierarchy "
             "capture get a verdict but no measurable taxonomic distance."),
) -> None:
    """Re-decide the BioCLIP cross-check over every stored detection.

    Re-reads the SAME stored evidence - the upstream label and BioCLIP's stored
    top-k - and re-applies the two-state, species-level decision, filling in the
    audit columns (resolution basis, matched rank, taxonomic distance). No image is
    re-classified, no model runs, and nothing upstream of the cross-check is touched.

    Prints a full old-flag x new-flag crosstab and FAILS LOUDLY if any previously
    agreed row now disagrees or vice versa: better matching should only ever explain
    what was unexplained, never reverse a verdict that was already decided.
    """
    from collections import Counter

    from .config import get_settings
    from .enrichment.agreement import compute_crosscheck
    from .enrichment.base import TaxonomicRead
    # A bulk write over every stored row: explicit project required.
    resolution = _resolve(tctx, project, command="recompute-crosscheck",
                          require_explicit=not dry_run)
    ctx = resolution.ctx
    settings = get_settings()          # machine-level: BioCLIP checkpoint only
    alias_map = ctx.load_alias_map()
    taxonomy = ctx.load_target_taxonomy()

    index = None
    if backfill_lineage:
        from .enrichment.bioclip_taxonomy import TolLineageIndex
        try:
            typer.echo("loading BioCLIP's label table for lineage backfill ...", err=True)
            index = TolLineageIndex.load(settings.bioclip_model)
            typer.echo(f"  {len(index)} binomials "
                       f"({index.merged_count} conflicting entries merged to agreed ranks)",
                       err=True)
        except RuntimeError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=2)

    repo = ctx.open_repo()
    try:
        rows = repo.iter_for_crosscheck_recompute()
        flags: Counter = Counter()
        bases: Counter = Counter()
        distances: Counter = Counter()
        crosstab: Counter = Counter()
        reversals: list[str] = []
        unresolved: Counter = Counter()

        for event_id, label, ok, error, topk, old_flag in rows:
            topk = [tuple(entry) for entry in topk]
            lineage = index.lineage_for(topk[0][0]) if (index and topk) else None
            read = TaxonomicRead(
                provider="bioclip", model_name=settings.bioclip_model, topk=topk,
                topk_lineages=[lineage.as_mapping()] if lineage else [],
                ok=ok, error=error,
            )
            check = compute_crosscheck(label, read, alias_map, taxonomy,
                                       use_topk=settings.agreement_topk_join)
            flags[check.flag] += 1
            bases[check.resolution_basis] += 1
            distances[check.taxonomic_distance] += 1
            crosstab[(old_flag, check.flag)] += 1
            # A verdict that was already decided must not swap sides. Moving OUT of
            # the retired 'indeterminate' state is the point of the change; agree
            # <-> disagree is a regression and is reported as one.
            if old_flag in ("agree", "disagree") and check.flag != old_flag:
                reversals.append(
                    f"  id={event_id} {old_flag} -> {check.flag}: {check.rationale}")
            if check.flag == "disagree" and check.taxonomic_distance == "undetermined":
                unresolved[topk[0][0] if topk else "(no top-1)"] += 1
            if not dry_run:
                repo.set_crosscheck(event_id, check, lineage)

        total = len(rows)
        typer.echo(f"\ndetections examined: {total}", err=True)
        typer.echo("verdict:", err=True)
        for flag in ("agree", "disagree", "not_evaluable"):
            typer.echo(f"  {flag:<16} {flags.get(flag, 0)}", err=True)
        evaluable = flags.get("agree", 0) + flags.get("disagree", 0)
        typer.echo(f"  (binary over {evaluable} evaluable; "
                   f"{flags.get('not_evaluable', 0)} excluded)", err=True)

        typer.echo("\nresolution basis:", err=True)
        for basis, n in bases.most_common():
            typer.echo(f"  {basis:<30} {n}", err=True)
        typer.echo("\ntaxonomic distance:", err=True)
        for dist, n in distances.most_common():
            typer.echo(f"  {dist:<30} {n}", err=True)

        typer.echo("\nold flag -> new flag:", err=True)
        for (old, new), n in sorted(crosstab.items(), key=lambda kv: -kv[1]):
            typer.echo(f"  {str(old):<16} -> {new:<16} {n}", err=True)

        if unresolved:
            typer.echo(f"\ndisagreements with an unmeasurable distance "
                       f"({sum(unresolved.values())}):", err=True)
            for taxon, n in unresolved.most_common(20):
                typer.echo(f"  {n:>4}  {taxon}", err=True)

        if reversals:
            typer.echo(f"\nREGRESSION: {len(reversals)} verdict(s) reversed sides - "
                       f"this must be explained before the result is used:", err=True)
            for line in reversals[:50]:
                typer.echo(line, err=True)

        typer.echo("\ndry run - nothing written" if dry_run
                   else "\n" + _done(resolution, f"{len(rows)} row(s) re-decided"),
                   err=True)
        if reversals:
            raise typer.Exit(code=1)
    finally:
        repo.close()


@app.command()
def enrich(
    tctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Project to operate on. Same as the global --project; give one "
             "form or the other, not both."),
    image: str = typer.Option(..., "--image", help="Path to an image to classify with BioCLIP."),
    label: str = typer.Option(None, "--label", help="Optional upstream token to compute agreement against."),
) -> None:
    """Run the BioCLIP taxonomic cross-check over a single image.

    Requires the [enrich] extra and downloads weights on first run. If --label
    and a species alias table are available, also prints the agreement flag.

    Touches no database. ``--project`` is honoured for the SPECIES TABLES only -
    which alias and taxonomy tables the ``--label`` comparison is made against -
    and with no project it falls back to the bundled tables with a notice, the
    same way ``load_alias_map`` already behaves for an unset path. No banner is
    printed, because announcing a project would overstate what is being used.
    """
    from pathlib import Path

    from .config import get_settings
    from .enrichment.factory import build_taxonomic_enricher

    settings = get_settings()
    data = Path(image).read_bytes()
    read = build_taxonomic_enricher(settings).classify(data)

    if not read.ok:
        typer.echo(f"BioCLIP failed: {read.error}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"top-{len(read.topk)} taxa ({read.model_name}):")
    for taxon, score in read.topk:
        typer.echo(f"  {taxon:<28} {score:.4f}")
    typer.echo(f"embedding dim: {len(read.embedding) if read.embedding else 0}")

    if label:
        from .enrichment.agreement import compute_crosscheck
        from .projects.errors import ProjectError
        from .species.aliases import load_alias_map
        from .species.taxonomy import load_target_taxonomy

        # A project's tables when one can be resolved; the bundled tables plus a
        # notice otherwise. Never a hard failure: this command's job is to
        # classify one image, and it can still do that with no project at all.
        try:
            ctx = _resolve(tctx, project, command="enrich", banner=False).ctx
            alias_map = ctx.load_alias_map(
                notify=lambda msg: typer.echo(msg, err=True))
            taxonomy = ctx.load_target_taxonomy()
            typer.echo(f'[species tables from project "{ctx.name}"]', err=True)
        except (ProjectError, typer.Exit):
            typer.echo("[no project selected; comparing against the bundled "
                       "species tables]", err=True)
            # NOTHING configured, rather than `settings`: the alias and taxonomy
            # paths are per-deployment and left `Settings` for projects, so
            # passing it here read an attribute that no longer exists and
            # crashed instead of falling back.
            alias_map = load_alias_map(notify=lambda msg: typer.echo(msg, err=True))
            taxonomy = load_target_taxonomy()

        check = compute_crosscheck(
            label, read, alias_map, taxonomy,
            use_topk=settings.agreement_topk_join,
        )
        typer.echo(f"cross-check({label}): {check.flag} - {check.rationale}")
        typer.echo(f"  status={check.status} basis={check.resolution_basis} "
                   f"matched_rank={check.matched_rank} distance={check.taxonomic_distance}")


@app.command()
def report(
    tctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Project to operate on. Same as the global --project; give one "
             "form or the other, not both."),
    species: str = typer.Option(None, "--species", help="Common name or scientific binomial."),
    all_species: bool = typer.Option(False, "--all-species", help="Summary across every species instead."),
    bng_aligned: bool = typer.Option(False, "--bng-aligned",
                                     help="BNG-aligned monitoring report (habitat-condition "
                                          "evidence; NOT a Biodiversity Gain Plan, no units)."),
    window: str = typer.Option("7d", "--window", help="Window ending now, e.g. 7d, 24h, 2w."),
    no_evidence_images: bool = typer.Option(
        False, "--no-evidence-images",
        help="Withhold Appendix A's representative camera frames, and say so in the "
             "report. For a sample distributed publicly."),
    out: str = typer.Option(None, "--out", help="Write markdown here (UTF-8); default prints to stdout."),
) -> None:
    """Generate a day-by-day species report.

    Reads only from the DB via the retrieval agent; renders prose via the report
    LLM, degrading to a figures-only report if the LLM is unavailable. The three
    honesty statements and all counts are always present regardless of the LLM.
    """
    from pathlib import Path

    from .agents.report import ReportGeneratorAgent
    from .agents.retrieval import RetrievalAgent
    from .agents.window import TimeWindow
    from .config import get_settings
    from .llm.ollama_client import OllamaLLMClient
    from .storage.repository import DetectionRepository

    if sum([species is not None, all_species, bng_aligned]) != 1:
        typer.echo("error: pass exactly one of --species X, --all-species, or --bng-aligned",
                   err=True)
        raise typer.Exit(code=2)

    # Validate the window before opening any resources so a typo is a friendly
    # error, not a traceback.
    try:
        parsed_window = TimeWindow.parse(window)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    # A read: it may default to the active or only project. Bulk WRITES may not.
    resolution = _resolve(tctx, project, command="report")
    ctx = resolution.ctx
    settings = get_settings()          # machine-level: LLM + report thresholds
    alias_map = ctx.load_alias_map()
    repo = ctx.open_repo()
    llm = OllamaLLMClient(
        model=settings.ollama_model,
        endpoint=settings.ollama_endpoint,
        timeout_seconds=settings.ollama_timeout_seconds,
    )
    agent = ReportGeneratorAgent(
        RetrievalAgent(repo, alias_map), llm,
        low_confidence_threshold=settings.confidence_low_flag_threshold,
        include_crosscheck=settings.report_include_crosscheck,
        # The deployment's own name — the agent constructor is unchanged; only
        # where the value comes from moved.
        site_name=ctx.site.name,
        # Appendix A embeds camera frames of the deployment. Withholding them is
        # stated IN the document rather than done quietly: the report says the
        # frames were generated and deliberately not published, which is a
        # different claim from the "unavailable" note that fires when the images
        # genuinely cannot be read.
        include_evidence_images=not no_evidence_images,
    )
    try:
        if all_species:
            markdown = agent.generate_all_species(parsed_window)
        elif bng_aligned:
            markdown = agent.generate_bng_aligned(parsed_window)
        else:
            markdown = agent.generate(species, parsed_window)
    finally:
        # Matches every sibling command. Without it a failure mid-generation
        # leaked the connection, and an inconsistent pattern is what gets copied.
        repo.close()

    if out:
        Path(out).write_text(markdown, encoding="utf-8")
        typer.echo(f"wrote report to {out}", err=True)
    else:
        _echo_document(markdown)


@app.command()
def run(
    tctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Project to operate on. Same as the global --project; give one "
             "form or the other, not both."),
    once: bool = typer.Option(False, "--once", help="Poll once and exit (default loops)."),
) -> None:
    """Run the full poll -> enrich -> store loop until interrupted.

    ONE project per process. Running several is several processes - two
    terminals, two systemd units, two Task Scheduler entries - which gives real
    failure isolation and independent restart for free, rather than inventing a
    supervisor inside this command.

    Holds the project's ingest lock for the whole run. A second ingest against
    the same project is refused rather than allowed to duplicate downloads and
    silently rewrite images (see ``projects/lock.py``).

    Requires the [enrich] extra (BioCLIP) plus Ollama for the VLM. Each
    single-detection failure degrades gracefully and never halts the loop.
    """
    import threading

    from .pipeline import build_pipeline_for
    from .projects.errors import ProjectError
    from .projects.lock import ingest_lock

    resolution = _resolve(tctx, project, command="run")
    ctx = resolution.ctx
    # Event.wait on a never-set event is exactly sleep(), but it makes the sleep
    # primitive identical to the web runner's interruptible one.
    idle = threading.Event()

    try:
        with ingest_lock(ctx.dir):
            # The SAME wiring the web ingest page uses, so the two cannot drift.
            bundle = build_pipeline_for(
                ctx, projects_in_scope=resolution.credential_scope)
            try:
                while True:
                    n = bundle.pipeline.run_once()
                    typer.echo(f'stored {n} new event(s) in "{ctx.name}"', err=True)
                    if once:
                        break
                    idle.wait(bundle.poll_seconds)
            finally:
                bundle.close()
    # A held lock and a project with no inbox are both `ProjectError`s carrying
    # their own actionable message, so both read as one line rather than as a
    # traceback out of the fetcher.
    except ProjectError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)


#: Addresses that reach only this machine. Anything else is reachable by others.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"})


def is_loopback(host: str) -> bool:
    """Whether binding to ``host`` keeps the server on this machine.

    Pure, so the refusal below is testable without starting a server. Anything
    unrecognised is treated as NON-loopback: the failure that matters is
    exposing the UI by accident, so an unfamiliar address must not be waved
    through on the assumption it is local.
    """
    return (host or "").strip().lower() in _LOOPBACK_HOSTS


#: Bind addresses meaning "every interface". A server can listen on one - Docker
#: needs it, since a published port reaches the container's own interface, not its
#: loopback - but a browser cannot open one: `http://0.0.0.0:8000` is
#: ERR_ADDRESS_INVALID in Chrome and Edge on Windows.
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "[::]"})


def browser_host(bind_host: str) -> str:
    """The host to put in a URL a person opens, for a server bound to ``bind_host``.

    A wildcard bind is reachable on this machine as ``localhost``. Under Docker
    that is the host's loopback, where `docker/compose.yaml` publishes the port.
    Any other address is returned as given, bracketed if it is an IPv6 literal.
    """
    host = (bind_host or "").strip()
    if host.lower() in _WILDCARD_HOSTS:
        return "localhost"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


class BrowserHostLogFilter:
    """Rewrites uvicorn's "Uvicorn running on http://0.0.0.0:8000" line.

    Only the host changes, and only for a wildcard bind, so the log names an
    address a browser can open rather than contradicting the URL `serve` printed.
    """

    def filter(self, record) -> bool:          # noqa: A003 - logging's own name
        if (isinstance(record.msg, str) and record.msg.startswith("Uvicorn running on")
                and isinstance(record.args, tuple) and len(record.args) == 3
                and isinstance(record.args[1], str)
                and record.args[1].strip().lower() in _WILDCARD_HOSTS):
            protocol, _, port = record.args
            record.args = (protocol, browser_host(record.args[1]), port)
        return True


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host (local only by default)."),
    port: int = typer.Option(8000, help="Bind port."),
    allow_remote: bool = typer.Option(
        False, "--i-understand-no-auth",
        help="Required to bind a non-loopback address. The UI has no authentication.",
    ),
) -> None:
    """Launch the local web UI (needs the optional web extra).

    A viva demonstration interface over the report generator; reads the DB via
    the same agents the CLI uses. Reports (honesty notes included) are produced by
    the agent and rendered server-side; the page holds no report logic.

    A SESSION TOKEN is minted at startup (or taken from TTR_UI_TOKEN) and printed
    in the URL below; every route refuses without it. ``/ingest`` WRITES - it polls the mailbox and stores what
    it finds - so before the token, any process on this machine could trigger a
    real ingest. Binding off loopback still needs saying so explicitly: the token
    is not transport security, and this is plain HTTP.
    """
    if not is_loopback(host) and not allow_remote:
        typer.echo(
            f"error: refusing to bind {host} - the demo UI has NO authentication, and\n"
            "/api/ingest/start triggers a real mailbox poll that writes to your\n"
            "database and image store. Anyone who can reach this port can run it.\n"
            "\n"
            "  * to demo locally:        ttr serve            (binds 127.0.0.1)\n"
            "  * to reach another host:  use an SSH tunnel, e.g.\n"
            "        ssh -L 8000:127.0.0.1:8000 <user>@<this-machine>\n"
            "  * to bind anyway:         ttr serve --host " + host +
            " --i-understand-no-auth\n",
            err=True)
        raise typer.Exit(code=2)

    try:
        import uvicorn
    except ImportError:
        typer.echo("The web UI needs the [web] extra: pip install -e \".[web]\"", err=True)
        raise typer.Exit(code=1)

    import logging
    import os

    from .projects.example import ensure_example_project
    from .web.app import app as web_app
    from .web.auth import (
        UI_TOKEN_ENV,
        InvalidFixedToken,
        RedactTokenFilter,
        fixed_token_from,
        mint_token,
        set_ui_token,
    )

    # By default minted here and handed to the app IN PROCESS, never written to an
    # environment variable or a file: both are readable by other processes running
    # as this user, which is most of what the token exists to stop. TTR_UI_TOKEN
    # opts out of that for a URL that survives restarts (`auth.py`).
    try:
        fixed = fixed_token_from(os.environ)
    except InvalidFixedToken as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)
    # Removed once read, so the processes this server starts - the PDF browser
    # among them - do not inherit it.
    os.environ.pop(UI_TOKEN_ENV, None)
    token = fixed or mint_token()
    set_ui_token(token)

    # uvicorn's access log records the full request line, query string included -
    # verified by probe. The one request that carries the token would otherwise
    # write it to the log in clear, outliving the session it belongs to.
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(RedactTokenFilter())
    logging.getLogger("uvicorn.error").addFilter(BrowserHostLogFilter())

    # A fresh install opens on the worked example rather than an empty page.
    ensure_example_project(notify=lambda message: typer.echo(message, err=True))

    if not is_loopback(host):
        typer.echo(
            f"WARNING: bound to {host} over plain HTTP - the session token is not "
            f"encrypted in transit, and anyone who obtains it can trigger ingestion "
            f"and read your reports.", err=True)

    # `host` is where the server LISTENS; the printed URL is where a browser GOES.
    # They differ under Docker, which binds 0.0.0.0. The port is the same on both
    # sides: `docker/compose.yaml` publishes host port == container port.
    base_url = f"http://{browser_host(host)}:{port}/"
    typer.echo("", err=True)
    typer.echo("TrapTracker Automatic Reporting Extension is running.", err=True)
    typer.echo("", err=True)
    typer.echo("Open in your browser:", err=True)
    if fixed:
        # Not printed: the holder already has it, and a fixed token written to a
        # log (`docker compose logs` keeps them) would outlive every restart.
        typer.echo(f"{base_url}?token=<{UI_TOKEN_ENV}>", err=True)
        typer.echo("", err=True)
        typer.echo(f"Put the value of {UI_TOKEN_ENV} in place of <{UI_TOKEN_ENV}>. "
                   f"It stays the same across restarts.", err=True)
    else:
        typer.echo(f"{base_url}?token={token}", err=True)
        typer.echo("", err=True)
        typer.echo("The token is exchanged for a session cookie and removed from the "
                   "address bar.\n"
                   f"A new one is minted each start; set {UI_TOKEN_ENV} to keep one.",
                   err=True)
    typer.echo("", err=True)
    uvicorn.run(web_app, host=host, port=port)


@app.command(name="recompute-crops")
def recompute_crops(
    tctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Project to operate on. Same as the global --project; give one "
             "form or the other, not both."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would change, write nothing."),
    limit: int = typer.Option(0, "--limit", help="Process only the first N events (smoke runs)."),
    classify: bool = typer.Option(
        True, "--classify/--no-classify",
        help="Re-run BioCLIP on the crop into the parallel bioclip_crop_* columns "
             "(needs the [enrich] extra). Without it, only the crop geometry and "
             "its provenance are stored."),
) -> None:
    """Produce a better crop for BioCLIP, and record how it was chosen.

    An independent detector (RT-DETR, Apache-2.0) proposes candidate boxes on the
    CLEAN frame; TrapTracker's own rendered box is recovered and its banner read to
    say which candidate is this event's animal; the two are matched spatially at
    tau. The crop is ALWAYS the detector's box or there is no crop - TrapTracker's
    rectangle never becomes crop geometry, which is what keeps BioCLIP independent
    of TrapTracker's localisation (Decision 1).

    Results land in PARALLEL columns. The existing bioclip_* full-frame read and
    every cross-check verdict are left exactly as they are: whether corroboration
    should use the cropped read is a Phase 3 decision, not a side effect of this.
    """
    from collections import Counter

    from .config import get_settings
    from .crop.factory import build_detector
    from .crop.runner import fit_templates, process_event

    # A bulk write: this rewrote every one of the corpus's 819 events.
    resolution = _resolve(tctx, project, command="recompute-crops",
                          require_explicit=not dry_run)
    ctx = resolution.ctx
    # Machine-level, and staying there: detector_model/device and the Phase 2a
    # constants crop_iou_tau / crop_pad_frac. Per-project copies would let two
    # projects disagree about a number the published findings rest on.
    settings = get_settings()
    repo = ctx.open_repo()
    try:
        rows = repo.iter_for_crop_recompute()
        if limit:
            rows = rows[:limit]
        typer.echo(f"{len(rows)} events", err=True)
        if not rows:
            typer.echo("nothing to do", err=True)
            return

        typer.echo("fitting banner templates from single-detection frames ...", err=True)
        resolver = fit_templates(
            rows, progress=lambda n, t: typer.echo(f"  {n}/{t}", err=True))
        typer.echo(f"  {len(resolver.labels.tpl)} label tokens, "
                   f"{len(resolver.confs.tpl)} confidence values", err=True)

        detector = build_detector(settings)
        taxonomic = None
        if classify:
            from .enrichment.factory import build_taxonomic_enricher
            taxonomic = build_taxonomic_enricher(settings)

        statuses: Counter = Counter()
        bases: Counter = Counter()
        localisation: Counter = Counter()
        mismatches: list[int] = []

        for n, (event_id, label, conf, clean, boxed) in enumerate(rows):
            out = process_event(event_id, label, conf, clean, boxed,
                                resolver=resolver, detector=detector,
                                taxonomic=taxonomic,
                                tau=settings.crop_iou_tau,
                                pad_frac=settings.crop_pad_frac)
            statuses[out.decision.status] += 1
            bases[out.decision.basis] += 1
            localisation[out.decision.localisation_agreement] += 1
            if out.tt.label_mismatch:
                mismatches.append(event_id)
            if not dry_run:
                repo.set_crop(event_id, out.decision, out.tt, out.detector, out.taxo)
            if n % 25 == 0:
                typer.echo(f"  {n}/{len(rows)}  {out.decision.status}", err=True)

        typer.echo("")
        typer.echo("crop_status:")
        for k, v in statuses.most_common():
            typer.echo(f"  {k:24s} {v:5d}  ({v / len(rows) * 100:5.1f}%)")
        typer.echo("crop_basis:")
        for k, v in bases.most_common():
            typer.echo(f"  {k:30s} {v:5d}")
        typer.echo("localisation_agreement:")
        for k, v in localisation.most_common():
            typer.echo(f"  {k:16s} {v:5d}")
        cropped = sum(v for k, v in statuses.items() if k.startswith("cropped_"))
        typer.echo("")
        typer.echo(f"cropped {cropped}/{len(rows)} = {cropped / len(rows) * 100:.1f}%")
        if mismatches:
            typer.echo(f"banner label mismatched the database on {len(mismatches)} "
                       f"events (a recovered-banner gap, not an error): "
                       f"{mismatches[:12]}")
        # Conservation: every event must land in exactly one status.
        if sum(statuses.values()) != len(rows):
            typer.echo("error: events were dropped by the crop path", err=True)
            raise typer.Exit(code=1)
        typer.echo("dry run - nothing written" if dry_run
                   else _done(resolution), err=True)
    finally:
        repo.close()



@app.command(name="recompute-crop-crosscheck")
def recompute_crop_crosscheck(
    tctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", metavar="NAME|ID",
        help="Project to operate on. Same as the global --project; give one "
             "form or the other, not both."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would change, write nothing."),
) -> None:
    """Decide the cross-check on the CROPPED read, into a parallel audit trail.

    This does NOT restate the published verdict. ``agreement_flag`` and its audit
    columns keep the full-frame decision that `recompute-crosscheck` produced; the
    cropped read lands in the six ``*_crop`` columns beside it. Phase 3 Option B
    reports the two side by side as a framing-sensitivity finding rather than
    adopting the higher number, because the change is not one-way: 17 events that
    agree on the full frame disagree on the crop.

    Also backfills the cropped top-1's lineage (``bioclip_crop_top1_*``), which the
    crop path left NULL. That is the same TolLineageIndex lookup the taxonomic
    distance needs, so both are filled in one pass rather than two.

    Prints the four-cell crosstab of published vs cropped verdict, and FAILS LOUDLY
    if any published flag moved - nothing in this command may write to those columns.
    """
    from collections import Counter

    from .config import get_settings
    from .enrichment.agreement import compute_crosscheck
    from .enrichment.base import TaxonomicRead
    from .enrichment.bioclip_taxonomy import TolLineageIndex

    resolution = _resolve(tctx, project, command="recompute-crop-crosscheck",
                          require_explicit=not dry_run)
    ctx = resolution.ctx
    settings = get_settings()          # machine-level: BioCLIP checkpoint only
    alias_map = ctx.load_alias_map()
    taxonomy = ctx.load_target_taxonomy()

    try:
        typer.echo("loading BioCLIP's label table for the cropped lineage ...", err=True)
        index = TolLineageIndex.load(settings.bioclip_model)
        typer.echo(f"  {len(index)} binomials", err=True)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2)

    repo = ctx.open_repo()
    try:
        rows = repo.iter_for_crop_crosscheck()
        if not rows:
            typer.echo("nothing to do", err=True)
            return

        flags: Counter = Counter()
        bases: Counter = Counter()
        distances: Counter = Counter()
        crosstab: Counter = Counter()
        by_crop_status: Counter = Counter()
        no_crop_read = 0

        for event_id, label, ok, error, topk, published, crop_status in rows:
            topk = [tuple(entry) for entry in topk]
            if not topk:
                no_crop_read += 1
            lineage = index.lineage_for(topk[0][0]) if topk else None
            read = TaxonomicRead(
                provider="bioclip", model_name=settings.bioclip_model, topk=topk,
                topk_lineages=[lineage.as_mapping()] if lineage else [],
                ok=ok, error=error,
            )
            check = compute_crosscheck(label, read, alias_map, taxonomy,
                                       use_topk=settings.agreement_topk_join)
            flags[check.flag] += 1
            bases[check.resolution_basis] += 1
            distances[check.taxonomic_distance] += 1
            crosstab[(published, check.flag)] += 1
            by_crop_status[(crop_status, published, check.flag)] += 1
            if not dry_run:
                repo.set_crop_crosscheck(event_id, check, lineage)

        total = len(rows)
        typer.echo("")
        typer.echo(f"detections examined: {total}")
        typer.echo(f"  rows with no stored cropped read: {no_crop_read}")
        typer.echo("")
        typer.echo("cropped-read verdict:")
        for flag in ("agree", "disagree", "not_evaluable"):
            typer.echo(f"  {flag:<16} {flags.get(flag, 0)}")
        typer.echo("")
        typer.echo("published (full-frame) x cropped verdict:")
        for pub in ("agree", "disagree", "not_evaluable"):
            for cro in ("agree", "disagree", "not_evaluable"):
                n = crosstab.get((pub, cro), 0)
                if n:
                    mark = "  <- reversal" if (pub, cro) == ("agree", "disagree") else ""
                    typer.echo(f"  full={pub:<14} crop={cro:<14} {n:5d}{mark}")
        typer.echo("")
        typer.echo("cropped-read taxonomic distance:")
        for d, n in distances.most_common():
            typer.echo(f"  {str(d):<24} {n:5d}")
        typer.echo("")
        typer.echo("cropped-read resolution basis:")
        for b, n in bases.most_common():
            typer.echo(f"  {str(b):<28} {n:5d}")

        # The published verdict must be exactly as it was. This command has no
        # statement that writes those columns, and this proves it row by row.
        after = {r[0]: r[5] for r in repo.iter_for_crop_crosscheck()}
        moved = [eid for eid, _l, _o, _e, _t, pub, _cs in rows if after.get(eid) != pub]
        typer.echo("")
        if moved:
            typer.echo(f"error: the published agreement_flag moved on {len(moved)} rows "
                       f"- this command must never write it: {moved[:10]}", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"published agreement_flag unchanged on all {total} rows")
        typer.echo("dry run - nothing written" if dry_run
                   else _done(resolution), err=True)
    finally:
        repo.close()


if __name__ == "__main__":
    app()
