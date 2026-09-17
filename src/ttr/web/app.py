"""FastAPI demo UI — one page, three endpoints.

`/api/report` renders the report generator's markdown to HTML and **sanitises it
server-side** before returning it, because the report embeds VLM descriptions —
untrusted, measured-confabulatory model output (CLAUDE.md §6). The same "VLM
content is data, never instructions" principle applies at the render boundary.

The frontend holds no report logic: it submits (species, start, end), and injects
the sanitised HTML the agent produced. Did-you-mean / empty / unrecognised all
arrive as the agent's own markdown — the agents are used unchanged.
"""

from __future__ import annotations

import html as _html
import os
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from datetime import date
from pathlib import Path
from typing import Literal

import markdown as _markdown
import nh3
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel

# The narrative retry budget is imported rather than restated: it is half of what a
# report can cost in wall-clock time, and the PDF wait must be sized from the SAME
# number or the two drift apart (see `report_pdf`).
from ..agents.report import _NARRATIVE_MAX_ATTEMPTS, ReportGeneratorAgent
# The standing statements, IMPORTED. Their whole value is that they are
# byte-identical wherever they appear — a report, a PDF, and now the page that
# says what a report will state — so the page reads the report stage's own
# constants and supplies only grammatical glue. Retyping them is how a claim
# with an audit trail quietly becomes two claims. See `_waiting_html`.
from ..agents.report import (COUNT_CAVEAT as _COUNT_CAVEAT,
                             _DECISION7_CLAIM, _SEND_TIME_CLAIM)
from ..agents.retrieval import RetrievalAgent
from ..agents.window import TimeWindow
from ..config import get_settings
from ..llm.ollama_client import OllamaLLMClient
from ..logging import get_logger
from ..projects.context import ProjectContext
from ..projects.errors import ProjectError
from ..projects.registry import Registry
from ..projects.service import project_dir_for
from ..species.aliases import SpeciesAliasMap
from .auth import (PRINT_TICKET_PARAM, discard_print_ticket, mint_print_ticket,
                   session_token_middleware)
from .ingest import RUNNER, IngestBusy, friendly_error
from .ingest_page import INGEST_PAGE
from .picker import PROJECT_SLOT as _PROJECT_SLOT
from .picker import banner as project_banner
from .picker import render as render_picker
from . import theme as _theme

app = FastAPI(title="TrapTracker Report — demo")
# EVERY route is behind this, including the project picker and the ingest
# endpoints that write. It fails closed: with no token configured the app
# refuses rather than serving openly, so handing this object to a bare ASGI
# server is not a way to get the old unauthenticated behaviour back.
app.middleware("http")(session_token_middleware)
logger = get_logger(__name__)


@app.exception_handler(StarletteHTTPException)
async def _error_response(request: Request, exc: StarletteHTTPException):
    """JSON for API clients, a readable page for a browser.

    Negotiated on Accept, and the test for HTML is explicit rather than a default:
    anything that did not ASK for HTML — every API client, every test client, curl
    — keeps the JSON body it has always had. Only a browser navigation, which
    sends `text/html` in Accept, gets the page.

    The detail text is the same either way. It is server-authored (a project id
    echoed from the URL is the only variable part) and is escaped before it
    reaches the page regardless, because "it cannot contain markup" is exactly the
    assumption that stops being true later.
    """
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    heading = {404: "Not found", 403: "Not allowed", 500: "Something broke"}.get(
        exc.status_code, "That did not work")
    return HTMLResponse(
        _theme.error_page(exc.status_code, heading, _html.escape(detail)),
        status_code=exc.status_code)

#: Reserved dropdown value routing to the all-species summary. Not a species term,
#: so it can never collide with an alias-table entry.
ALL_SPECIES = "*all*"

#: Reserved dropdown value routing to the BNG-aligned monitoring report (a third
#: report type). Same sentinel discipline as ALL_SPECIES — not a species term.
BNG_ALIGNED = "*bng*"

# TWO POLICIES, and the split is the whole security model.
# Full design record: docs/render-safety.md.
#
# HISTORY, because the comment that stood here was WRONG and a reader should know
# that this one is checked rather than asserted. It claimed an SVG-shaped VLM
# string "is stripped of every attribute below and rendered inert". That was false
# for the very path it described - fill and width survived on <rect> - and it was
# disproved by the project's own negative testing on 2026-08-14, then carried as a
# strict xfail for three weeks (Results/coverage_map.md §7.3). The claims below are
# each pinned by a test, named where it matters.
#
# The DOCUMENT policy guards everything that reaches the page through the report
# markdown - which includes model-authored text: the narrative LLM's prose and the
# VLM's image descriptions, the latter email-derived and untrusted under CLAUDE.md
# §6. It permits NO SVG at all. Chart-shaped markup from either model is therefore
# not sanitised into harmlessness, it is simply not renderable: there is no tag for
# it to become.
#
# The CHART policy guards the agent's own computed charts, which do not travel in
# the document. They are held aside during rendering and injected after the
# document has been sanitised (`RenderedReport.substitute_into`), so they are never
# subject to the policy above. Reaching this policy requires holding a per-render
# nonce, which model output cannot know.
#
# Chart policy = document policy + SVG. Expressed that way rather than as two
# lists, so the shared half cannot drift apart, and so "what the charts may do that
# the document may not" is exactly one readable set.
_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "strong", "em",
    "code", "pre", "blockquote", "hr", "br", "table", "thead", "tbody", "tr", "th", "td", "del",
    # Structural wrappers for headline cards and figure captions (item 7/10) and
    # data-URI thumbnails (item 11). All agent-generated; none permits script,
    # href, handlers, or a non-data URL.
    "div", "span", "figure", "figcaption", "img",
}
_ALLOWED_ATTRS = {
    # Cards / figures / thumbnails. 'class' only (cosmetic); img carries no handlers
    # and its src is restricted to the data: scheme by _URL_SCHEMES below.
    "div": {"class"}, "span": {"class"}, "figure": {"class"}, "figcaption": {"class"},
    "img": {"src", "alt", "width", "height", "class"},
}

#: The inline-SVG vocabulary the chart generators use. Kept in exact case: nh3
#: matches case-sensitively and the HTML5 parser preserves the SVG camelCase
#: names. No script, href/xlink, foreignObject or event handler appears here.
_SVG_TAGS = {"svg", "rect", "line", "text", "title", "g", "path", "circle"}
_SVG_ATTRS = {
    "svg": {"viewBox", "width", "height", "role", "aria-label", "preserveAspectRatio"},
    "rect": {"x", "y", "width", "height", "rx", "ry", "fill",
             "stroke", "stroke-width", "stroke-dasharray"},
    "line": {"x1", "y1", "x2", "y2", "stroke", "stroke-width", "stroke-dasharray"},
    "text": {"x", "y", "fill", "font-size", "text-anchor", "font-weight"},
    "g": {"fill", "transform"},
    # Pie chart (weather correlation). Geometry + fill/stroke only.
    "path": {"d", "fill", "stroke", "stroke-width"},
    "circle": {"cx", "cy", "r", "fill", "stroke", "stroke-width"},
}

#: The PRIVILEGED policy. It is the only one permitting SVG, and its safety rests
#: entirely on never being applied to anything but held-aside generator output -
#: an invariant enforced by `test_chart_policy_is_privileged.py`, not by memory.
#: That guard asserts these names are read in exactly one function, reached only
#: through `substitute_into(normalise=...)`, and never imported out of this module.
#: See docs/render-safety.md §1 for why that is the thing worth guarding.
_CHART_TAGS = _ALLOWED_TAGS | _SVG_TAGS
_CHART_ATTRS = {**_ALLOWED_ATTRS, **_SVG_ATTRS}


#: URL schemes permitted on any surviving attribute (there are no links; only data-URI
#: thumbnails). Restricting to 'data' means an external/tracking image URL is stripped.
_URL_SCHEMES = {"data"}


# --- dependencies (overridable in tests) -----------------------------------
#: Every project-scoped route lives under this prefix. Project identity is part
#: of the URL rather than server state, deliberately:
#:
#:   * a process-global "current project" recreates the `_BODY_CACHE` bug class
#:     server-wide — one request's switch changes what every other request sees;
#:   * a session variable makes a bookmarked or shared report link silently mean
#:     something different later;
#:   * in the path, the project is part of the IDENTITY of every request, so it
#:     reaches the cache key, the server-built print URL and every log line
#:     without anyone having to remember to add it.
PROJECT_PREFIX = "/p/{project_id}"


def get_project(project_id: str) -> ProjectContext:
    """The project this request is about, from the URL.

    An unknown OR ARCHIVED id is a 404, never a fallback to some other project.
    Archiving is a statement that a project should not be reachable by ordinary
    selection, and quietly serving it anyway would make that meaningless.
    """
    try:
        registry = Registry.load()
        entry = registry.get(project_id)
    except ProjectError:
        raise HTTPException(status_code=404,
                            detail=f"no such project: {project_id}")
    if entry.archived:
        raise HTTPException(
            status_code=404,
            detail=f"project {entry.name!r} is archived; unarchive it to use it")
    return ProjectContext.load(project_dir_for(entry))


