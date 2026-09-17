"""The project picker at ``/`` — the server's entry point.

Kept beside ``ingest_page.py`` rather than in ``app.py`` because ``app.py`` is
long enough already. The palette is no longer duplicated here: it comes from
``theme.py``, which explains why the three copies were merged and what was
deliberately left behind in ``app.py``.

**There IS a creation form here now**, and the paragraph this docstring used to
carry — "there is no creation form here, and there never will be" — was written
when ``ttr serve`` bound loopback with no authentication at all. POSTing a mailbox
app password to an endpoint any local process could reach was the objection, and
it was a good one. The session token (``auth.py``) removed it: the endpoint is no
longer open to anything else on the machine. The exclusion was conditional, and the
condition has been met.

What the token did **not** change is stated on the page itself rather than only
here: plain HTTP protects nothing in transit, and a password typed into a browser
is still within reach of autofill and history in a way ``getpass`` at a terminal
is not. So the CLI remains the safer route and everything else — changing a site,
rotating a credential, archiving — is still CLI-only.

Each project's database is opened READ-ONLY here. A page load must never migrate
or stamp somebody's corpus as a side effect of being listed — the whole point of
the picker is to look at projects you have not opened yet.
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..agents import report as _report
from ..projects.context import ProjectContext
from ..projects.errors import ProjectError
from ..projects.manifest import ProjectManifest
from ..projects.registry import Registry, RegistryEntry
from ..projects.service import project_dir_for
from . import theme as _theme

#: What each alias-table source means, in the words `ttr project list` uses.
#: "bundled" is worth naming: it is an ILLUSTRATIVE table, not the operator's
#: class list, so a project running on it resolves species it may not detect and
#: fails to resolve ones it does.
_ALIAS_LABEL = {
    "manifest": "alias table: named in the manifest",
    "project": "alias table: this project's own",
    "bundled": "alias table: bundled example",
}


@dataclass
class ProjectCard:
    """Everything the picker shows for one project. Every field is optional
    because every field can be unavailable: a manifest can be unreadable, a
    database can be missing or belong to a different schema, and the picker's
    job is to say so rather than to omit the project."""

    entry: RegistryEntry
    active: bool
    #: The site NAME alone. Coordinates used to be appended to it in brackets;
    #: they are separate fields now because the card shows them in two different
    #: places — the label beside the pin, the numbers on the map.
    site: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    events: Optional[int] = None
    first: Optional[str] = None
    last: Optional[str] = None
    #: ``[(YYYY-MM-DD, count), …]`` for days that HAVE stored events, ascending.
    #: Days in between with no row are absent from this list and stay absent:
    #: see `_chart_svg` for why they must not become zeroes.
    daily: Optional[list] = None
    credential: bool = False
    #: "keyring" or "environment" when `credential` is set.
    credential_from: Optional[str] = None
    #: What to run or set when there is none, for this machine.
    credential_hint: Optional[str] = None
    alias_source: Optional[str] = None
    linked: bool = False
    problem: Optional[str] = None


def _span(db_path: Path
          ) -> tuple[Optional[int], Optional[str], Optional[str], Optional[list]]:
    """``(count, first, last, daily)`` over stored events, read-only.

    Dated by ``COALESCE(capture_time_utc, event_time_utc)`` — the same effective
    key the report buckets days by, so the range shown here is the range a report
    would cover. Read-only and best-effort: a database that cannot be read is a
    thing to report on the card, not an exception that blanks the whole page.

    The per-day aggregate is a second statement on the SAME read-only connection
    the totals already opened, grouped on the same COALESCE expression, so the
    bars cannot land on different days than the range above them. It returns only
    days that HAVE rows; nothing here invents a zero for a day it has no rows for,
    and `_chart_svg` is the other half of that promise.
    """
    if not db_path.is_file():
        return None, None, None, None
    dated = "date(COALESCE(capture_time_utc, event_time_utc))"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*), "
                "       MIN(COALESCE(capture_time_utc, event_time_utc)), "
                "       MAX(COALESCE(capture_time_utc, event_time_utc)) "
                "FROM detection_events").fetchone()
            daily = conn.execute(
                f"SELECT {dated} AS d, COUNT(*) FROM detection_events "
                f"WHERE {dated} IS NOT NULL GROUP BY d ORDER BY d").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None, None, None, None
    count, first, last = row
    day = lambda v: v[:10] if isinstance(v, str) and len(v) >= 10 else None
    series = [(d, n) for d, n in daily if isinstance(d, str) and len(d) == 10]
    return count, day(first), day(last), series


def card_for(entry: RegistryEntry, registry: Registry,
             root: Optional[Path] = None) -> ProjectCard:
    """Gather one project's row. Never raises: a broken project still appears."""
    card = ProjectCard(entry=entry, active=entry.id == registry.active_project)
    try:
        project_dir = project_dir_for(entry, root)
        manifest = ProjectManifest.read(project_dir)
        ctx = ProjectContext.from_manifest(manifest, project_dir)
    except (ProjectError, OSError, ValueError) as exc:
        card.problem = f"manifest unreadable: {exc}"
        return card

    card.linked = manifest.is_linked
    card.alias_source = ctx.alias_table_source()
    card.site = ctx.site.name or None
    # Set together or not at all — `set_site` enforces it, and the map pane
    # relies on it: half a coordinate pair would put a pin somewhere real and
    # wrong. Read straight off the manifest already loaded above; no new I/O.
    if ctx.site.latitude is not None and ctx.site.longitude is not None:
        card.latitude, card.longitude = ctx.site.latitude, ctx.site.longitude

    # Whether a credential would be FOUND, and where. The value is never read here,
    # and the picker has no route that could return one. Asking the keyring alone
    # said "no credential" for every project in a container, where the password
    # comes from the environment. The count is the server's own (every
    # non-archived project, as in `app._server_credential_scope`), so a card
    # never promises a password an ingest from this server would refuse.
    try:
        from ..projects import credentials

        source = ctx.credential_source(projects_in_scope=max(1, len(registry.visible())))
        card.credential = source is not None
        if source is not None:
            card.credential_from = ("environment" if source.startswith("environment")
                                    else "keyring")
        elif not credentials.backend_available():
            card.credential_hint = credentials.supply_command(entry.id)
    except Exception:                                    # a backend can be absent
        card.credential = False

    card.events, card.first, card.last, card.daily = _span(ctx.db_path)
    if card.events is None:
        card.problem = "database not readable from here"
    return card


def _esc(value) -> str:
    return _html.escape(str(value))


# --------------------------------------------------------------------- icons #
#: Line icons, drawn here rather than pulled from a package. An icon font or an
#: SVG sprite library would be a network request or a new static mount, and this
#: server is local, may be offline, and has neither. 24x24, stroked in
#: `currentColor` so an icon takes the colour of the text it sits beside.
_ICON_PATHS = {
    "database": "M4 6c0-1.1 3.6-2 8-2s8 .9 8 2-3.6 2-8 2-8-.9-8-2Zm0 0v12c0 1.1 "
                "3.6 2 8 2s8-.9 8-2V6M4 12c0 1.1 3.6 2 8 2s8-.9 8-2",
    "image": "M3 5h18v14H3zM3 16l5-5 4 4 3-3 6 6",
    "mail": "M3 6h18v12H3zM3 7l9 6 9-6",
    "tag": "M3 3h8l10 10-8 8L3 11zM7.5 7.5h.01",
    "pin": "M12 21s7-6.3 7-11a7 7 0 1 0-14 0c0 4.7 7 11 7 11Z M12 10.5h.01",
    "check": "M4 12.5 9 17.5 20 6.5",
    "check-circle": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM8 12l3 3 5-5",
    "info": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 11v5M12 7.5h.01",
    "shield": "M12 3 5 6v5.5c0 4.4 3 8.2 7 9.5 4-1.3 7-5.1 7-9.5V6Z",
    "lock": "M5 11h14v9H5zM8 11V7.5a4 4 0 0 1 8 0V11",
    "warning": "M12 3 2 20h20ZM12 10v4M12 17.5h.01",
    "calendar": "M4 6h16v14H4zM4 10h16M8.5 3v4M15.5 3v4",
    "arrow-right": "M4 12h15M13 6l6 6-6 6",
    "plus": "M12 5v14M5 12h14",
    "copy": "M9 9h11v11H9zM5 15H4V4h11v1",
    "terminal": "M5 7l4 4-4 4M12 16h7",
    # Added for the reports page's three-step strip (stored rows -> figures ->
    # report). One icon set for the whole web layer, not one per page.
    "chart": "M5 20V11M10 20V5M15 20v-6M20 20V8",
    "doc": "M6 3h8l4 4v14H6zM14 3v4h4M9 12h6M9 16h6",
    # Added for the ingest page's pipeline strip and its run controls.
    "sparkle": "M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9Z"
               "M18 15.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7Z",
    "refresh": "M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5",
    "stop": "M7 7h10v10H7z",
}


def _icon(name: str, *, label: Optional[str] = None, size: int = 24) -> str:
    """One inline SVG icon.

    `label` gives it an accessible name; without one it is marked
    ``aria-hidden`` — an icon sitting beside its own label is decoration, and
    announcing it twice is worse than not announcing it (§11).
    """
    path = _ICON_PATHS[name]
    if label:
        a11y = f'role="img" aria-label="{_esc(label)}"'
    else:
        a11y = 'aria-hidden="true" focusable="false"'
    return (f'<svg class="ico" {a11y} viewBox="0 0 24 24" width="{size}" '
            f'height="{size}" fill="none" stroke="currentColor" stroke-width="1.6" '
            f'stroke-linecap="round" stroke-linejoin="round">'
            f'<path d="{path}"/></svg>')


def _copy_button(value: str, what: str) -> str:
    """A copy-to-clipboard control. Falls back to selecting the text when the
    clipboard API is unavailable, which it is on plain HTTP in some browsers —
    this page is plain HTTP by design, so that is the ordinary case, not an edge."""
    return (f'<button type="button" class="copy" data-copy="{_esc(value)}" '
            f'title="Copy {_esc(what)}" aria-label="Copy {_esc(what)}">'
            f'{_icon("copy", size=14)}</button>')


#: The topographic contour wash behind the hero and the create tile. Inline
#: paths, no raster, no request. Purely decorative, so `aria-hidden`.
_CONTOURS = (
    '<svg class="contours" aria-hidden="true" viewBox="0 0 800 600" '
    'preserveAspectRatio="xMidYMid slice" fill="none" stroke="currentColor" '
    'stroke-width="1">'
    '<ellipse cx="560" cy="250" rx="70" ry="52"/>'
    '<ellipse cx="560" cy="250" rx="120" ry="92"/>'
    '<ellipse cx="558" cy="252" rx="172" ry="134"/>'
    '<ellipse cx="556" cy="254" rx="226" ry="178"/>'
    '<ellipse cx="554" cy="256" rx="282" ry="224"/>'
    '<ellipse cx="552" cy="258" rx="340" ry="272"/>'
    '<ellipse cx="550" cy="260" rx="400" ry="322"/>'
    '<ellipse cx="548" cy="262" rx="462" ry="374"/>'
    '</svg>')


#: The isolation paragraph, which the strip below it makes visible. One literal:
#: the strip's four chips and this sentence must not be able to disagree about
#: what a project owns.
_ISOLATION = ("Each project has its own database, image store, mailbox and "
              "species table. Nothing is shared between them, and nothing on "
              "this page crosses from one to another.")

_ISOLATION_CHIPS = (("database", "Database"), ("image", "Image store"),
                    ("mail", "Mailbox"), ("tag", "Species table"))


def _posture_html() -> str:
    """The server-posture pill in the top bar.

    Unconditional, and true unconditionally: `auth.py` fails closed, so a page
    that rendered at all was fetched with a valid session token. It is not a
    status that can be wrong, which is why it carries no live check.
    """
    return (f'<span class="posture">{_icon("lock")}'
            f'Local server &middot; session token</span>')


def _hero_html(visible: list, archived: list) -> str:
    """Status line, title, the isolation paragraph, and the New project button.

    The status line counts the real lists. It does NOT count "active": the only
    thing resembling one is `registry.active_project`, which is whichever project
    `ttr project use` selected, so the figure could only ever be 0 or 1 and would
    read as a count of mailboxes still delivering — which nothing on this page
    knows. The card's pill says "current" for that, and says it once, where the
    word is attached to the project it describes. See `_status_pill`.

    Archived is appended only when there are some: "· 0 archived" is a count of
    nothing, and a status line should not carry a figure that is always absent
    for most deployments.
    """
    n = len(visible)
    line = f"{n} project{'' if n == 1 else 's'}"
    if archived:
        line += f" &middot; {len(archived)} archived"
    return (
        '<section class="hero reveal">'
        f'<span class="hero-ink" aria-hidden="true">{_CONTOURS}</span>'
        '<div class="hero-body">'
        f'<p class="eyebrow"><span class="pip" aria-hidden="true"></span>{line}</p>'
        '<h1>Projects</h1>'
        f'<p class="lede">{_ISOLATION}</p>'
        '</div>'
        '<div class="hero-act">'
        f'<button type="button" class="btn-new" data-open-create>'
        f'{_icon("plus", size=18)}New project</button>'
        '</div>'
        '</section>')


def _isolation_strip_html() -> str:
    """The paragraph above, made visible. Carries no data by design (§5.3)."""
    chips = "".join(
        f'<li><span class="chip-ico">{_icon(name)}</span>{_esc(label)}</li>'
        for name, label in _ISOLATION_CHIPS)
    return (f'<ul class="isolation reveal">{chips}'
            f'<li class="isolation-note">&mdash; one of each, per project</li></ul>')


def _create_tile_html() -> str:
    """The last cell of the grid, in every state including no projects at all.

    Offers both routes and says which is safer in the same breath, rather than
    offering the browser route and burying the caveat in a panel further down.
    """
    return (
        '<article class="tile">'
        f'<span class="tile-ink" aria-hidden="true">{_CONTOURS}</span>'
        '<div class="tile-body">'
        f'<span class="tile-plus" aria-hidden="true">{_icon("plus", size=22)}</span>'
        '<h2>Create a project</h2>'
        '<p>Name it, pin the site and connect its mailbox. Everything it '
        'records stays inside it.</p>'
        '<button type="button" class="btn-start" data-open-create>'
        'Start in browser</button>'
        '<p class="tile-or"><span>or, safer, in a terminal</span></p>'
        '<p class="cmd-chip"><code>ttr project create</code>'
        f'{_copy_button("ttr project create", "the create command")}</p>'
        '</div></article>')


def _status_pill(card: ProjectCard) -> str:
    """The pill in the card's top-right corner, or nothing.

    NOT called "active". `registry.active_project` is the project
    ``ttr project use`` selected — it says which project the CLI acts on by
    default, and nothing whatever about whether a mailbox is still delivering.
    A green ACTIVE beside a live-looking dot asserts exactly the liveness this
    page cannot see, so the word is CURRENT and the dot does not pulse. If a
    real last-delivery timestamp is ever stored, THAT is the thing to surface
    as liveness, and it is a different pill.
    """
    if card.entry.archived:
        return ('<span class="pill pill-archived">'
                '<span class="pill-dot" aria-hidden="true"></span>Archived</span>')
    if card.active:
        return ('<span class="pill pill-current">'
                '<span class="pill-dot" aria-hidden="true"></span>Current</span>')
    return ""


def _rate_html(card: ProjectCard) -> str:
    """"≈ 20 a day over 40 days", or nothing at all.

    Derived from the DATED SPAN rather than from the number of bars: days with no
    delivery are part of the period the average covers, and dividing by the bar
    count instead would quietly report the rate on delivering days only — a
    higher, flattering, different number.

    Omitted when either input is missing, and when the span is under two days:
    "a day" over one day is not a rate, it is the same figure twice.
    """
    if not (card.events and card.first and card.last):
        return ""
    days = _span_days(card.first, card.last)
    if days is None or days < 2:
        return ""
    per_day = card.events / days
    shown = f"{per_day:.1f}" if per_day < 10 else f"{per_day:,.0f}"
    return (f'<span class="rate"> &#8776; {shown} a day over '
            f'{days:,} days</span>')


def _span_days(first: str, last: str) -> Optional[int]:
    """Inclusive day count between two ``YYYY-MM-DD`` strings."""
    try:
        a = _dt.date.fromisoformat(first)
        b = _dt.date.fromisoformat(last)
    except ValueError:
        return None
    return (b - a).days + 1


def _count_html(card: ProjectCard) -> str:
    """The headline number and its unit.

    "alert event" is spelled out every time rather than shortened to "events" —
    Decision 4 is that a count is of ALERTS, not of animals, and the unit line is
    exactly where a reader would otherwise supply the wrong noun themselves.
    """
    if card.events is None:
        return '<span class="unknown">stored total unavailable</span>'
    if card.events == 0:
        return '<span class="unknown">no events stored yet</span>'
    plural = "s" if card.events != 1 else ""
    # The space lives INSIDE the unit span, not between the tags. Flex `gap`
    # spaces these visually, but a reader — and a screen reader — gets the text
    # with the tags removed, and "16alert events" is what that produces without
    # it. `test_web_projects` pins the reader's text for exactly this reason.
    return (f'<span class="n">{card.events:,}</span>'
            f'<span class="unit"> alert event{plural}</span>')


#: Chart geometry. Bars are 5px on a 3px gap, so a day occupies 8px.
_BAR_W, _BAR_GAP, _CHART_H = 5, 3, 58