def get_repo(ctx: ProjectContext = Depends(get_project)):
    """This project's repository, opened per request in the request thread."""
    repo = ctx.open_repo()
    try:
        yield repo
    finally:
        repo.close()


def get_alias_map(ctx: ProjectContext = Depends(get_project)) -> SpeciesAliasMap:
    return ctx.load_alias_map()


def get_llm():
    """The report/VLM client.

    NOT project-derived, and deliberately so: the Ollama endpoint, model and
    timeout describe the MACHINE, not the deployment. Making this depend on the
    project would imply a per-project LLM configuration that does not exist and
    should not — two projects disagreeing about which model wrote their narrative
    would make the reports incomparable.
    """
    s = get_settings()
    return OllamaLLMClient(model=s.ollama_model, endpoint=s.ollama_endpoint,
                           timeout_seconds=s.ollama_timeout_seconds)


#: Project-scoped routes. Mounted on the app at the end of this module.
project_routes = APIRouter(prefix=PROJECT_PREFIX)


class ReportRequest(BaseModel):
    species: str
    start: str                          # YYYY-MM-DD
    end: str                            # YYYY-MM-DD
    include_crosscheck: bool | None = None   # None -> use the configured default


class CreateProjectRequest(BaseModel):
    """The creation form's payload.

    A BODY model, never query parameters, and that is a security property rather
    than a style choice: uvicorn's access log records the full request line
    including the query string and does NOT record bodies — audited by probe, see
    `docs/multi-project-phase1b-gap-closure.md` §6. A password in a query string
    would be written to the log in clear.
    """

    name: str
    email: str
    password: str | None = None
    site_name: str | None = None
    latitude: str | None = None
    longitude: str | None = None


@app.post("/api/projects")
def create_project_route(req: CreateProjectRequest):
    """Create a project from the picker page.

    Excluded from v1 because this server had no authentication and a mailbox app
    password must not be POSTed to an endpoint any local process can reach. That
    exclusion was conditional; `ttr serve` is now behind a session token and every
    route — this one included — refuses without it.

    NOTHING IS REIMPLEMENTED HERE. It calls the same `create_project` the CLI
    calls, so the validate-then-verify-then-write ordering, the Gmail-only
    refusal, and the guarantee that a failure leaves no directory, no registry
    entry and no credential are the same code and cannot drift from it. This
    route only translates a form into that call and its errors into JSON.
    """
    from .. import projects as _projects            # noqa: F401  (package import)
    from ..projects.errors import ProjectError
    from ..projects.service import create_project, set_site

    password = (req.password or "").strip() or None
    try:
        latitude = float(req.latitude) if (req.latitude or "").strip() else None
        longitude = float(req.longitude) if (req.longitude or "").strip() else None
    except ValueError:
        return JSONResponse(
            {"error": "Latitude and longitude must be numbers, or both left empty."},
            status_code=400)
    if (latitude is None) != (longitude is None):
        return JSONResponse(
            {"error": "Give both latitude and longitude, or neither — a half-set "
                      "coordinate pair is not a location."}, status_code=400)

    try:
        manifest, _dir = create_project(
            req.name, req.email, password,
            skip_connection_test=password is None)
    except ProjectError as exc:
        # The service's own message, which already explains Microsoft accounts,
        # unknown providers and rejected credentials in the terms a user needs.
        return JSONResponse({"error": str(exc)}, status_code=400)

    note = ""
    if password is None:
        from ..projects.credentials import supply_step

        note = (" The mailbox was NOT verified and no credential is stored. Next, "
                f"{supply_step(manifest.id)}, when you are online.")
    if req.site_name or latitude is not None:
        try:
            set_site(manifest.id, name=(req.site_name or None),
                     latitude=latitude, longitude=longitude)
        except ProjectError as exc:
            note += f" The project was created, but the site was not set: {exc}"

    return JSONResponse({
        "id": manifest.id,
        "name": manifest.name,
        "message": f"Created {manifest.name!r}." + note,
    })


#: Replaced at render time with the window payload below.
_WINDOW_SLOT = "<!--WINDOW-->"


def _window_payload(db_path) -> str:
    """``[[day, canonical_binomial, count], …]`` for the whole corpus, as JSON.

    Feeds the controls card: the default window (the project's real first and
    last dated) and the summary strip, which sums a slice of this for whatever
    window is chosen. ONE query at page load rather than an endpoint per window
    change — the strip is then exact for any window, never stale, and there is no
    new route or parameter.

    Three deliberate properties:

    * **Its own read-only connection.** `get_repo` opens the project read-write
      because generating a report may write; this read must not be able to, and a
      separate `mode=ro` handle says so in a way a comment cannot.
    * **Grouped on the same COALESCE key** the report buckets days by, so the
      preview and the report cannot disagree about which day a row falls on.
    * **Days with no rows are absent, not zero** — the same rule the projects page
      chart follows. The strip counts days that HAVE rows; it never invents one.

    Returns ``"null"`` when the corpus cannot be read, and the page then hides the
    strip rather than rendering zeros: an unreadable database is not an empty one.
    """
    import json
    import sqlite3

    dated = "date(COALESCE(capture_time_utc, event_time_utc))"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT {dated} AS d, canonical_binomial, COUNT(*) "
                f"FROM detection_events WHERE {dated} IS NOT NULL "
                f"GROUP BY d, canonical_binomial ORDER BY d").fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return "null"
    payload = [[d, b, n] for d, b, n in rows
               if isinstance(d, str) and len(d) == 10]
    # Embedded in a <script type="application/json"> block, so the only sequence
    # that could break out of it is a literal "</". There is none in ISO dates or
    # binomials, but the corpus is external data and this is cheaper than trusting.
    return json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")


@app.get("/", response_class=HTMLResponse)
def project_index() -> str:
    """The project picker: the server's entry point.

    Markup and style live in `picker.py`; this route only loads the registry and
    reports a registry that will not load. It now carries a CREATION form — see
    `create_project_route` — which was excluded while the server had no
    authentication and became defensible once it did. Editing an existing project
    is still absent on purpose: site, credential and archive stay on the CLI.
    """
    try:
        registry = Registry.load()
    except ProjectError as exc:
        return ("<!doctype html><meta charset='utf-8'>"
                "<title>TrapTracker Reporting Extension</title>"
                "<h1>TrapTracker Reporting Extension</h1><p>Could not read the project "
                f"registry: {_html.escape(str(exc))}</p>")
    return render_picker(registry)


@project_routes.get("/", response_class=HTMLResponse)
def index(ctx: ProjectContext = Depends(get_project)) -> str:
    return (_PAGE.replace(_PROJECT_SLOT, project_banner(ctx.name, ctx.id))
                 .replace(_WINDOW_SLOT, _window_payload(ctx.db_path)))


@project_routes.get("/api/species")
def species(repo=Depends(get_repo), alias_map=Depends(get_alias_map)) -> dict:
    """Full class list from the alias table, annotated with the count actually in
    the dataset — so the UI tells the '31 classes, N present' story."""
    counts = repo.species_counts()
    return {
        "species": [
            {"display": e.display_name, "query": e.query_term, "canonical": e.canonical_key,
             "is_binomial": e.is_binomial, "count": counts.get(e.canonical_key, 0)}
            for e in alias_map.entries()
        ],
        # So the toggle initialises to the configured (env) default.
        "include_crosscheck_default": get_settings().report_include_crosscheck,
        "all_species_value": ALL_SPECIES,
        "bng_value": BNG_ALIGNED,
    }


#: Bodies already rendered for the on-screen view, so `/print` can print EXACTLY the
#: report the user is looking at. Two of the three report types put an LLM in the
#: generation path, so regenerating for the PDF would cost a second model call and
#: could return a differently-worded narrative than the one on screen. Bounded and
#: short-lived; a miss simply regenerates, so this is an optimisation and a fidelity
#: guarantee, never a correctness dependency.
_BODY_CACHE: "OrderedDict[tuple, tuple[float, str]]" = OrderedDict()
_BODY_CACHE_MAX = 4
_BODY_CACHE_TTL_SECONDS = 900.0
_BODY_CACHE_LOCK = threading.Lock()   # sync endpoints run in a threadpool


def _cache_get(key: tuple) -> str | None:
    with _BODY_CACHE_LOCK:
        hit = _BODY_CACHE.get(key)
        if hit is None:
            return None
        stored_at, body = hit
        if time.monotonic() - stored_at > _BODY_CACHE_TTL_SECONDS:
            _BODY_CACHE.pop(key, None)
            return None
        _BODY_CACHE.move_to_end(key)
        return body


def _cache_put(key: tuple, body: str) -> None:
    with _BODY_CACHE_LOCK:
        _BODY_CACHE[key] = (time.monotonic(), body)
        _BODY_CACHE.move_to_end(key)
        while len(_BODY_CACHE) > _BODY_CACHE_MAX:
            _BODY_CACHE.popitem(last=False)