def _chart_html(card: ProjectCard) -> str:
    """A day-by-day bar chart of stored alert events, as inline SVG.

    **A day with no rows is not a zero.** The email source cannot distinguish "no
    wildlife triggered the camera" from "nothing was delivered that day" — an
    outage, a backlog, a mailbox that stopped being polled all look identical
    from here. So a day inside the span with no stored rows gets a faint baseline
    tick, visibly unlike a short bar, and its hover text says "no alert events
    delivered". Delivered is the only claim this page can support.

    That is deliberately as far as it goes. Classifying a gap — outage versus
    genuine quiet — is the analyse stage's job, it is done properly there against
    operational status, and a card that guessed at it would be competing with the
    report rather than pointing at it.

    Returns "" when there is no series, and the panel keeps its shape without it.
    """
    series = card.daily or []
    if not series or not card.first or not card.last:
        return ""
    days = _span_days(card.first, card.last)
    if days is None or days < 1:
        return ""

    counts = {d: n for d, n in series}
    peak = max(counts.values())
    start = _dt.date.fromisoformat(card.first)
    # A long corpus would otherwise draw a bar narrower than a hairline; past
    # this width the chart stops being readable and is dropped rather than
    # compressed into a smear that implies a precision it does not have.
    if days > 400:
        return ""

    bars, step = [], _BAR_W + _BAR_GAP
    for i in range(days):
        day = (start + _dt.timedelta(days=i)).isoformat()
        x = i * step
        n = counts.get(day)
        if n is None:
            bars.append(
                f'<rect class="gap" x="{x}" y="{_CHART_H - 2}" width="{_BAR_W}" '
                f'height="2" rx="1"><title>{day} &mdash; no alert events '
                f'delivered</title></rect>')
            continue
        h = max(3, round(n / peak * (_CHART_H - 4)))
        plural = "s" if n != 1 else ""
        bars.append(
            f'<rect class="bar" x="{x}" y="{_CHART_H - h}" width="{_BAR_W}" '
            f'height="{h}" rx="2"><title>{day} &mdash; {n:,} alert event{plural}'
            f'</title></rect>')

    width = days * step - _BAR_GAP
    # The bars are one reading of the series; the table below is the same numbers
    # as text, for anyone who cannot hover an SVG (§11). It is the chart's
    # accessible equivalent, not a duplicate of it, so the chart itself is hidden
    # from the tree rather than announced twice.
    rows = "".join(f"<tr><td>{d}</td><td>{n:,}</td></tr>" for d, n in series)
    return (
        f'<figure class="chart">'
        f'<svg viewBox="0 0 {width} {_CHART_H}" width="100%" height="{_CHART_H}" '
        f'preserveAspectRatio="none" aria-hidden="true" focusable="false">'
        f'{"".join(bars)}</svg>'
        f'<span class="baseline" aria-hidden="true"></span>'
        f'<figcaption class="chart-x">'
        f'<span class="mono">{_esc(card.first)}</span>'
        f'<span>Daily alert events</span>'
        f'<span class="mono">{_esc(card.last)}</span></figcaption>'
        f'<table class="vh"><caption>Alert events per day. Days absent from this '
        f'table had no alert events delivered.</caption><thead><tr><th>Day</th>'
        f'<th>Alert events</th></tr></thead><tbody>{rows}</tbody></table>'
        f'</figure>')


def _stat_panel_html(card: ProjectCard) -> str:
    """The sunken panel: the count, the derived rate, the chart, the caption.

    The caption is `report.COUNT_CAVEAT` — the same literal the report prints in
    its limitations list, imported rather than retyped. Its value is that it is
    byte-identical wherever it appears, and a second copy is how that stops being
    true. It is unconditional: it states what the number above it means, and that
    does not become less true when the number is small or absent.
    """
    return (
        '<div class="stat">'
        f'<p class="count">{_count_html(card)}{_rate_html(card)}</p>'
        f'{_chart_html(card)}'
        f'<p class="caveat">{_icon("info")}'
        f'<span>{_esc(_report.COUNT_CAVEAT)}</span></p>'
        '</div>')


def _meta_rows_html(card: ProjectCard) -> str:
    """Dated / Mailbox / Species, hairline-separated.

    Dates stay ISO. The design asked for "28 Jun – 6 Aug 2026", but these are
    dates-as-data in a dissertation artifact: ISO is unambiguous across locales,
    it is the form every other surface in this project prints, and it is what the
    reader would type back into a report window.
    """
    if card.first and card.last:
        span = (f'<span class="mono">{_esc(card.first)} to '
                f'{_esc(card.last)}</span>')
    elif card.events == 0:
        span = '<span class="unknown">nothing stored</span>'
    else:
        span = '<span class="unknown">undated</span>'

    if card.credential:
        where = "from the environment" if card.credential_from == "environment" else "stored"
        credential = f'{_icon("check-circle")}<span>mailbox credential {where}</span>'
    else:
        hint = card.credential_hint or "ttr project set-password"
        credential = ('<span class="unknown">no mailbox credential</span>'
                      f'<code class="hintcmd">{_esc(hint)}</code>')

    alias = _ALIAS_LABEL.get(card.alias_source or "", "alias table: unknown")
    rows = (("calendar", "Dated", span),
            ("mail", "Mailbox", credential),
            ("tag", "Species", _esc(alias)))
    return "".join(
        f'<div class="meta"><span class="meta-k">{_icon(ico)}{_esc(label)} </span>'
        f'<span class="meta-v">{value}</span></div>'
        for ico, label, value in rows)


#: The site map's ground — a stylised sketch, drawn once and reused for every
#: project. It is NOT cartography and makes no claim to be: no tile provider, no
#: network request, no relationship between these roads and any real street. The
#: only real thing in the pane is the coordinate chip beneath it, which is why
#: that chip is text rather than a label baked into the drawing.
_MAP_GROUND = (
    '<rect width="300" height="420" fill="#e8f0ea"/>'
    '<g fill="#dde9e0">'
    '<rect x="14" y="18" width="46" height="34" rx="3"/>'
    '<rect x="74" y="12" width="38" height="30" rx="3"/>'
    '<rect x="18" y="70" width="34" height="42" rx="3"/>'
    '<rect x="70" y="62" width="44" height="36" rx="3"/>'
    '<rect x="196" y="20" width="50" height="38" rx="3"/>'
    '<rect x="256" y="30" width="32" height="44" rx="3"/>'
    '<rect x="204" y="78" width="40" height="30" rx="3"/>'
    '<rect x="16" y="300" width="42" height="36" rx="3"/>'
    '<rect x="74" y="312" width="36" height="30" rx="3"/>'
    '<rect x="198" y="298" width="48" height="40" rx="3"/>'
    '<rect x="252" y="352" width="36" height="34" rx="3"/>'
    '<rect x="20" y="356" width="40" height="30" rx="3"/>'
    '<rect x="120" y="376" width="44" height="32" rx="3"/>'
    '</g>'
    # Parks, in the accent at low alpha so the map reads as one tinted family.
    '<g fill="#1b5e3f" opacity=".14">'
    '<ellipse cx="150" cy="212" rx="74" ry="52"/>'
    '<ellipse cx="44" cy="196" rx="34" ry="26"/>'
    '<ellipse cx="268" cy="240" rx="30" ry="38"/>'
    '</g>'
    # Roads last, so they sit over the parks.
    '<g stroke="#ffffff" fill="none" stroke-linecap="round">'
    '<path d="M-10 132 H310" stroke-width="13"/>'
    '<path d="M-10 288 H310" stroke-width="10"/>'
    '<path d="M156 -10 C150 90 186 150 168 220 C152 284 180 350 172 430" '
    'stroke-width="15"/>'
    '<path d="M62 -10 C58 80 30 150 44 430" stroke-width="7"/>'
    '</g>')


def _map_html(card: ProjectCard) -> str:
    """The card's right-hand pane: the stylised map, a SITE tag, a coordinate chip.

    With no coordinates stored there is no pin — the pane is ground only, and the
    chip says so and names the command that fixes it, the same way the create
    tile points at `ttr project create`. A pin at the centre of a decorative map
    with no coordinates behind it would be a location the project does not have.
    """
    if card.latitude is not None and card.longitude is not None:
        pin = (
            '<g class="pin" transform="translate(150 210)">'
            '<circle class="pulse" r="15" fill="#1b5e3f" opacity=".25"/>'
            '<path d="M0-26c-7.7 0-14 6.3-14 14 0 10.5 14 26 14 26s14-15.5 '
            '14-26c0-7.7-6.3-14-14-14Z" fill="#1b5e3f"/>'
            '<circle cy="-12" r="5" fill="#ffffff"/>'
            '</g>')
        chip = (f'{_icon("pin", size=13)}'
                f'<span class="mono">{card.latitude:.4f}, '
                f'{card.longitude:.4f}</span>')
        alt = (f"Stylised locator for {card.site or card.entry.name}, "
               f"centred on {card.latitude:.4f}, {card.longitude:.4f}. "
               f"Not a street map.")
    else:
        pin = ""
        chip = (f'{_icon("pin", size=13)}'
                f'<span class="unknown">No coordinates set</span>'
                f'<code class="hintcmd">ttr project set-site</code>')
        alt = "No coordinates are stored for this project."

    return (
        f'<div class="card-map">'
        f'<svg class="map" viewBox="0 0 300 420" preserveAspectRatio="xMidYMid slice" '
        f'role="img" aria-label="{_esc(alt)}">{_MAP_GROUND}{pin}</svg>'
        f'<span class="map-tag">Site</span>'
        f'<span class="map-coords">{chip}</span>'
        f'</div>')


def _card_html(card: ProjectCard) -> str:
    """One project, as two panes: content left, site map right."""
    e = card.entry
    marks = []
    if card.linked:
        marks.append('<span class="mark">linked storage</span>')
    badges = " ".join(marks)

    site = (f'{_icon("pin")}<span>{_esc(card.site)}</span>' if card.site
            else f'{_icon("pin")}<span class="unknown">site unnamed</span>')
    problem = (f'<p class="problem">{_esc(card.problem)}</p>'
               if card.problem else "")

    return (
        f'<article class="card">'
        f'<div class="card-main">'
        f'<div class="card-head">'
        f'<h2><a href="/p/{_esc(e.id)}/">{_esc(e.name)}</a></h2>'
        f'<span class="idtag">{_esc(e.id[:8])}</span>'
        f'{_copy_button(e.id, "the full project id")}'
        f'{badges}{_status_pill(card)}'
        f'</div>'
        f'<p class="site">{site}</p>'
        f'{_stat_panel_html(card)}'
        f'<div class="metas">{_meta_rows_html(card)}</div>'
        f'{problem}'
        f'<p class="card-go"><a class="btn outline" href="/p/{_esc(e.id)}/">'
        f'Open project{_icon("arrow-right", size=17)}</a></p>'
        f'</div>'
        f'{_map_html(card)}'
        f'</article>')