def _render_report_body(project_id: str, species: str, start: str, end: str,
                        include_crosscheck: bool | None,
                        repo, alias_map, llm, *, site_name: str | None = None,
                        reuse_rendered: bool = False) -> str:
    """Produce the sanitised report HTML fragment (the same body used on screen and
    as the print source). Raises ValueError on a bad date range.

    With ``reuse_rendered`` (the print path) an identical body rendered moments ago
    for the screen is returned unchanged instead of being regenerated. The date range
    is validated first either way, so a bad window still fails on the print route.
    """
    window = TimeWindow.from_dates(date.fromisoformat(start), date.fromisoformat(end))
    settings = get_settings()
    resolved = (include_crosscheck if include_crosscheck is not None
                else settings.report_include_crosscheck)
    # Keyed on the PROJECT first, then the RESOLVED toggle (so the screen's
    # explicit False and the print route's absent parameter are one entry, not
    # two).
    #
    # The project id is what closes the cross-project leak. Without it two
    # projects rendering the same species and window collide on one key, and the
    # print path — the only reader, via reuse_rendered — serves whichever body
    # was written last. That produces a downloadable PDF of the wrong site, with
    # no query issued, nothing raised and nothing logged.
    #
    # Note the fix is the KEY, not clearing the cache on switch: a project-blind
    # key that gets cleared is still a project-blind key, and under concurrent
    # requests it leaks anyway.
    cache_key = (project_id, species, start, end, resolved)
    if reuse_rendered:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    agent = ReportGeneratorAgent(
        RetrievalAgent(repo, alias_map), llm,
        low_confidence_threshold=settings.confidence_low_flag_threshold,
        include_crosscheck=resolved,
        # The deployment's own name, from its manifest.
        site_name=site_name,
    )
    # The charts come back HELD ASIDE, not inline: `rendered.markdown` carries an
    # opaque per-render token where each chart belongs.
    if species == ALL_SPECIES:
        rendered = agent.render_all_species(window)
    elif species == BNG_ALIGNED:
        rendered = agent.render_bng_aligned(window)             # third report type
    else:
        rendered = agent.render(species, window)                # the agent, unchanged
    html = _markdown.markdown(rendered.markdown, extensions=["tables"])
    safe = nh3.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS,
                     url_schemes=_URL_SCHEMES)           # sanitise untrusted VLM text
    # ORDER IS THE POINT, and it is the wrong way round in the obvious version.
    # The agent's charts go in AFTER sanitising, so they are never subject to the
    # allowlist at all. Substituting first would leave them exactly as exposed as
    # model text — and the moment the allowlist stops permitting svg/rect/text,
    # which is the next stage, the real charts would vanish with the fake ones.
    # Model output cannot reach this route: it would have to guess the nonce.
    safe = rendered.substitute_into(
        safe,
        normalise=lambda markup: nh3.clean(markup, tags=_CHART_TAGS,
                                           attributes=_CHART_ATTRS,
                                           url_schemes=_URL_SCHEMES))
    # Cached SUBSTITUTED, so the print path — the only reader of this cache —
    # cannot serve a PDF with a raw token where a chart should be.
    _cache_put(cache_key, safe)
    return safe


@project_routes.post("/api/report", response_class=HTMLResponse)
def report(req: ReportRequest, ctx: ProjectContext = Depends(get_project),
           repo=Depends(get_repo),
           alias_map=Depends(get_alias_map), llm=Depends(get_llm)) -> HTMLResponse:
    try:
        safe = _render_report_body(ctx.id, req.species, req.start, req.end,
                                   req.include_crosscheck, repo, alias_map, llm,
                                   site_name=ctx.site.name)
    except ValueError as exc:
        return HTMLResponse(f"<p class='err'>Invalid date range: {nh3.clean(str(exc))}</p>",
                            status_code=400)
    return HTMLResponse(safe)


@project_routes.get("/print", response_class=HTMLResponse)
def print_page(species: str, start: str, end: str, crosscheck: bool | None = None,
               ctx: ProjectContext = Depends(get_project),
               repo=Depends(get_repo), alias_map=Depends(get_alias_map),
               llm=Depends(get_llm)) -> HTMLResponse:
    """A standalone, self-contained HTML document of the report — the source the
    headless-Chrome PDF flow navigates to (NOT window.print). Same sanitised body as
    /api/report — literally the same body when the screen has just rendered it — wrapped
    with the print stylesheet."""
    try:
        body = _render_report_body(ctx.id, species, start, end, crosscheck,
                                   repo, alias_map, llm, site_name=ctx.site.name,
                                   reuse_rendered=True)
    except ValueError as exc:
        return HTMLResponse(f"<p class='err'>Invalid date range: {nh3.clean(str(exc))}</p>",
                            status_code=400)
    doc = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<title>Monitoring report</title><style>{_PRINT_STYLE}</style></head>"
           f"<body><main id='report'>{body}</main></body></html>")
    return HTMLResponse(doc)


def _pdf_footer(species: str, start: str, end: str) -> str:
    """Chrome footer template (isolated context — inline styles only): abbreviated
    title · monitoring period · Page X of Y. Values are HTML-escaped."""
    title = "Automated Faunal Activity Monitoring Report"
    if species == BNG_ALIGNED:
        title = "Faunal Activity Monitoring — BNG-aligned"
    period = _html.escape(f"{start} to {end}")
    left = f"{_html.escape(title)} &middot; {period}"
    return (
        "<div style=\"width:100%; font-family:'Segoe UI',sans-serif; font-size:8px; "
        "color:#444; padding:0 12mm; box-sizing:border-box; display:flex; "
        "justify-content:space-between; align-items:center;\">"
        f"<span>{left}</span>"
        "<span>Page <span class=\"pageNumber\"></span> of "
        "<span class=\"totalPages\"></span></span></div>"
    )


@project_routes.get("/api/report.pdf")
def report_pdf(request: Request, species: str, start: str, end: str,
               crosscheck: bool | None = None,
               ctx: ProjectContext = Depends(get_project)) -> Response:
    """Render the report to PDF via headless Chrome/Edge (Page.printToPDF). The
    browser navigates to this server's own /print page and prints it with a controlled
    footer; the browser's own header/date/URL are disabled."""
    from . import pdf as pdf_module
    from .browser import BrowserNotFound
    from .pdf import PdfRenderError, render_url_to_pdf

    # Pin the render target to THIS local server (never the client-supplied Host
    # header), so the headless browser can only ever navigate to our own /print page
    # — no Host-header-driven arbitrary navigation. Only the report params are passed.
    # The PROJECT is part of the print URL, so the headless browser fetches this
    # project's report and no other. It reaches the URL because it is in the
    # path, not because someone remembered to append it.
    base = f"http://127.0.0.1:{request.url.port or 8000}"
    # The headless browser has no session cookie, so it carries a single-use
    # ticket for exactly this project's /print instead (`auth.py`, "THE PDF
    # BROWSER GETS A PRINT TICKET"). Without it the browser was refused and the
    # refusal page came back as the PDF.
    print_ticket = mint_print_ticket(f"/p/{ctx.id}/print")
    query = urllib.parse.urlencode(
        {"species": species, "start": start, "end": end,
         **({"crosscheck": str(crosscheck).lower()} if crosscheck is not None else {}),
         PRINT_TICKET_PARAM: print_ticket})
    print_url = f"{base}/p/{urllib.parse.quote(ctx.id)}/print?{query}"
    # The browser's wait must cover the /print page's OWN generation time, not just its
    # load. A cache hit answers instantly, but a cold print route re-runs the report
    # generator — and for the two LLM-backed types that is an Ollama call with bounded
    # retries. Size the wait from that worst case (plus load/paint headroom) instead of
    # a fixed 30s a slow narrative would overrun.
    settings = get_settings()
    render_timeout = settings.ollama_timeout_seconds * _NARRATIVE_MAX_ATTEMPTS + 30.0
    # One line per download, at INFO. It carries the process id and the renderer's
    # own file path deliberately: a server left running across a code change keeps
    # the OLD modules in memory (uvicorn does not auto-reload here), and that is
    # otherwise invisible from the outside — a blank PDF looked identical to a bug.
    kind = {ALL_SPECIES: "all-species", BNG_ALIGNED: "bng-aligned"}.get(species, "single-species")
    context = {"report_type": kind, "project": ctx.id, "species": species,
               "start": start, "end": end,
               "pid": os.getpid(), "renderer": pdf_module.__file__}
    started = time.monotonic()
    try:
        override = get_settings().browser_executable
        pdf_bytes = render_url_to_pdf(print_url, footer_html=_pdf_footer(species, start, end),
                                      browser_override=str(override) if override else None,
                                      render_timeout=render_timeout)
    except BrowserNotFound as exc:
        logger.warning("report_pdf_no_browser", extra={**context, "error": str(exc)})
        return PlainTextResponse(str(exc), status_code=503)
    except PdfRenderError as exc:
        logger.error("report_pdf_failed", extra={**context, "error": str(exc)})
        return PlainTextResponse(f"PDF generation failed: {exc}", status_code=500)
    finally:
        discard_print_ticket(print_ticket)
    logger.info("report_pdf_rendered",
                extra={**context, "bytes": len(pdf_bytes),
                       "elapsed_s": round(time.monotonic() - started, 2)})
    # Safe download filename: only [0-9A-Za-z_] from the dates, so nothing from the
    # query string can break out of the Content-Disposition header.
    stamp = re.sub(r"[^0-9A-Za-z]", "", f"{start}_{end}") or "report"
    fname = f"faunal-monitoring-report_{stamp}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             # Every download is generated on the spot from live data;
                             # a cached copy would silently misrepresent the window.
                             "Cache-Control": "no-store, max-age=0"})