_STYLE = _theme.BASE + """
  /* ============================================================ page shell */
  /* 1200px column with generous gutters, narrowing to 20px on a phone (§10).
     `clamp` rather than a media query: the gutter should shrink continuously,
     and the one breakpoint this page has is reserved for the layout changes
     that genuinely cannot be expressed as a fluid value. */
  body { padding: 0 clamp(20px, 8vw, 120px) 5rem; }
  .wrap { width: min(1200px, 100%); }

  header.app { padding: 0; height: 64px; margin-bottom: 0; }
  header.app .brand { font-size: 1.06rem; }
  .posture {
    display: inline-flex; align-items: center; gap: .4rem;
    font-size: .84rem; color: var(--muted);
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 999px; padding: .34rem .8rem .34rem .65rem;
  }
  .posture .ico { width: 15px; height: 15px; color: var(--faint); }

  /* ================================================================== hero */
  .hero { position: relative; display: flex; align-items: flex-end;
          gap: 2rem; padding: 3.4rem 0 2.2rem; }
  .hero-body { flex: 1 1 auto; min-width: 0; }
  .hero-act { flex: none; padding-bottom: .5rem; }

  /* The contour wash. Absolutely placed and clipped to the hero, drawn in the
     accent at low alpha, and inert to the pointer so it can never eat a click. */
  .hero-ink {
    position: absolute; inset: -1rem -8vw -1rem 0; overflow: hidden;
    pointer-events: none; z-index: 0; color: var(--green);
  }
  .hero-ink .contours { width: 100%; height: 100%; opacity: .13; }
  .hero-body, .hero-act { position: relative; z-index: 1; }

  .eyebrow {
    display: flex; align-items: center; gap: .5rem; margin: 0 0 .9rem;
    font-size: .78rem; font-weight: 600; letter-spacing: .09em;
    text-transform: uppercase; color: var(--muted);
  }
  .eyebrow .pip { width: .45rem; height: .45rem; border-radius: 50%;
                  background: var(--green); flex: none; }

  .hero h1 {
    font-size: clamp(46px, 4.7vw, 68px); line-height: .95;
    letter-spacing: -.035em; color: var(--ink); margin: 0 0 1.1rem;
  }
  .lede { margin: 0; max-width: 52ch; font-size: 1.05rem; color: var(--muted); }

  .btn-new {
    display: inline-flex; align-items: center; gap: .5rem; white-space: nowrap;
    padding: .85rem 1.5rem; border-radius: var(--radius);
    font-size: 1rem; box-shadow: 0 10px 22px -12px rgba(19, 37, 27, .5);
  }

  /* ======================================================= isolation strip */
  .isolation {
    list-style: none; margin: .4rem 0 2.6rem; padding: 0;
    display: flex; flex-wrap: wrap; align-items: center; gap: .7rem;
  }
  .isolation li {
    display: inline-flex; align-items: center; gap: .55rem;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); padding: .5rem .95rem .5rem .5rem;
    font-size: .93rem; font-weight: 500; color: var(--ink);
  }
  .isolation .chip-ico {
    display: grid; place-items: center; width: 28px; height: 28px; flex: none;
    border-radius: var(--radius-sm); background: var(--tint); color: var(--green);
  }
  .isolation .chip-ico .ico { width: 16px; height: 16px; }
  .isolation .isolation-note {
    background: none; border: 0; padding: 0 0 0 .3rem;
    color: var(--faint); font-weight: 400; font-size: .9rem;
  }

  /* ======================================================= copy-to-clipboard */
  .copy {
    background: none; border: 0; padding: .15rem; margin: 0; line-height: 0;
    color: inherit; opacity: .45; border-radius: 5px; flex: none;
    transition: opacity .12s ease;
  }
  .copy:hover, .copy:focus-visible { opacity: 1; background: none; }
  :hover > .copy, :hover .copy { opacity: 1; }
  .copy.done { opacity: 1; color: var(--green); }

  /* ============================================================== entrance */
  /* One entrance, once, and nothing here loops. Sections start shifted and
     settle; `prefers-reduced-motion` removes it entirely rather than shortening
     it, because a 14px lurch at 1ms is a flash, not a gentler animation. */
  .reveal { animation: rise .7s cubic-bezier(.2, .7, .3, 1) both; }
  .reveal:nth-of-type(2) { animation-delay: .08s; }
  .reveal:nth-of-type(3) { animation-delay: .16s; }
  .reveal:nth-of-type(4) { animation-delay: .24s; }
  @keyframes rise { from { opacity: 0; transform: translateY(14px); } }
  @media (prefers-reduced-motion: reduce) {
    .reveal { animation: none; }
  }

  /* ============================================================ project grid */
  /* Three equal columns; a project card spans two, the create tile takes the
     remaining one and is always last in source order, so it is last for a
     screen reader and for the keyboard as well as for the eye (§5.4). */
  .grid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px;
    margin: 0 0 24px;
    /* `dense` because a 2-column card in a 3-column grid never fills a row on
       its own: without it every row but the last carries a dead third column,
       and the tile sits at the bottom beside the last card. The design was drawn
       with one project, where the question does not arise. Dense backfills the
       tile into the first gap, which reproduces the drawing exactly at one
       project and stays tidy at any number.
       It changes only PAINT order — the tile is still last in the source, so it
       is still last for the keyboard and for a screen reader. */
    grid-auto-flow: row dense;
  }
  .grid > .card { grid-column: span 2; }
  .grid > .tile { grid-column: span 1; }

  /* ------------------------------------------------------------ create tile */
  .tile {
    position: relative; overflow: hidden;
    border: 1.5px dashed var(--dash); border-radius: var(--radius-card);
    background: var(--surface);
    display: grid; place-items: center; text-align: center;
    padding: 2.6rem 1.7rem; min-height: 22rem;
  }
  .tile-ink { position: absolute; inset: 0; pointer-events: none;
              color: var(--green); }
  .tile-ink .contours { width: 100%; height: 100%; opacity: .1; }
  .tile-body { position: relative; z-index: 1; width: 100%; }
  .tile-plus {
    display: grid; place-items: center; width: 56px; height: 56px;
    margin: 0 auto 1.2rem; border-radius: 50%;
    background: var(--surface); border: 1px solid var(--line);
    color: var(--green); box-shadow: var(--shadow);
    transition: transform .25s cubic-bezier(.2, .7, .3, 1);
  }
  .tile:hover .tile-plus { transform: rotate(90deg); }
  .tile h2 { font-size: 24px; margin: 0 0 .55rem; color: var(--ink);
             letter-spacing: -.02em; }
  .tile p { margin: 0 auto; max-width: 26ch; color: var(--muted);
            font-size: .95rem; }
  .btn-start { margin-top: 1.4rem; padding: .7rem 1.4rem; }

  .tile-or {
    display: flex; align-items: center; gap: .7rem;
    margin: 1.5rem 0 1rem !important; max-width: none !important;
    color: var(--faint); font-size: .82rem;
  }
  .tile-or::before, .tile-or::after {
    content: ""; flex: 1; height: 1px; background: var(--line);
  }

  .cmd-chip {
    display: inline-flex; align-items: center; gap: .5rem;
    background: var(--term-bg); border-radius: var(--radius-sm);
    padding: .5rem .7rem .5rem .85rem; margin: 0 !important;
  }
  .cmd-chip code { font-family: var(--font-mono); font-size: .84rem;
                   color: var(--term-ink); }
  .cmd-chip code::before { content: "$ "; color: var(--term-prompt); }
  .cmd-chip .copy { color: var(--term-ink); }

  /* =========================================================== project card */
  .card {
    position: relative; display: grid; grid-template-columns: 1fr 300px;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius-card); overflow: hidden;
    transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
  }
  .card:hover {
    transform: translateY(-3px); box-shadow: var(--shadow-card);
    border-color: var(--tint-line);
  }
  .card-main { padding: 1.6rem 1.7rem 1.4rem; display: flex;
               flex-direction: column; min-width: 0; }

  .card-head { display: flex; align-items: center; flex-wrap: wrap; gap: .55rem;
               margin-bottom: .35rem; }
  .card h2 { font-size: 30px; margin: 0; color: var(--ink);
             letter-spacing: -.025em; line-height: 1.1; }
  /* The whole card is the target, not just the characters of the name. The
     overlay sits under the controls below, which raise themselves above it. */
  .card h2 a { color: inherit; text-decoration: none; }
  .card h2 a::after { content: ""; position: absolute; inset: 0; z-index: 0; }
  .card h2 a:hover { text-decoration: underline; text-underline-offset: 3px; }
  .card .idtag { font-size: .76rem; }
  .card-head .copy, .card-head .pill, .card-head .mark { position: relative;
                                                         z-index: 1; }

  .pill {
    display: inline-flex; align-items: center; gap: .38rem; margin-left: auto;
    font-size: .68rem; font-weight: 700; letter-spacing: .07em;
    text-transform: uppercase; white-space: nowrap;
    border-radius: 999px; padding: .26rem .7rem;
    background: var(--tint); color: var(--green); border: 1px solid var(--tint-line);
  }
  .pill-dot { width: .4rem; height: .4rem; border-radius: 50%;
              background: currentColor; flex: none; }
  /* No pulse. The dot marks which project the CLI acts on, not a live feed --
     see `_status_pill`. */
  .pill-archived { background: var(--bg); color: var(--faint);
                   border-color: var(--line); }

  .card .site {
    display: flex; align-items: center; gap: .45rem; position: relative;
    margin: 0 0 1.1rem; color: var(--muted); font-size: .97rem;
  }
  .card .site .ico { width: 16px; height: 16px; color: var(--faint); flex: none; }

  /* ------------------------------------------------------------ stat panel */
  .stat { position: relative; background: var(--sunken);
          border: 1px solid var(--line-soft); border-radius: var(--radius-panel);
          padding: 1.15rem 1.25rem 1rem; }
  .stat .count { display: flex; align-items: baseline; flex-wrap: wrap;
                 gap: .5rem; margin: 0 0 .9rem; }
  .stat .count .n { font-family: var(--font-display); font-weight: 700;
                    font-size: 56px; line-height: .9; letter-spacing: -.035em;
                    color: var(--ink); }
  .stat .count .unit { font-size: .97rem; color: var(--muted); }
  .stat .count .unknown { font-size: 1.05rem; }
  .stat .count .rate { margin-left: auto; font-size: .88rem; color: var(--faint);
                       white-space: nowrap; }

  /* ---------------------------------------------------------------- chart */
  .chart { margin: 0 0 .85rem; position: relative; }
  .chart svg { display: block; overflow: visible; }
  .chart .bar { fill: var(--green); transition: fill .12s ease, opacity .12s ease; }
  /* A day with no delivery. Deliberately unlike a short bar: it sits ON the
     baseline in the hairline colour and never grows. */
  .chart .gap { fill: var(--dash); }
  .chart:hover .bar { opacity: .32; }
  .chart .bar:hover { opacity: 1; }
  .chart .baseline { display: block; height: 1px; background: var(--line);
                     margin-top: 1px; }
  .chart-x { display: flex; justify-content: space-between; align-items: center;
             gap: 1rem; margin-top: .5rem; font-size: .74rem; color: var(--faint); }
  .chart-x .mono { font-family: var(--font-mono); }

  .caveat { display: flex; align-items: flex-start; gap: .45rem; margin: 0;
            font-size: .79rem; line-height: 1.45; color: var(--faint); }
  .caveat .ico { width: 14px; height: 14px; flex: none; margin-top: .18rem; }

  /* ------------------------------------------------------------ meta rows */
  .metas { margin: 1.1rem 0 0; position: relative; }
  .meta { display: grid; grid-template-columns: 78px 1fr; gap: .9rem;
          align-items: center; padding: .62rem 0;
          border-bottom: 1px solid var(--hairline); font-size: .91rem; }
  .meta:first-child { border-top: 1px solid var(--hairline); }
  .meta-k { display: inline-flex; align-items: center; gap: .4rem;
            color: var(--faint); }
  .meta-k .ico { width: 15px; height: 15px; flex: none; }
  .meta-v { display: inline-flex; align-items: center; gap: .45rem;
            flex-wrap: wrap; color: var(--ink); min-width: 0; }
  .meta-v .ico { width: 15px; height: 15px; color: var(--green); flex: none; }
  .meta-v .mono { font-family: var(--font-mono); font-size: .88em; }
  .hintcmd { font-family: var(--font-mono); font-size: .76rem;
             color: var(--faint); background: var(--bg);
             border: 1px solid var(--line); border-radius: 6px;
             padding: .05rem .35rem;
             /* On a machine with no credential store the hint is an environment
                variable name (~60 characters), not a short command. */
             overflow-wrap: anywhere; }

  .card-go { margin: auto 0 0; padding-top: 1.1rem; text-align: right;
             position: relative; z-index: 1; }
  .btn.outline {
    display: inline-flex; align-items: center; gap: .45rem;
    background: var(--surface); color: var(--green);
    border: 1px solid var(--tint-line); padding: .6rem 1.1rem;
  }
  .btn.outline:hover { background: var(--tint); color: var(--green-dark); }

  /* -------------------------------------------------------------- site map */
  .card-map { position: relative; overflow: hidden;
              border-left: 1px solid var(--line-soft); background: var(--sunken); }
  .card-map .map { position: absolute; inset: 0; width: 100%; height: 100%; }
  .map .pulse { transform-origin: center; animation: pulse 3.2s ease-out infinite; }
  @keyframes pulse {
    0%   { transform: scale(.7); opacity: .45; }
    70%  { transform: scale(2.1); opacity: 0; }
    100% { transform: scale(2.1); opacity: 0; }
  }
  @media (prefers-reduced-motion: reduce) { .map .pulse { animation: none; } }

  .map-tag {
    position: absolute; top: .8rem; left: .8rem;
    font-size: .64rem; font-weight: 700; letter-spacing: .11em;
    text-transform: uppercase; color: var(--muted);
    background: rgba(255, 255, 255, .88); border-radius: 6px;
    padding: .2rem .45rem;
  }
  .map-coords {
    position: absolute; left: .8rem; right: .8rem; bottom: .8rem;
    display: flex; align-items: center; justify-content: center;
    flex-wrap: wrap; gap: .2rem .4rem; text-align: center;
    background: rgba(255, 255, 255, .93); border-radius: var(--radius-sm);
    padding: .48rem .6rem; font-size: .8rem; color: var(--muted);
  }
  .map-coords .mono { font-family: var(--font-mono); }
  .map-coords .ico { width: 13px; height: 13px; color: var(--green); flex: none; }
  /* The unknown state carries the command that fixes it, so it needs a second
     line on a 300px pane rather than a smaller, unreadable one. */
  .map-coords .hintcmd { background: none; border: 0; padding: 0;
                         flex-basis: 100%; font-size: .72rem; }

  /* Visually hidden, still read aloud -- the chart's text equivalent (§11). */
  .vh { position: absolute; width: 1px; height: 1px; overflow: hidden;
        clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; }
  /* ------------------------------------------------------------ empty state */
  .empty-state { text-align: center; padding: 2.6rem 1.2rem;
    background: var(--surface); border: 1px dashed var(--tint-line);
    border-radius: var(--radius); margin: 1.5rem 0; }
  .empty-state h2 { margin: 0 0 .3rem; }
  .empty-state p { margin: 0 auto; color: var(--muted); max-width: 44ch; }

  /* =========================================================== create panel */
  body.locked { overflow: hidden; }
  .drawer { position: fixed; inset: 0; z-index: 40; display: flex;
            justify-content: flex-end; }
  /* A class selector outranks the user agent's `[hidden] { display: none }`, so
     without this the panel is open on every page load and the close button only
     appears to work. Needed on any element given a `display` and toggled with
     the attribute. */
  .drawer[hidden] { display: none; }
  .scrim { position: absolute; inset: 0; background: rgba(19, 37, 27, .38);
           animation: fade .2s ease both; }
  @keyframes fade { from { opacity: 0; } }

  .panel {
    position: relative; width: min(560px, 100%); height: 100%;
    display: flex; flex-direction: column;
    background: var(--surface); border-left: 1px solid var(--line);
    box-shadow: -24px 0 60px -30px rgba(19, 37, 27, .45);
    animation: slide .28s cubic-bezier(.2, .7, .3, 1) both;
  }
  @keyframes slide { from { transform: translateX(24px); opacity: 0; } }
  @media (prefers-reduced-motion: reduce) {
    .scrim, .panel { animation: none; }
  }

  .panel-head { padding: 1.5rem 1.7rem 0; border-bottom: 1px solid var(--line); }
  .panel-head-row { display: flex; align-items: flex-start; gap: 1rem; }
  .panel-head .eyebrow { margin-bottom: .3rem; }
  .panel-head h2 { font-family: var(--font-display); font-weight: 700;
                   font-size: 28px; margin: 0; color: var(--ink);
                   letter-spacing: -.025em; }
  .panel-x { margin-left: auto; background: none; border: 1px solid var(--line);
             color: var(--muted); font-size: 1.3rem; line-height: 1;
             width: 34px; height: 34px; padding: 0; border-radius: var(--radius-sm);
             display: grid; place-items: center; flex: none; }
  .panel-x:hover { background: var(--bg); color: var(--ink); }
  .panel-lede { margin: .5rem 0 1.1rem; color: var(--muted); font-size: .94rem; }

  .steps { display: flex; list-style: none; margin: 0; padding: 0; gap: 0; }
  .steps li {
    flex: 1; display: flex; align-items: center; gap: .45rem;
    padding: .55rem 0 .7rem; font-size: .82rem; color: var(--faint);
    border-bottom: 2px solid var(--line);
  }
  .steps li span {
    display: grid; place-items: center; width: 19px; height: 19px; flex: none;
    border-radius: 50%; background: var(--bg); color: var(--faint);
    font-size: .68rem; font-weight: 700;
  }
  .steps li.on { color: var(--green); border-bottom-color: var(--green); }
  .steps li.on span { background: var(--green); color: #fff; }

  .panel form { display: flex; flex-direction: column; min-height: 0; flex: 1; }
  .panel-body { flex: 1; overflow-y: auto; padding: 1.4rem 1.7rem 1.8rem; }
  .fset { margin-bottom: 2rem; }
  .fset:last-child { margin-bottom: 0; }
  .fset h3 { display: flex; align-items: center; gap: .55rem; margin: 0 0 1rem;
             font-family: var(--font-display); font-weight: 700; font-size: 19px;
             color: var(--ink); letter-spacing: -.02em; }
  .fnum { display: grid; place-items: center; width: 22px; height: 22px;
          flex: none; border-radius: 50%; background: var(--tint);
          color: var(--green); font-size: .75rem; font-weight: 700; }

  .panel label { display: grid; gap: .35rem; margin-bottom: .9rem;
                 font-size: 13.5px; font-weight: 600; color: var(--ink); }
  .panel input { height: 46px; padding: 0 .8rem; border-radius: var(--radius);
                 font-weight: 400; font-size: .97rem; }
  .panel input:focus-visible {
    outline: none; border: 1.5px solid var(--green);
    box-shadow: 0 0 0 4px #e1ede5;
  }
  .panel .pair { display: grid; grid-template-columns: 1fr 1fr; gap: .9rem; }
  .panel .hint { font-size: .86rem; color: var(--muted); margin: -.2rem 0 0;
                 font-weight: 400; line-height: 1.55; max-width: none; }
  .panel .callout { margin: .2rem 0 .9rem !important; }

  .infocard { background: var(--sunken); border: 1px solid var(--line-soft);
              border-radius: var(--radius-panel); padding: 1rem 1.1rem; }
  .infocard p { margin: 0 0 .7rem; font-size: .9rem; color: var(--muted);
                line-height: 1.6; }
  .infocard p:last-child { margin-bottom: 0; }
  .infocard strong { color: var(--ink); }

  .panel-foot { flex: none; padding: 1rem 1.7rem 1.2rem;
                border-top: 1px solid var(--line); background: var(--surface); }
  .panel-acts { display: flex; justify-content: flex-end; gap: .7rem; }
  button.ghost { background: none; color: var(--muted); border-color: transparent; }
  button.ghost:hover { background: var(--bg); color: var(--ink); }
  #create-status { font-size: .9rem; margin: 0 0 .7rem; min-height: 0; }
  #create-status:empty { display: none; }
  #create-status.err { color: var(--err-ink); }
  #create-status.ok { color: var(--green); font-weight: 600; }
  /* --------------------------------------------------------- archived / CLI */
  details.archived { margin: 1.5rem 0; }
  details.archived > summary { cursor: pointer; color: var(--green-dark);
    font-weight: 600; font-size: .95rem; }
  details.archived ul.projects { margin-top: 1rem; }

  /* ============================================= browser-or-terminal panel */
  .cli {
    display: grid; grid-template-columns: 1fr 1fr; gap: 2.4rem;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius-card); padding: 2rem 2.1rem;
    margin: 0 0 1.5rem; font-size: inherit; color: inherit;
  }
  .cli h2 { display: flex; align-items: center; gap: .7rem; font-size: 24px;
            margin: 0 0 1rem; color: var(--ink); letter-spacing: -.02em; }
  .cli-ico { display: grid; place-items: center; width: 34px; height: 34px;
             flex: none; border-radius: var(--radius-sm);
             background: var(--tint); color: var(--green); }
  .cli-ico .ico { width: 19px; height: 19px; }
  .cli p { color: var(--muted); max-width: 56ch; margin: 0 0 1.2rem; }

  .callouts { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem;
              margin-bottom: 1.4rem; }
  .callout { margin: 0 !important; border-left-width: 1px !important;
             padding: .8rem .9rem; border-radius: var(--radius-sm); }
  .callout-h { display: flex; align-items: center; gap: .45rem;
               margin: 0 0 .3rem !important; font-weight: 600;
               color: var(--warn-ink) !important; font-size: .92rem; }
  .callout-h .ico { width: 16px; height: 16px; flex: none; }
  .callout-b { margin: 0 !important; font-size: .86rem; line-height: 1.5;
               color: var(--warn-body) !important; }
  .callout-b code { background: rgba(255, 255, 255, .5); border-radius: 4px;
                    padding: 0 .2rem; }

  /* ---------------------------------------------------------- action table */
  .actions { width: 100%; border-collapse: collapse; font-size: .9rem; }
  .actions th, .actions td { padding: .6rem .5rem; text-align: left;
                             border-bottom: 1px solid var(--hairline); }
  .actions thead th {
    font-size: .68rem; font-weight: 700; letter-spacing: .09em;
    text-transform: uppercase; color: var(--faint);
    background: var(--sunken); border-bottom: 1px solid var(--line-soft);
  }
  .actions thead th:first-child { border-top-left-radius: var(--radius-sm);
                                  padding-left: .8rem; }
  .actions thead th:last-child { border-top-right-radius: var(--radius-sm); }
  .actions th[scope="row"] { font-weight: 500; color: var(--ink);
                             padding-left: .8rem; }
  .actions th[scope="row"] .hintcmd { margin-left: .5rem; }
  .actions td { text-align: center; width: 7.5rem; }
  .actions td .ico { width: 17px; height: 17px; color: var(--green); }
  /* --dash is a BORDER colour (1.7:1 on white) and was invisible as a glyph.
     The dash carries "not available in the browser" beside it for a screen
     reader, but a sighted reader gets only the dash, so it has to be legible. */
  .actions .no { color: var(--faint); }
  .tag-safer {
    display: inline-block; font-size: .64rem; font-weight: 700;
    letter-spacing: .08em; text-transform: uppercase; color: #fff;
    background: var(--green); border-radius: 999px; padding: .2rem .55rem;
  }

  /* -------------------------------------------------------- terminal block */
  .term { background: var(--term-bg); border-radius: var(--radius-panel);
          padding: 1rem 0 1.3rem; height: 100%; display: flex;
          flex-direction: column; }
  .term-bar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; padding: 0 1.1rem .8rem;
    border-bottom: 1px solid rgba(214, 232, 220, .12);
    font-size: .68rem; font-weight: 700; letter-spacing: .1em;
    text-transform: uppercase; color: var(--term-prompt);
  }
  .term-bar > span:first-child { display: inline-flex; align-items: center;
                                 gap: .45rem; }
  .term-bar .ico { width: 15px; height: 15px; }
  .term-ctx { color: rgba(214, 232, 220, .45); font-weight: 500;
              letter-spacing: .04em; text-transform: none; }

  .term-lines { list-style: none; margin: 0; padding: .9rem 1.1rem 0; }
  .term-lines li { padding: .42rem 0; }
  .tl-cmd { display: flex; align-items: center; gap: .5rem; }
  .tl-cmd code { font-family: var(--font-mono); font-size: .88rem;
                 color: var(--term-ink); }
  .tl-p { font-family: var(--font-mono); color: var(--term-prompt); flex: none; }
  .tl-cmd .copy { margin-left: auto; color: var(--term-ink); }
  .tl-note { display: block; padding-left: 1.1rem; font-family: var(--font-mono);
             font-size: .78rem; color: rgba(214, 232, 220, .42); }

  .term-cursor { display: flex; align-items: center; gap: .5rem;
                 margin: .5rem 0 0 !important; padding: 0 1.1rem; }
  .caret { display: inline-block; width: .5rem; height: 1.05rem;
           background: var(--term-prompt); animation: blink 1.1s steps(1) infinite; }
  @keyframes blink { 50% { opacity: 0; } }
  @media (prefers-reduced-motion: reduce) { .caret { animation: none; } }
  /* ============================================================= responsive */
  /* One breakpoint, at the width where the card's two panes genuinely stop
     fitting side by side rather than at a device name. Everything above it is
     already fluid — the gutters, the title and the hero all scale by `clamp`,
     so this block only carries the changes that are structural. */
  @media (max-width: 860px) {
    /* The posture pill wraps below the brand here, so the bar cannot keep a
       fixed 64px. */
    header.app { height: auto; min-height: 64px; padding: .7rem 0; }
    header.app nav.whose { margin-left: 0; }

    .hero { display: block; padding: 2.4rem 0 1.8rem; }
    .hero-act { padding: 1.4rem 0 0; }
    .btn-new { width: 100%; justify-content: center; }
    .lede { max-width: none; }

    /* 2x2, and the note drops to its own line under them. */
    .isolation { display: grid; grid-template-columns: 1fr 1fr; gap: .6rem; }
    .isolation .isolation-note { grid-column: 1 / -1; padding-left: 0; }

    .grid { grid-template-columns: 1fr; gap: 16px; }
    .grid > .card, .grid > .tile { grid-column: 1 / -1; }

    /* The map becomes a header strip. Source order still puts it last, so it is
       moved with `order` rather than by rebuilding the markup — a screen reader
       keeps reading the name and the count before the decorative locator. */
    .card { display: flex; flex-direction: column; }
    .card-map { order: -1; height: 168px; border-left: 0;
                border-bottom: 1px solid var(--line-soft); }
    .card-main { padding: 1.2rem 1.2rem 1.1rem; }
    .card h2 { font-size: 26px; }
    /* The pill goes to its own line rather than squeezing the name: `auto`
       margins in a wrapping flex row push it off on its own anyway, and a
       half-wrapped name is worse than a pill on line two. */
    .card-head .pill { margin-left: 0; }
    .stat .count .n { font-size: 46px; }
    .stat .count .rate { margin-left: 0; flex-basis: 100%; }

    /* Two ISO dates and a caption do not fit across 300px without all three
       wrapping. The dates are the data and stay; the caption repeats what the
       count above already says. */
    .chart-x > span:nth-child(2) { display: none; }
    .chart-x { font-size: .7rem; }

    /* Label and value stay on ONE line, the label no longer a fixed column. */
    .meta { grid-template-columns: auto 1fr; gap: .6rem; }

    .card-go { text-align: stretch; }
    .btn.outline { width: 100%; justify-content: center; min-height: 44px; }

    .cli { grid-template-columns: 1fr; gap: 1.6rem; padding: 1.5rem 1.3rem; }
    .callouts { grid-template-columns: 1fr; }

    /* The action table collapses to what it actually says on a phone: one row
       is available in both places, the other three are terminal-only. Keeping a
       three-column grid at this width would need a horizontal scroll to read a
       tick, and the tick is not the information — the command is. */
    .actions thead, .actions td { display: none; }
    .actions, .actions tbody, .actions tr, .actions th { display: block; }
    .actions th[scope="row"] {
      padding: .7rem 0; border-bottom: 1px solid var(--hairline);
      display: flex; flex-wrap: wrap; align-items: center; gap: .5rem;
    }
    .actions th[scope="row"] .hintcmd { margin-left: 0; }
    .actions th[scope="row"]::after {
      content: "terminal only"; order: 3;
      font-size: .64rem; font-weight: 700; letter-spacing: .08em;
      text-transform: uppercase; color: var(--faint);
      background: var(--bg); border: 1px solid var(--line);
      border-radius: 999px; padding: .18rem .55rem;
    }
    .actions tr:first-child th[scope="row"]::after { content: "browser or terminal"; }

    /* Full height, full width: a 560px drawer on a 390px screen is the page. */
    .panel { width: 100%; border-left: 0; }
    .panel-head, .panel-body, .panel-foot { padding-left: 1.2rem;
                                            padding-right: 1.2rem; }
    .panel .pair { grid-template-columns: 1fr; }
    .panel-acts button { flex: 1; min-height: 44px; }
    .steps li { font-size: .76rem; }

    /* Hit targets. The copy controls are 14px icons in a .15rem pad on desktop,
       which is fine for a mouse and not for a thumb. */
    .copy { padding: .55rem; }
    .panel-x { width: 44px; height: 44px; }
  }
"""


def _shell(body: str) -> str:
    # The posture pill rides in the header's own right-hand slot rather than in
    # a second bar, so every page keeps one top bar in one place.
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>TrapTracker Reporting Extension &mdash; projects</title>"
            f"<style>{_STYLE}</style></head>\n<body>"
            f'<div class="wrap">{_theme.header(banner_html=_posture_html())}'
            f'{body}</div>{_SCRIPT}</body></html>')


#: Page behaviour that is not the create form's. Deliberately small and
#: dependency-free — there is no framework and no build step here.
#:
#: `navigator.clipboard` is only exposed in a SECURE context, and this page is
#: plain HTTP by design, so on most browsers it is simply absent. That is the
#: ordinary path rather than a fallback, so the selection route is written first
#: and the clipboard is the enhancement.
_SCRIPT = """
<script>
(function () {
  function flash(btn, ok) {
    btn.classList.toggle("done", ok);
    btn.setAttribute("aria-label", ok ? "Copied" : "Copy failed");
    setTimeout(function () { btn.classList.remove("done"); }, 1400);
  }
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest(".copy") : null;
    if (!btn) return;
    ev.preventDefault();
    var text = btn.getAttribute("data-copy") || "";
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { flash(btn, true); },
                                               function () { flash(btn, false); });
      return;
    }
    // No clipboard on plain HTTP: put the value in a field and select it, so a
    // ctrl-C still works and the user can see exactly what they are copying.
    var f = document.createElement("textarea");
    f.value = text; f.setAttribute("readonly", "");
    f.style.position = "fixed"; f.style.opacity = "0";
    document.body.appendChild(f); f.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(f);
    flash(btn, ok);
  });
})();
</script>
"""