# ---------------------------------------------------------------- ingest (live run)
def _server_credential_scope() -> int:
    """How many projects an unscoped ``TTR_IMAP_PASSWORD`` could be meant for here.

    Every non-archived project. `ttr serve` is ONE process reaching all of them,
    the variable was set for that process, and the project for an ingest arrives
    later in the URL. Nothing ties that password to one mailbox, so with more
    than one project `credentials.resolve` refuses it and the per-project
    variable is the way in.
    """
    return max(1, len(Registry.load().visible()))


def get_pipeline_factory(ctx: ProjectContext = Depends(get_project)):
    """Returns a callable that BUILDS this project's pipeline *in the job thread*.

    A factory, not a pipeline: sqlite connections are thread-bound
    (`migrations.connect` leaves check_same_thread=True), so nothing the
    background job uses may be constructed on this request thread. `ctx.open_repo()`
    is reached inside `build_pipeline_for`, which the factory calls from the job
    thread — never hoisted out here.

    Resolving the config inside the factory also means a credential or config
    failure becomes a readable console line and a `failed` state rather than a
    500 on the button. Tests override this to run a whole job with no inbox and
    no models.
    """
    scope = _server_credential_scope()

    def _factory(on_event):
        from ..pipeline import build_pipeline_for

        return build_pipeline_for(ctx, app_settings=get_settings(),
                                  on_event=on_event, projects_in_scope=scope)
    return _factory


class IngestStartRequest(BaseModel):
    """The ingest button's payload.

    It carries NO project field, and must not grow one: the project arrives as a
    path parameter, and a body field would be a second — forgeable — source for
    the same fact. `test_web_projects` asserts this on the model, so the rule is
    enforced rather than remembered.
    """

    mode: Literal["once", "continuous"] = "once"
    level: Literal["info", "debug"] = "info"


@project_routes.get("/ingest", response_class=HTMLResponse)
def ingest_page(ctx: ProjectContext = Depends(get_project)) -> str:
    return INGEST_PAGE.replace(_PROJECT_SLOT, project_banner(ctx.name, ctx.id))


@project_routes.get("/api/ingest/preflight")
def ingest_preflight(ctx: ProjectContext = Depends(get_project)) -> dict:
    """What a run would actually touch, before anything is pressed.

    The seen-store and the configured database can disagree — a populated
    seen-store against an empty DB means a run skips everything and stores
    nothing. That is invisible from the outside, so it is reported here rather
    than discovered after a fruitless run. Reports the IMAP HOST only; never a
    user or password.

    Paths are reported as BASENAMES, not absolute paths. The panel exists to show
    WHICH database and seen-store a run would use, and "live_ingest.db" vs
    "working.db" answers that; the absolute path additionally discloses the
    operator's directory layout and username to anyone who can reach this
    endpoint, which buys the panel nothing.
    """
    seen = ctx.seen_store_path
    try:
        seen_count = sum(1 for line in seen.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        seen_count = 0
    try:
        repo = ctx.open_repo()
        try:
            db_rows = sum(repo.species_counts().values())
        finally:
            repo.close()
    except Exception:
        db_rows = 0

    # Whether a credential EXISTS, and where it would come from — never its value.
    # Under the same scope a run would use, so a password the run would refuse is
    # not shown here as available.
    credential = ctx.credential_source(projects_in_scope=_server_credential_scope())

    # A live lock (this process's web job, or a `ttr run` in a terminal) is the
    # most useful thing this panel can say: pressing the button while one is held
    # is refused, and knowing that before pressing is the point.
    from ..projects import credentials
    from ..projects.lock import holder

    live = holder(ctx.dir)

    return {
        "config_error": None,
        # The project is now the most important part of "what would this touch".
        "project_id": ctx.id,
        "project_name": ctx.name,
        "imap_host": ctx.mailbox.imap_host,
        "imap_folder": ctx.mailbox.imap_folder,
        "credential_source": credential,
        "has_credential": credential is not None,
        "db_path": Path(ctx.db_path).name,
        "db_rows": db_rows,
        "seen_path": Path(seen).name,
        "seen_count": seen_count,
        "image_store_dir": Path(ctx.image_store_dir).name,
        "poll_seconds": ctx.mailbox.poll_seconds,
        "ingest_lock_held_by": (f"pid {live.get('pid')} on {live.get('host')}"
                                if live else None),
        # What the "no credential" callout should tell this machine to do. A
        # container has no credential store, so `set-password` would be a dead end.
        "credential_store_available": credentials.backend_available(),
        "credential_hint": None if credential else credentials.supply_command(ctx.id),
    }


@project_routes.post("/api/ingest/start")
def ingest_start(req: IngestStartRequest,
                 ctx: ProjectContext = Depends(get_project),
                 factory=Depends(get_pipeline_factory)) -> JSONResponse:
    from ..projects.lock import IngestLocked

    try:
        snap = RUNNER.start(mode=req.mode, level=req.level, factory=factory,
                            project_id=ctx.id, project_name=ctx.name,
                            project_dir=ctx.dir)
    except IngestBusy as exc:
        # 409 with a usable snapshot, so the page can just render the running job.
        return JSONResponse({**RUNNER.snapshot(), "error": str(exc)}, status_code=409)
    except IngestLocked as exc:
        # Someone else — very possibly `ttr run` in a terminal — holds this
        # project's lock. The in-process IngestBusy guard cannot see that.
        return JSONResponse({**RUNNER.snapshot(), "error": str(exc)}, status_code=409)
    return JSONResponse(snap, status_code=202)


@project_routes.post("/api/ingest/stop")
def ingest_stop(ctx: ProjectContext = Depends(get_project)) -> JSONResponse:
    if not RUNNER.owns(ctx.id):
        return JSONResponse(_not_this_projects_run(ctx), status_code=409)
    return JSONResponse(RUNNER.stop())   # idempotent; 200 even when already idle


@project_routes.get("/api/ingest/state")
def ingest_state(cursor: int = 0,
                 ctx: ProjectContext = Depends(get_project)) -> JSONResponse:
    """This project's run state.

    A run belonging to ANOTHER project is a 409, not that project's snapshot:
    otherwise B's page would render A's live log and image strip as though they
    were its own, which is the same class of confusion as the report cache leak.
    """
    if not RUNNER.owns(ctx.id):
        return JSONResponse(_not_this_projects_run(ctx), status_code=409)
    return JSONResponse(RUNNER.snapshot(cursor))


def _not_this_projects_run(ctx: ProjectContext) -> dict:
    running = RUNNER.project()
    return {
        "state": "foreign",
        "error": (f"an ingest is running for a different project "
                  f"({running.get('name') or running.get('id')}). This server "
                  f"runs one ingest at a time."),
        "running_project_id": running.get("id"),
        "running_project_name": running.get("name"),
        "events": [], "cursor": 0,
    }


#: Stylesheet for the standalone /print document (the headless-Chrome PDF source).
#: Same green report theme as the on-screen view, plus print-specific page-breaking
#: (item 15) and the headline-card grid (item 7). @page sets A4 for preferCSSPageSize;
#: the footer margin is supplied by Page.printToPDF, so no @page margin here.
_PRINT_STYLE = """
  :root { --green:#1b5e3f; --green-dark:#123c29; --tint:#eaf4ee; --tint-line:#c9e0d3;
          --ink:#1b1b1b; --muted:#4a4a4a; --line:#cbd6ce; --warn:#8a5a00; --warn-bg:#fdf6e9; }
  * { -webkit-print-color-adjust: exact; print-color-adjust: exact; box-sizing: border-box; }
  html, body { margin: 0; }
  body { color: var(--ink); background: #fff; font: 10.4pt/1.42 "Segoe UI", system-ui, sans-serif; }
  #report { padding: 0; }
  #report h1 { font-size: 16.5pt; color: var(--green-dark); margin: 0 0 .1rem; }
  #report h1 + p em, #report > p em:first-child { color: var(--muted); }
  #report h2 { color: var(--green); font-size: 12pt; margin: .85rem 0 .35rem;
               padding-bottom: .12rem; border-bottom: 2px solid var(--tint-line);
               break-after: avoid; break-inside: avoid; }
  #report h3 { font-size: 10.7pt; margin: .55rem 0 .22rem; break-after: avoid; }
  #report p, #report li { margin: .25rem 0; }
  #report ul { margin: .3rem 0; padding-left: 1.25rem; }
  #report em { font-style: italic; color: var(--ink); }
  #report strong { color: var(--ink); }
  #report blockquote { margin: .4rem 0; padding: .4rem .7rem; background: var(--warn-bg);
                       border-left: 5px solid var(--warn); border-radius: 4px;
                       font-style: normal; break-inside: avoid; }
  #report svg { display: block; max-width: 100%; height: auto; margin: .25rem 0; break-inside: avoid; }
  #report table { border-collapse: collapse; margin: .35rem 0; width: 100%;
                  font-variant-numeric: tabular-nums; font-size: 9.3pt; }
  #report th, #report td { border: 1px solid var(--line); padding: .2rem .45rem; text-align: left;
                           vertical-align: top; }
  #report thead th { background: var(--tint); color: var(--green-dark); }
  #report thead { display: table-header-group; }        /* repeat header across pages */
  #report tfoot { display: table-footer-group; }
  #report tr { break-inside: avoid; }
  /* Headline cards (item 7) — a print-safe grid; never split a card across a page. */
  .cards { display: flex; flex-wrap: wrap; gap: 8px; margin: .6rem 0; break-inside: avoid; }
  .card { flex: 1 1 150px; border: 1px solid var(--tint-line); border-top: 3px solid var(--green);
          border-radius: 6px; padding: .5rem .6rem; background: var(--tint); break-inside: avoid; }
  .card .k { display: block; font-size: 8pt; letter-spacing: .02em; text-transform: uppercase;
             color: var(--green-dark); }
  .card .v { display: block; font-size: 15pt; font-weight: 700; color: var(--ink); margin-top: .1rem; }
  .card .s { display: block; font-size: 8pt; color: var(--muted); }
  /* Charts (item 10) figure captions and legends. */
  figure { margin: .5rem 0; break-inside: avoid; }
  figure figcaption { font-size: 8.5pt; color: var(--muted); margin-top: .2rem; }
  .legend { display: flex; flex-wrap: wrap; gap: .3rem .9rem; font-size: 8.5pt; margin: .2rem 0; }
  .legend span { display: inline-flex; align-items: center; gap: .3rem; }
  .legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  /* Representative image evidence (item 11) — larger frames for review. */
  .thumbs { display: flex; flex-wrap: wrap; gap: 14px; }
  .thumb { width: 300px; break-inside: avoid; }
  .thumb img { width: 300px; height: auto; border: 1px solid var(--line); border-radius: 4px; }
  .thumb .cap { display: block; font-size: 8.5pt; color: var(--muted); margin-top: .2rem; }
  /* A block that must not be split across a page (e.g. the effort-index table, item 7). */
  .keep-together { break-inside: avoid; }
  @page { size: A4; }
"""


#: The three-step strip: what actually happens between pressing the button and
#: reading prose. One sentence each, describing the real pipeline.
_PIPELINE_STEPS = (
    ("database", "Stored rows",
     "One record per image, with both timestamps and per-field provenance."),
    ("chart", "Figures",
     "Activity, composition, effort and gaps &mdash; all computed, then locked."),
    ("doc", "Report",
     "Prose written around the locked figures, never over them."),
)


def _waiting_html() -> str:
    """The state before a report exists.

    Says what the page will do rather than sitting empty, and the three
    statements at the bottom are the ones every report carries.

    **The statements are imported, not written here.** Each is the report stage's
    own constant with a short grammatical lead — the same shape the report itself
    uses ("... is {claim}", "... was {claim}"). That matters more than it looks:
    `_SEND_TIME_CLAIM` records Decision 3 **as amended**, so events are dated by
    CAPTURE time where the filename decoded and by send time only as a flagged
    fallback. The sentence this page used to carry ("times are when the alert was
    sent rather than when the capture happened") is the pre-amendment claim, and
    the mockup repeats it. Importing is what makes that impossible to ship.
    """
    from .picker import _icon

    steps = "".join(
        f'<li><span class="step-ico">{_icon(name)}</span>'
        f'<p class="step-h">{head}</p><p class="step-b">{body}</p></li>'
        for name, head, body in _PIPELINE_STEPS)

    # Grammatical glue only. Every claim clause below is an imported constant.
    statements = (
        ("Counts", _COUNT_CAVEAT),
        ("Timestamps", f"Every dated event is {_SEND_TIME_CLAIM}."),
        ("Ingestion", f"Every event was {_DECISION7_CLAIM}."),
    )
    items = "".join(
        f'<li><span class="stmt-k">{_html.escape(key)}</span>'
        f'<span class="stmt-v">{_html.escape(text)}</span></li>'
        for key, text in statements)

    return (
        '<div class="waiting" id="placeholder">'
        '<p class="waiting-h">Choose a window and a species, then generate</p>'
        '<p class="waiting-b">Nothing is computed until you ask for it. '
        'This is what happens when you do:</p>'
        f'<ol class="steps3">{steps}</ol>'
        # The fence. A statement of fact about the pipeline, not decoration:
        # figures are computed and locked BEFORE any prose is requested.
        '<p class="fence"><span>no figure changes past this line</span></p>'
        '<div class="standing">'
        '<p class="standing-h">Every report states, unchanged</p>'
        f'<ol class="stmts">{items}</ol>'
        '</div>'
        '</div>')


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrapTracker Reporting Extension — demo</title>
<style>""" + _theme.BASE + """
  /* Page chrome comes from theme.py. What stays HERE is everything that governs
     the report itself — #report and the whole @media print block. Those were
     tuned against real PDF output and are the reason the palette used to be
     duplicated rather than shared; sharing the palette does not touch them. */

  /* ------------------------------------------------------- title block (§4) */
  /* The page title, the unchanged paragraph about where figures come from, and
     the ingest action. "Ingest new alerts" was an inline arrow-link inside that
     paragraph; it is the page's one other destination, so it reads as an action
     rather than as a footnote to a sentence about the language model. */
  .titlerow { display: flex; align-items: flex-start; gap: 2rem;
              margin: 2.2rem 0 1.6rem; }
  .titletext { flex: 1 1 auto; min-width: 0; }
  .titlerow h1 { font-size: clamp(38px, 3.6vw, 54px); line-height: 1;
                 letter-spacing: -.035em; color: var(--ink); margin: 0 0 .7rem; }
  .titlerow .note { margin: 0; max-width: 56ch; font-size: 1rem; }
  .ingest-cta { flex: none; display: inline-flex; align-items: center; gap: .5rem;
                white-space: nowrap; }

  /* ----------------------------------------------------- controls card (§4) */
  /* One bordered card, three bands: window, report, summary+action. Replaces
     `form.query`, which was an auto-fit grid whose columns re-flowed at
     arbitrary widths; the bands are explicit because the grouping is the point. */
  .controls {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius-card); overflow: hidden;
    margin: 0 0 1.5rem;
  }
  .band { padding: 1.3rem 1.4rem; }
  .band-window { display: flex; flex-wrap: wrap; align-items: flex-end;
                 gap: 1.4rem 2.5rem; justify-content: space-between; }
  .band-report { display: grid; grid-template-columns: 1fr 1fr;
                 gap: 1.4rem 2.5rem; align-items: start;
                 border-top: 1px solid var(--line-soft); }

  .field { display: flex; flex-direction: column; gap: .45rem; min-width: 0; }
  .flabel, .field { font-size: .7rem; font-weight: 700; letter-spacing: .09em;
                    text-transform: uppercase; color: var(--faint); }
  /* The control inside a label must not inherit the label's uppercase micro-type. */
  .field input, .field select { font-size: .97rem; font-weight: 400;
                                letter-spacing: 0; text-transform: none;
                                color: var(--ink); }

  /* ------------------------------------------------------ window presets */
  .segmented { display: inline-flex; background: var(--sunken);
               border: 1px solid var(--line); border-radius: var(--radius);
               padding: 3px; gap: 2px; }
  .segmented label { position: relative; display: block; }
  /* The radio itself is hidden from sight but NOT from the accessibility tree or
     the keyboard: it still receives focus, and the ring is drawn on the span. */
  .segmented input { position: absolute; inset: 0; opacity: 0; margin: 0;
                     width: 100%; height: 100%; cursor: pointer; }
  .segmented span {
    display: block; padding: .5rem .9rem; border-radius: 9px;
    font-size: .86rem; font-weight: 500; letter-spacing: 0;
    text-transform: none; color: var(--muted); white-space: nowrap;
    transition: background .12s ease, color .12s ease;
  }
  .segmented label:hover span { color: var(--ink); }
  .segmented input:checked + span {
    background: var(--surface); color: var(--green); font-weight: 600;
    box-shadow: 0 1px 2px rgba(16, 44, 32, .08);
  }
  .segmented input:focus-visible + span {
    outline: 2px solid var(--green-mid); outline-offset: 1px;
  }

  /* ------------------------------------------------------------ date pair */
  .dates { display: flex; align-items: flex-end; gap: .7rem; }
  .dates .tosep { padding-bottom: .75rem; color: var(--faint);
                  font-size: .86rem; }
  /* Mono for a date-as-data, matching the projects page. The VALUE is always
     ISO; `input[type=date]` renders it in the browser's own locale and no CSS
     changes that, so the typography is what can be made to match, not the order
     of the fields. */
  .controls input[type="date"] {
    font-family: var(--font-mono); font-size: .92rem;
    height: 46px; padding: 0 .7rem; border-radius: var(--radius); min-width: 10.5rem;
  }

  /* -------------------------------------------------------- report select */
  .controls select {
    height: 46px; width: 100%; padding: 0 .7rem; border-radius: var(--radius);
    background: var(--surface);
  }
  .controls input:focus-visible, .controls select:focus-visible {
    outline: none; border: 1.5px solid var(--green);
    box-shadow: 0 0 0 4px #e1ede5;
  }

  /* ----------------------------------------------------- cross-check switch */
  .switchrow { display: flex; flex-direction: column; gap: .35rem; }
  .switch { display: inline-flex; align-items: center; gap: .65rem;
            cursor: pointer; text-transform: none; letter-spacing: 0; }
  .switch input { position: absolute; opacity: 0; width: 0; height: 0; }
  .switch .track {
    position: relative; flex: none; width: 44px; height: 26px;
    border-radius: 999px; background: #cbd6ce;
    transition: background .16s ease;
  }
  .switch .track::after {
    content: ""; position: absolute; top: 3px; left: 3px;
    width: 20px; height: 20px; border-radius: 50%; background: #fff;
    box-shadow: 0 1px 3px rgba(16, 44, 32, .3);
    transition: transform .16s cubic-bezier(.2, .7, .3, 1);
  }
  .switch input:checked + .track { background: var(--green); }
  .switch input:checked + .track::after { transform: translateX(18px); }
  .switch input:focus-visible + .track {
    outline: 2px solid var(--green-mid); outline-offset: 2px;
  }
  .switchtext { font-size: .97rem; font-weight: 600; color: var(--ink);
                letter-spacing: 0; text-transform: none; }
  /* The mockup fixes this text to the switch's column and it overflows there.
     It wraps instead: the sentence is the part that explains what the control
     does, so it is the last thing that should be clipped. */
  .fhelp { margin: 0; font-size: .86rem; font-weight: 400; line-height: 1.5;
           letter-spacing: 0; text-transform: none; color: var(--muted);
           max-width: 46ch; }

  /* ----------------------------------------------- summary strip + action */
  .band-act {
    display: flex; flex-wrap: wrap; align-items: center; gap: 1rem 2rem;
    justify-content: space-between;
    background: var(--sunken); border-top: 1px solid var(--line-soft);
  }
  .summary { min-width: 0; }
  /* Says what it is. The strip is a query over the stored rows for the window in
     the fields; the report's figures come from the generator. Labelling it here
     is cheaper than a reader having to infer which of the two they are reading. */
  .summary-h { margin: 0 0 .5rem; font-size: .66rem; font-weight: 700;
               letter-spacing: .1em; text-transform: uppercase;
               color: var(--faint); }
  .summary-h span { font-weight: 500; letter-spacing: .04em; }
  .figs { display: flex; flex-wrap: wrap; align-items: stretch; gap: 0 1.3rem; }
  .fig { display: flex; flex-direction: column; gap: .1rem; padding-right: 1.3rem;
         border-right: 1px solid var(--line); }
  .fig:last-child { border-right: 0; padding-right: 0; }
  .fig strong { font-family: var(--font-display); font-weight: 700;
                font-size: 1.7rem; line-height: 1.1; letter-spacing: -.025em;
                color: var(--ink); font-variant-numeric: tabular-nums; }
  .fig span { font-size: .8rem; color: var(--muted); }
  .summary-none { margin: 0; font-size: .92rem; color: var(--muted);
                  max-width: 62ch; }
  .summary-none .mono { font-family: var(--font-mono); font-size: .88em;
                        color: var(--ink); }

  .actbtns { display: flex; flex-wrap: wrap; align-items: center; gap: .7rem;
             margin-left: auto; }
  #go { display: inline-flex; align-items: center; gap: .5rem;
        padding: .8rem 1.4rem; }
  #status { margin: .8rem 0; color: var(--green-dark); font-weight: 600;
    min-height: 1.4rem; display: flex; align-items: center; gap: .55rem; }
  /* A spinner only while something is actually running, and it is suppressed
     entirely under prefers-reduced-motion by the shared rule. */
  #status.busy::before {
    content: ""; width: .95rem; height: .95rem; flex: none; border-radius: 50%;
    border: 2px solid var(--tint-line); border-top-color: var(--green);
    animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* --------------------------------------------------- waiting state (§4) */
  /* Shown until the first report exists, so the page is never just a form above
     an empty white expanse. Dashed, because it is a container for something not
     yet made rather than a card carrying content. */
  .waiting { padding: 2.6rem 2rem 2.2rem; margin: 0 0 1.5rem;
             background: var(--surface); border: 1.5px dashed var(--dash);
             border-radius: var(--radius-card); }
  .waiting-h { margin: 0 0 .5rem; text-align: center;
               font-family: var(--font-display); font-weight: 700;
               font-size: 24px; letter-spacing: -.02em; color: var(--ink); }
  .waiting-b { margin: 0 auto 2rem; text-align: center; max-width: 52ch;
               color: var(--muted); font-size: .97rem; }

  /* ------------------------------------------------- stored -> figures -> report */
  .steps3 { list-style: none; display: grid; grid-template-columns: repeat(3, 1fr);
            margin: 0 0 1.6rem; padding: 0;
            border: 1px solid var(--line-soft); border-radius: var(--radius-panel);
            overflow: hidden; background: var(--surface); }
  .steps3 li { position: relative; padding: 1.2rem 1.3rem; min-width: 0;
               border-right: 1px solid var(--line-soft); }
  .steps3 li:last-child { border-right: 0; }
  /* The arrow between steps, drawn on the divider rather than as a list item, so
     the ordered list stays three items long for a screen reader. */
  .steps3 li + li::before {
    content: ""; position: absolute; left: -5px; top: 50%;
    width: 9px; height: 9px; transform: translateY(-50%) rotate(45deg);
    border-top: 1.5px solid var(--dash); border-right: 1.5px solid var(--dash);
    background: var(--surface);
  }
  .step-ico { display: grid; place-items: center; width: 30px; height: 30px;
              border-radius: var(--radius-sm); background: var(--tint);
              color: var(--green); margin-bottom: .8rem; }
  .step-ico .ico { width: 17px; height: 17px; }
  .step-h { margin: 0 0 .25rem; font-weight: 600; font-size: 1rem;
            color: var(--ink); }
  .step-b { margin: 0; font-size: .88rem; line-height: 1.55; color: var(--muted); }

  /* ------------------------------------------------------------------ fence */
  /* Not decoration: figures are computed and locked before any prose is asked
     for, and this is where that happens. Mono, because it reads as a rule in a
     machine rather than a caption in a brochure. */
  .fence { display: flex; align-items: center; gap: 1rem; margin: 0 0 1.4rem; }
  .fence::before, .fence::after {
    content: ""; flex: 1; border-top: 2px dashed #c6d5c9;
  }
  .fence span { font-family: var(--font-mono); font-size: .78rem;
                color: var(--faint); white-space: nowrap; }

  /* ---------------------------------------------------- standing statements */
  .standing { border: 1px solid var(--line-soft); border-radius: var(--radius-panel);
              padding: 1.2rem 1.3rem; background: var(--sunken); }
  .standing-h { margin: 0 0 .9rem; font-size: .66rem; font-weight: 700;
                letter-spacing: .1em; text-transform: uppercase;
                color: var(--faint); }
  .stmts { list-style: none; margin: 0; padding: 0; display: grid; gap: .8rem; }
  .stmts li { display: grid; grid-template-columns: 6.5rem 1fr; gap: .9rem;
              align-items: baseline; }
  .stmt-k { font-size: .8rem; font-weight: 700; letter-spacing: .04em;
            text-transform: uppercase; color: var(--green); }
  .stmt-v { font-size: .88rem; line-height: 1.6; color: var(--muted); }
  /* Report body: legible prose + a clean table, projector-friendly. */
  #report { margin-top: 1.2rem; }
  #report h1 { font-size: 1.5rem; }
  #report h2 { color: var(--green); font-size: 1.28rem; margin: 1.7rem 0 .5rem;
               padding-bottom: .2rem; border-bottom: 2px solid var(--tint-line); }
  #report p { margin: .5rem 0; }
  #report ul { margin: .5rem 0; padding-left: 1.4rem; }
  #report li { margin: .3rem 0; }
  #report em { font-style: italic; color: var(--ink); }   /* not greyed — keeps the VLM warning visible */
  #report strong { color: var(--ink); }                   /* bold stays neutral, not colour-coded */
  /* Day-by-day chart: the agent's inline SVG. Same counts as the table, shown
     as discrete bars (not a trend line). Sits above the table it mirrors. */
  #report svg { display: block; max-width: 100%; height: auto; margin: .4rem 0 .2rem; }
  #report table { border-collapse: collapse; margin: .6rem 0; font-variant-numeric: tabular-nums; }
  #report th, #report td { border: 1px solid var(--line); padding: .35rem .75rem; text-align: left; }
  #report thead th { background: var(--tint); color: var(--green-dark); }
  #report td:last-child, #report th:last-child { text-align: right; }

  /* Honesty block ("What these figures mean") is ALWAYS the last section by the
     report generator's design. Styled as a PROMINENT, integral green callout —
     body-size text, bold leads, a strong left accent — deliberately NOT a muted
     footnote/disclaimer. This is the system's core contribution; it must not be
     demoted by styling any more than by markup. */
  #report h2:last-of-type {
    background: var(--tint); color: var(--green-dark); border: 0;
    border-left: 6px solid var(--green); border-radius: 8px 8px 0 0;
    padding: .55rem .85rem; margin: 2rem 0 0; font-size: 1.3rem;
  }
  #report h2:last-of-type + ul {
    background: var(--tint); border-left: 6px solid var(--green);
    border-radius: 0 0 8px 8px; margin: 0; padding: .5rem 1.2rem .9rem 2.2rem;
    font-size: 1.05rem;
  }
  #report h2:last-of-type + ul li { margin: .55rem 0; color: var(--ink); }
  .err { color: #a12; }
  #pdf { background: #fff; color: var(--green-dark); border: 1px solid var(--green); }
  #pdf:hover { background: var(--tint); }

  /* Print / Save-as-PDF: print ONLY the report, keep colour (the green honesty
     callout, tinted table headers, and the pie fills must survive), and control
     page breaks so a heading never dangles and a table row never splits. The
     browser's native "Save as PDF" then produces the download — vector charts
     stay crisp, and no server-side PDF dependency is introduced. */
  /* ============================================== responsive (§7, Phase 1) */
  /* One breakpoint, matching the projects page, at the width where the controls
     card's two-column bands stop fitting. Everything above it is already fluid. */
  @media (max-width: 860px) {
    .titlerow { display: block; }
    .titlerow h1 { font-size: clamp(34px, 9vw, 42px); }
    .titlerow .note { max-width: none; }
    .ingest-cta { margin-top: 1.1rem; width: 100%; justify-content: center;
                  min-height: 44px; }

    .band { padding: 1.1rem 1.1rem; }
    .band-window { display: block; }
    /* The presets scroll rather than wrap: four cells on two lines reads as two
       groups, and the whole point of a segmented control is that it is one. */
    .segmented { display: flex; width: 100%; overflow-x: auto; }
    .segmented span { padding: .6rem .75rem; }
    .dates { margin-top: 1.1rem; display: grid;
             grid-template-columns: 1fr auto 1fr; align-items: end; }
    .controls input[type="date"] { min-width: 0; width: 100%; }

    .band-report { display: block; }
    .band-report .field + .field { margin-top: 1.2rem; }

    .band-act { display: block; }
    /* One figure per row, value beside its label. Three columns at this width
       wraps "days with an alert delivered" to three lines; the vertical
       dividers then wrap too and the strip reads as a ragged grid rather than
       as three figures. */
    .figs { display: grid; grid-template-columns: 1fr; gap: .55rem; }
    .fig { display: grid; grid-template-columns: auto 1fr; gap: .55rem;
           align-items: baseline; border-right: 0; padding-right: 0; }
    .fig strong { font-size: 1.3rem; }
    .fig span { font-size: .84rem; }
    .summary-none { font-size: .88rem; }

    /* `flex: 1` alone leaves the button at its content width, because the row
       is `flex-wrap: wrap` and the button is itself an inline-flex box with its
       own intrinsic size. `width: 100%` is what actually fills the line. */
    .actbtns { margin: 1.1rem 0 0; display: grid; gap: .6rem; }
    .actbtns button { width: 100%; min-height: 44px; justify-content: center; }

    .waiting { padding: 1.8rem 1.1rem 1.5rem; }
    .waiting-h { font-size: 21px; }
    /* Stacked, and the connector turns to point down the column rather than
       across a row that no longer exists. */
    .steps3 { grid-template-columns: 1fr; }
    .steps3 li { border-right: 0; border-bottom: 1px solid var(--line-soft); }
    .steps3 li:last-child { border-bottom: 0; }
    .steps3 li + li::before {
      left: 50%; top: -5px;
      transform: translateX(-50%) rotate(135deg);
    }

    /* Two lines: the label drops below the rule instead of splitting it. */
    .fence { flex-wrap: wrap; justify-content: center; gap: .5rem; }
    .fence::before { flex: 1 0 100%; }
    .fence::after { display: none; }

    .stmts li { grid-template-columns: 1fr; gap: .2rem; }
  }
  @media print {
    * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    /* Every piece of CHROME, listed one per concept rather than relying on an
       ancestor to catch it. A PDF of a report must be the report, not a
       screenshot of the tool that made it, and `_CHROME_SELECTORS` in
       `tests/test_ingest_web.py` asserts each of these appears here. When this
       page gains furniture, it goes in BOTH places, deliberately. */
    h1, .note, form, #status, #pdf, nav.whose,
    header.app,
    .titlerow, .ingest-cta, .controls, .summary, .waiting
      { display: none !important; }
    .wrap { width: auto; }
    /* padding is zeroed as well as margin: the screen body now carries fluid side
       padding, which would otherwise be added on top of the @page margins and
       narrow every printed page by a centimetre. */
    body { margin: 0; padding: 0; max-width: none; background: #fff; font-size: 11pt; }
    #report { margin: 0; }
    @page { margin: 16mm 15mm; }
    #report h1 { font-size: 16pt; }
    #report h2 { font-size: 13pt; break-after: avoid; }
    #report h3 { break-after: avoid; }
    #report thead { display: table-header-group; }   /* repeat header across pages */
    #report tr { break-inside: avoid; }
    #report svg, #report blockquote { break-inside: avoid; }
    /* Keep the honesty block ("What these figures mean") whole on one page. */
    #report h2:last-of-type, #report h2:last-of-type + ul { break-inside: avoid; }
    #report h2:last-of-type { break-before: auto; }
  }
</style></head>
<body>
<div class="wrap">
  <header class="app">
    <a class="brand" href="/"><span class="dot" aria-hidden="true"></span>TrapTracker Reporting Extension</a>
    <nav class="whose"><!--PROJECT--></nav>
  </header>
  <div class="titlerow">
    <div class="titletext">
      <h1>Monitoring reports</h1>
      <p class="note">Every figure is computed in Python from the stored rows; the
         language model writes the prose around them and cannot change a number.</p>
    </div>
    <a class="btn secondary ingest-cta" href="ingest">
      <svg class="ico" aria-hidden="true" focusable="false" viewBox="0 0 24 24"
           width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.6"
           stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 3v12M7 11l5 5 5-5M4 20h16"/></svg>Ingest new alerts</a>
  </div>
  <form id="f" class="controls">
    <div class="band band-window">
      <div class="field">
        <span class="flabel" id="window-label">Window</span>
        <!-- Real radios, not styled spans: this is a single choice among four,
             which is what a radiogroup IS. Arrow keys move between them, the
             focus ring lands on the control, and it works with the form
             disabled or JS broken. The presets only SET #start and #end; those
             two remain the authoritative parameters. -->
        <div class="segmented" role="radiogroup" aria-labelledby="window-label">
          <label><input type="radio" name="preset" value="7"><span>7 days</span></label>
          <label><input type="radio" name="preset" value="30"><span>30 days</span></label>
          <label><input type="radio" name="preset" value="all" checked><span>All records</span></label>
          <label><input type="radio" name="preset" value="custom"><span>Custom</span></label>
        </div>
      </div>
      <div class="dates">
        <label class="field">Start
          <input type="date" id="start" required></label>
        <span class="tosep" aria-hidden="true">to</span>
        <label class="field">End
          <input type="date" id="end" required></label>
      </div>
    </div>

    <div class="band band-report">
      <label class="field species">Report
        <select id="species" required><option value="">Loading…</option></select>
      </label>
      <div class="field">
        <span class="flabel" id="xcheck-label">Cross-check</span>
        <div class="switchrow">
          <label class="switch">
            <input type="checkbox" id="crosscheck" aria-describedby="xcheck-help">
            <span class="track" aria-hidden="true"></span>
            <span class="switchtext">Show BioCLIP cross-check</span>
          </label>
          <p class="fhelp" id="xcheck-help">An independent taxonomic read that
             never sees the upstream label.</p>
        </div>
      </div>
    </div>

    <div class="band band-act">
      <!-- Replaced on load from the window payload; hidden when there is none. -->
      <div class="summary" id="summary" hidden></div>
      <div class="actbtns">
        <button type="button" id="pdf" class="secondary" hidden
                title="Renders a clean PDF server-side via headless Chrome/Edge (controlled footer, no browser header)">
          Download PDF</button>
        <button type="submit" id="go">Generate report
          <svg class="ico" aria-hidden="true" focusable="false" viewBox="0 0 24 24"
               width="17" height="17" fill="none" stroke="currentColor"
               stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 12h15M13 6l6 6-6 6"/></svg></button>
      </div>
    </div>
  </form>
  <script type="application/json" id="window-data"><!--WINDOW--></script>
  <div id="status"></div>
  <div id="report"></div>
""" + _waiting_html() + """
</div>
<script>
const $ = id => document.getElementById(id);
const today = new Date(), iso = d => d.toISOString().slice(0,10);

// ---------------------------------------------------------------- window data
// One page-load aggregate: [[day, binomial, count], ...] for the whole corpus,
// days with rows only. Everything below reads it; nothing generated does — a
// report's figures come from the generator, always. `null` (or a parse failure)
// means the corpus could not be read, which is NOT the same as empty, so the
// strip hides rather than showing zeros.
let WINDOW_DATA = null;
try {
  const raw = document.getElementById('window-data').textContent.trim();
  const parsed = JSON.parse(raw);
  if (Array.isArray(parsed)) WINDOW_DATA = parsed;
} catch (e) { WINDOW_DATA = null; }

const CORPUS = (WINDOW_DATA && WINDOW_DATA.length)
  ? {first: WINDOW_DATA[0][0], last: WINDOW_DATA[WINDOW_DATA.length - 1][0]}
  : null;

// --------------------------------------------------------------- window presets
// The presets only WRITE to #start and #end. Those two fields stay the
// authoritative parameters and remain editable; editing one selects "Custom"
// rather than silently contradicting a highlighted preset.
function applyPreset(value) {
  if (value === 'custom') return;
  if (value === 'all') {
    // Default. The corpus here is historical: a rolling 30-day window lands on
    // nothing and teaches the page's worst first impression. These are the
    // project's REAL first and last dated, visible and editable in the fields.
    if (CORPUS) { $('start').value = CORPUS.first; $('end').value = CORPUS.last; }
    return;
  }
  const days = parseInt(value, 10);
  $('end').value = iso(today);
  $('start').value = iso(new Date(today.getTime() - (days - 1) * 864e5));
}

function selectedPreset() {
  const on = document.querySelector('input[name="preset"]:checked');
  return on ? on.value : 'custom';
}

// ---------------------------------------------------------------- summary strip
// A PREVIEW of the chosen window, computed from the page-load aggregate. It is
// labelled as such on the page because it is a query, not a figure: a report's
// numbers come from the generator, and nothing that generates reads this.
//
// Hidden — never zeroed — when there is no payload. An unreadable corpus is not
// an empty one, and a strip reading "0 alert events" would assert the second.
function renderSummary() {
  const box = $('summary');
  if (!WINDOW_DATA) { box.hidden = true; return; }

  const lo = $('start').value, hi = $('end').value;
  if (!lo || !hi || lo > hi) { box.hidden = true; return; }

  let events = 0;
  const days = new Set(), classes = new Set();
  for (const [day, binomial, n] of WINDOW_DATA) {
    if (day >= lo && day <= hi) { events += n; days.add(day); classes.add(binomial); }
  }
  // Denominator: calendar days in the window, inclusive. Numerator: days that
  // HAVE stored rows. That is a delivery claim and nothing more — it is not the
  // analyse stage's gap classification, which weighs operational status and is
  // the only thing entitled to call a gap an outage.
  const span = Math.round((Date.parse(hi) - Date.parse(lo)) / 864e5) + 1;

  const fig = (value, label) =>
    `<div class="fig"><strong>${value}</strong><span>${label}</span></div>`;

  if (!events) {
    // Reachable the moment anyone picks 7 days on a historical corpus. It says
    // where the records actually are rather than moving the window itself.
    const where = CORPUS
      ? ` All records covers <span class="mono">${CORPUS.first}</span> to ` +
        `<span class="mono">${CORPUS.last}</span>.`
      : '';
    box.innerHTML =
      `<p class="summary-h">Window preview <span>&middot; not report output</span></p>` +
      `<p class="summary-none">No alert events in this window.${where}</p>`;
    box.hidden = false;
    return;
  }

  box.innerHTML =
    `<p class="summary-h">Window preview <span>&middot; not report output</span></p>` +
    `<div class="figs">` +
      fig(events.toLocaleString(), 'alert events in this window') +
      fig(`${days.size} of ${span}`, 'days with an alert delivered') +
      fig(classes.size, 'classes present') +
    `</div>`;
  box.hidden = false;
}

document.querySelectorAll('input[name="preset"]').forEach(radio => {
  radio.addEventListener('change', () => { applyPreset(radio.value); renderSummary(); });
});
['start', 'end'].forEach(id => $(id).addEventListener('input', () => {
  const custom = document.querySelector('input[name="preset"][value="custom"]');
  if (custom) custom.checked = true;          // the fields are authoritative
  renderSummary();
}));

// "All records" needs the corpus; without it fall back to the rolling 30 days
// and let the user see a window that at least means something.
if (!CORPUS) {
  const thirty = document.querySelector('input[name="preset"][value="30"]');
  if (thirty) thirty.checked = true;
  applyPreset('30');
} else {
  applyPreset('all');
}
renderSummary();          // the fields are populated now; the strip reflects them

async function loadSpecies() {
  const data = await (await fetch('api/species')).json();
  $('crosscheck').checked = data.include_crosscheck_default;   // init from env default
  const species = data.species;
  const present = species.filter(s => s.count > 0).sort((a,b) => b.count - a.count);
  const absent  = species.filter(s => s.count === 0);
  const sel = $('species');
  sel.innerHTML = '';
  const mk = (s, suffix) => {
    const o = document.createElement('option');
    o.value = s.query; o.textContent = s.display + suffix; return o;
  };
  // All-species summary sits above the per-species list.
  const allOpt = document.createElement('option');
  allOpt.value = data.all_species_value;
  allOpt.textContent = 'All species (summary across the window)';
  sel.appendChild(allOpt);
  // BNG-aligned monitoring report — a third report type (not a Gain Plan, no units).
  const bngOpt = document.createElement('option');
  bngOpt.value = data.bng_value;
  bngOpt.textContent = 'BNG-aligned monitoring report (habitat-condition evidence)';
  sel.appendChild(bngOpt);
  if (present.length) {
    const g = document.createElement('optgroup');
    g.label = `In this dataset (${present.length})`;
    present.forEach(s => g.appendChild(mk(s, ` — ${s.count}`)));
    sel.appendChild(g);
  }
  const g2 = document.createElement('optgroup');
  g2.label = `Not in this dataset (${absent.length} of ${species.length} classes)`;
  absent.forEach(s => g2.appendChild(mk(s, ' (0)')));
  sel.appendChild(g2);
}

async function submitReport() {
  if (!$('species').value) return;
  $('go').disabled = true;
  $('pdf').hidden = true;                        // stale report shouldn't be printable
  $('status').textContent = 'Generating report… (this runs an LLM call, a few seconds)';
  $('status').className = 'busy';
  $('placeholder').hidden = true;        // the prompt has been taken up
  $('report').innerHTML = '';
  try {
    const r = await fetch('api/report', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        species: $('species').value, start: $('start').value, end: $('end').value,
        include_crosscheck: $('crosscheck').checked
      })
    });
    $('report').innerHTML = await r.text();     // server-sanitised, server-rendered HTML
    $('status').textContent = '';
    $('pdf').hidden = !$('report').textContent.trim();   // offer PDF once a report is shown
  } catch (err) {
    $('status').textContent = 'Error: ' + err;
    $('status').classList.add('err');
    $('placeholder').hidden = false;     // nothing was rendered; put the prompt back
  } finally {
    $('go').disabled = false;
    $('status').classList.remove('busy');
  }
}