#: Shown whether or not any project exists: the picker is read-only by design,
#: and saying why is better than leaving the absence to be discovered.
#: The session-token paragraph, unchanged from the note this panel replaced. It
#: is the sentence that justifies there being a creation form on this page at
#: all, so it is quoted rather than re-argued.
_SESSION_PARA = (
    'Creating a project here is possible because this server is behind a '
    'session token: the endpoints are not open to anything else on the '
    'machine. Two things that does <em>not</em> change:')

#: The two caveats, pulled out of that paragraph's second half so each can carry
#: an icon and a heading (§11: status is never colour alone). The wording is the
#: note's own.
_CAVEATS = (
    ("warning", "Plain HTTP",
     "The session token is unprotected in transit."),
    ("lock", "Browser passwords",
     "A password typed into a browser is still within reach of autofill and "
     "history in a way <code>getpass</code> at a terminal is not."),
)

#: Who can do what. Taken from the CLI's own command set — `create`, `set-site`,
#: `set-password`, `archive` in `cli_project.py` — and from the fact that the
#: web layer exposes exactly one of them. If a command is added to the CLI, or
#: the web layer grows a second endpoint, this table is the thing that has to
#: change with it.
_ACTIONS = (
    ("Create a project", True, "ttr project create", "safer"),
    ("Change a site", False, "ttr project set-site", None),
    ("Rotate a credential", False, "ttr project set-password", None),
    ("Archive a project", False, "ttr project archive", None),
)

#: The terminal block's lines. `create` carries the reason it is the safer one.
_TERMINAL_LINES = (
    ("ttr project create", "asks for the password with getpass"),
    ("ttr project import", None),
    ("ttr project list", None),
    ("ttr project --help", None),
)


def _action_table_html() -> str:
    """Action / browser / terminal. A tick is never the only signal: the browser
    column says "no" in a dash with a screen-reader word behind it, and the
    terminal column names the command."""
    rows = []
    for action, in_browser, command, note in _ACTIONS:
        if in_browser:
            browser = f'{_icon("check", label="yes")}'
        else:
            browser = ('<span class="no" aria-hidden="true">&mdash;</span>'
                       '<span class="vh">not available in the browser</span>')
        tag = (f'<span class="tag-safer">{_esc(note)}</span>' if note else
               f'{_icon("check", label="yes")}')
        rows.append(
            f'<tr><th scope="row">{_esc(action)}'
            f'<code class="hintcmd">{_esc(command)}</code></th>'
            f'<td>{browser}</td><td>{tag}</td></tr>')
    return (
        '<table class="actions"><thead><tr><th scope="col">Action</th>'
        '<th scope="col">Browser</th><th scope="col">Terminal</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>')


def _terminal_html() -> str:
    """The dark command block. Every line is copyable; the cursor does not blink
    under `prefers-reduced-motion`."""
    lines = []
    for command, note in _TERMINAL_LINES:
        comment = (f'<span class="tl-note"># {_esc(note)}</span>'
                   if note else "")
        lines.append(
            f'<li><span class="tl-cmd"><span class="tl-p" aria-hidden="true">$</span>'
            f'<code>{_esc(command)}</code>'
            f'{_copy_button(command, _esc(command))}</span>{comment}</li>')
    return (
        '<div class="term">'
        f'<div class="term-bar"><span>{_icon("terminal")}Terminal</span>'
        '<span class="term-ctx">ttr project</span></div>'
        f'<ul class="term-lines">{"".join(lines)}</ul>'
        '<p class="term-cursor" aria-hidden="true"><span class="tl-p">$</span>'
        '<span class="caret"></span></p>'
        '</div>')