// Server-side PDF: headless Chrome/Edge prints the /print page (Page.printToPDF) with
// a controlled footer and NO browser header/date/URL. We fetch it as a blob so the
// user gets progress and a clean download, never window.print().
$('pdf').addEventListener('click', async () => {
  const q = new URLSearchParams({ species: $('species').value,
    start: $('start').value, end: $('end').value, crosscheck: $('crosscheck').checked });
  $('pdf').disabled = true;
  const prev = $('status').textContent;
  $('status').textContent = 'Rendering PDF… (launches a headless browser, a few seconds)';
  try {
    const r = await fetch('api/report.pdf?' + q.toString());
    if (!r.ok) { $('status').textContent = 'PDF failed: ' + (await r.text()); return; }
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = url; a.download = 'faunal-monitoring-report.pdf';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    $('status').textContent = prev;
  } catch (e) { $('status').textContent = 'PDF error: ' + e; }
  finally { $('pdf').disabled = false; }
});
$('f').addEventListener('submit', e => { e.preventDefault(); submitReport(); });
// Toggling re-requests the CURRENT report from the server (no client-side hiding —
// the server decides what to render, and the suppression note stays honest).
$('crosscheck').addEventListener('change', () => { if ($('report').innerHTML) submitReport(); });
loadSpecies();
</script>
</body></html>
"""


# Mounted LAST, after every @project_routes handler is defined — an APIRouter
# is copied into the app at include time, so anything registered afterwards
# would silently not exist.
app.include_router(project_routes)