def _cli_panel_html() -> str:
    """Browser or terminal? Shown whether or not any project exists: it explains
    how this server is reached and why one route is safer, and neither depends on
    a project already existing."""
    caveats = "".join(
        f'<div class="warn callout"><p class="callout-h">{_icon(ico)}'
        f'{_esc(head)}</p><p class="callout-b">{body}</p></div>'
        for ico, head, body in _CAVEATS)
    return (
        '<section class="cli reveal">'
        '<div class="cli-say">'
        f'<h2><span class="cli-ico">{_icon("shield")}</span>'
        f'Browser or terminal?</h2>'
        f'<p>{_SESSION_PARA}</p>'
        f'<div class="callouts">{caveats}</div>'
        f'{_action_table_html()}'
        '</div>'
        f'<div class="cli-term">{_terminal_html()}</div>'
        '</section>')
#: The creation panel. Present only because `ttr serve` is now behind a session
#: token - before that, this POSTed a mailbox app password to an endpoint any
#: local process could reach.
#:
#: The password field is `type="password"` and the request is a POST with a JSON
#: BODY. That is not cosmetic: uvicorn's access log records the full request line
#: including the query string and does NOT record bodies, so a password in a
#: query string would be written to the log in clear. Never build this as a GET.
#:
#: THE FIELDS ARE THE ENDPOINT'S, not the design's. `POST /api/projects` takes
#: name, email, password, site_name, latitude and longitude — six — so six is
#: what this asks for. The design's mockup also drew a mailbox "server" field;
#: there is none, because `create_project` derives the IMAP host from the address
#: and refuses anything that is not Gmail. A field the endpoint would ignore is a
#: question the user cannot answer wrongly, which is worse than not asking it.
_CREATE_FORM = """
<div class="drawer" id="create" hidden>
  <div class="scrim" data-close-create></div>
  <aside class="panel" role="dialog" aria-modal="true"
         aria-labelledby="create-title">
    <header class="panel-head">
      <div class="panel-head-row">
        <div>
          <p class="eyebrow">New project</p>
          <h2 id="create-title">Create a project</h2>
        </div>
        <button type="button" class="panel-x" data-close-create
                aria-label="Close">&times;</button>
      </div>
      <p class="panel-lede">Everything below is written into one project and
        stays there.</p>
      <ol class="steps" aria-hidden="true">
        <li class="on"><span>1</span>Site</li>
        <li><span>2</span>Mailbox</li>
        <li><span>3</span>Species</li>
      </ol>
    </header>

    <form id="create-form" autocomplete="off">
      <div class="panel-body">
        <section class="fset" id="step-1">
          <h3><span class="fnum">1</span>Site</h3>
          <label>Name <input name="name" required placeholder="North Field Camera"></label>
          <label>Site name <input name="site_name" placeholder="optional"></label>
          <div class="pair">
            <label>Latitude <input name="latitude" inputmode="decimal"
                   placeholder="optional"></label>
            <label>Longitude <input name="longitude" inputmode="decimal"
                   placeholder="optional"></label>
          </div>
          <p class="hint">Coordinates are needed for weather correlation, and are
            set together or not at all.</p>
        </section>

        <section class="fset" id="step-2">
          <h3><span class="fnum">2</span>Mailbox</h3>
          <label>Alert inbox <input name="email" type="email" required
            placeholder="alerts@gmail.com" autocomplete="off"></label>
          <label>App password
            <input name="password" type="password" autocomplete="new-password"
                   placeholder="16 characters, spaces optional"></label>
          <div class="warn callout">
            <p class="callout-h">&#9888; A password typed here</p>
            <p class="callout-b">is within reach of autofill and browser history,
              and this page is plain HTTP. <code>ttr project create</code> asks
              for it with <code>getpass</code> instead, which is why it is the
              safer route.</p>
          </div>
          <p class="hint">Gmail only. The password is verified against the mailbox
            before anything is written, then stored in your operating system's
            credential store &mdash; never in a file, and never in this page.
            Leave it empty to create the project unverified and run
            <code>ttr project set-password</code> later. On a machine with no
            credential store, such as a container, leave it empty and set
            <code>TTR_IMAP_PASSWORD__&lt;ID&gt;</code> in the server's environment.</p>
        </section>

        <section class="fset" id="step-3">
          <h3><span class="fnum">3</span>Species</h3>
          <div class="infocard">
            <p>Nothing to set here, and nothing is copied from another project.
              A new project starts on the <strong>bundled example</strong> alias
              table &mdash; illustrative, not your camera's class list, so it
              resolves species you may not detect and misses ones you do.</p>
            <p>Drop your own table at the project's species path to replace it.
              The card on this page names which table each project is actually
              resolving against.</p>
          </div>
        </section>
      </div>

      <footer class="panel-foot">
        <p id="create-status" role="status"></p>
        <div class="panel-acts">
          <button type="button" class="ghost" data-close-create>Cancel</button>
          <button type="submit">Create project</button>
        </div>
      </footer>
    </form>
  </aside>
</div>
<script>
(function () {
  var drawer = document.getElementById("create");
  var form = document.getElementById("create-form");
  var status = document.getElementById("create-status");
  if (!drawer || !form) return;

  // --------------------------------------------------------------- open/close
  var lastFocus = null;
  function open() {
    lastFocus = document.activeElement;
    drawer.hidden = false;
    document.body.classList.add("locked");   // the page behind must not scroll
    var first = form.querySelector("input");
    if (first) first.focus();
  }
  function close() {
    drawer.hidden = true;
    document.body.classList.remove("locked");
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  document.addEventListener("click", function (ev) {
    if (!ev.target.closest) return;
    if (ev.target.closest("[data-open-create]")) { ev.preventDefault(); open(); }
    else if (ev.target.closest("[data-close-create]")) { ev.preventDefault(); close(); }
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && !drawer.hidden) close();
  });

  // ------------------------------------------------------------- step marker
  // Which of the three parts is in view. Reflects the scroll rather than
  // driving it: the form is one scroll, not a wizard, and the marker should not
  // imply that anything is gated behind anything else.
  var body = drawer.querySelector(".panel-body");
  var steps = drawer.querySelectorAll(".steps li");
  var sections = drawer.querySelectorAll(".fset");
  if (body && steps.length === sections.length) {
    body.addEventListener("scroll", function () {
      var edge = body.getBoundingClientRect().top + 80, current = 0;
      sections.forEach(function (s, i) {
        if (s.getBoundingClientRect().top <= edge) current = i;
      });
      steps.forEach(function (s, i) { s.classList.toggle("on", i === current); });
    }, {passive: true});
  }

  // ------------------------------------------------------------------ submit
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var data = Object.fromEntries(new FormData(form).entries());
    status.textContent = "Validating the address, then connecting to the mailbox…";
    status.className = "";
    fetch("api/projects", {
      method: "POST",                       // never GET: the body stays out of logs
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data)
    }).then(function (r) { return r.json().then(function (b) { return [r.ok, b]; }); })
      .then(function (pair) {
        if (!pair[0]) { status.className = "err"; status.textContent = pair[1].error; return; }
        status.className = "ok";
        status.textContent = pair[1].message;
        form.reset();
        setTimeout(function () { window.location.reload(); }, 1200);
      })
      .catch(function (e) { status.className = "err"; status.textContent = String(e); });
  });
})();
</script>
"""
def render(registry: Registry, root: Optional[Path] = None) -> str:
    """The picker page for a loaded registry.

    One page for every state — no projects, one project, several, all archived.
    The hero, the isolation strip and the browser-or-terminal panel are
    unconditional: they say what a project IS and how this server is reached,
    and neither of those depends on whether any project exists yet (§12).
    """
    visible = [e for e in registry.entries if not e.archived]
    archived = [e for e in registry.entries if e.archived]

    body = [_hero_html(visible, archived),
            _isolation_strip_html()]

    # The grid always renders: with no projects it holds the create tile alone,
    # which is the empty state rather than a separate one.
    cards = "".join(_card_html(card_for(e, registry, root)) for e in visible)
    body.append(f'<section class="grid reveal">{cards}{_create_tile_html()}</section>')

    if archived:
        hidden = "".join(_card_html(card_for(e, registry, root)) for e in archived)
        body.append(
            f'<details class="archived"><summary>Show {len(archived)} archived project'
            f"{'s' if len(archived) != 1 else ''}</summary>"
            f'<p class="note">Archived projects are hidden from this list and '
            f'refuse to open. Their data is untouched; '
            f'<code>ttr project unarchive</code> brings one back.</p>'
            f'<section class="grid">{hidden}</section></details>')

    body.append(_cli_panel_html())
    body.append(_CREATE_FORM)
    return _shell("".join(body))


#: Placeholder the in-project pages carry, replaced with `banner()` when the
#: route renders them. A token rather than a format field because those pages are
#: mostly CSS, and `str.format` would choke on every brace in it.
PROJECT_SLOT = "<!--PROJECT-->"


def banner(name: str, project_id: str) -> str:
    """The "whose data is this" line for a page INSIDE a project.

    The CLI prints a provenance banner for the same reason: a report on screen
    looks identical whichever project produced it, and the report-cache leak
    found in Stage 6 was precisely that failure — the wrong project's content
    under a URL that looked right. Knowing at a glance is the cheap defence.
    """
    return (f'<span class="projchip"><span class="pill-dot" aria-hidden="true"></span>'
            f'<strong>{_html.escape(name)}</strong> '
            f'<code>{_html.escape(project_id[:8])}</code></span> '
            f'<a class="allproj" href="/">All projects</a>')
