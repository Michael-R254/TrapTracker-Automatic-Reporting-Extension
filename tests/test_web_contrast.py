"""Text contrast, in two layers.

The projects redesign asked for 4.5:1 on body text and label/value pairs, and
the palette it shipped with did not meet its own bar: the "ink muted" it
specified measured 4.01:1 on the page background. That was found by measuring,
not by looking, and the point of this file is that the next page does not have
to remember to measure.

**Layer 1 — the palette contract** (`test_every_text_token_*`). Pure Python over
``theme.TOKENS``: every foreground token is checked against every surface it is
used on. Always runs, needs nothing installed, and catches the common case,
which is someone adjusting a hex value.

**Layer 2 — the rendered page** (`test_the_rendered_page_*`). Drives the real
page in headless Chrome and resolves the ACTUAL cascade — computed colour
against the nearest painted ancestor background — for every text-bearing
element. This is the layer that caught the dash in the action table, which
Layer 1 could not: the bug was not a bad token, it was a border token used as a
text colour. Skips when no browser is installed, in the same way the PDF tests
do, so CI without Chrome stays green.

Both layers use the WCAG 2.1 relative-luminance formula.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ttr.web import theme

# --------------------------------------------------------------------------- #
# WCAG 2.1 relative luminance and contrast
# --------------------------------------------------------------------------- #
def _channel(value: float) -> float:
    value /= 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _tokens() -> dict[str, str]:
    """Every ``--name: #hex`` declared in the shared token block."""
    return dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b", theme.TOKENS))


# --------------------------------------------------------------------------- #
# Layer 1 — the palette contract
# --------------------------------------------------------------------------- #
#: Foreground token -> the surfaces it is painted on, as token names. Kept
#: explicit rather than derived: the pairs that MATTER are the ones the design
#: actually uses, and a cross-product would test combinations no page renders
#: while saying nothing about the ones it does.
_TEXT_ON = {
    "--ink":      ("--bg", "--surface", "--sunken"),
    "--muted":    ("--bg", "--surface", "--sunken"),
    "--faint":    ("--bg", "--surface", "--sunken"),
    "--green":    ("--bg", "--surface", "--sunken", "--tint"),
    "--warn-ink":  ("--warn-bg",),
    "--warn-body": ("--warn-bg",),
    "--err-ink":   ("--surface",),
    "--term-ink":    ("--term-bg",),
    "--term-prompt": ("--term-bg",),
}

#: Tokens that describe a LINE, not a letter. The action table's "not available
#: in the browser" dash was painted in `--dash` and measured 1.71:1 — legible as
#: a border, invisible as a glyph. Naming them here is what makes
#: `test_a_border_token_is_never_used_as_a_text_colour` able to say so.
_BORDER_ONLY = ("--line", "--line-soft", "--hairline", "--dash", "--tint-line")


@pytest.mark.parametrize("fg,bg", [(f, b) for f, bgs in _TEXT_ON.items() for b in bgs])
def test_every_text_token_meets_4_5_to_1_on_every_surface_it_is_used_on(fg, bg):
    tokens = _tokens()
    ratio = contrast(tokens[fg], tokens[bg])
    assert ratio >= 4.5, (
        f"{fg} ({tokens[fg]}) on {bg} ({tokens[bg]}) is {ratio:.2f}:1, under 4.5:1")


def test_white_on_the_accent_passes_both_ways():
    """Primary buttons are white on `--green`; the accent is also used as text
    on white. Both directions have to hold or one of the two is unreadable."""
    tokens = _tokens()
    assert contrast("#ffffff", tokens["--green"]) >= 4.5
    assert contrast(tokens["--green"], tokens["--surface"]) >= 4.5


def test_a_border_token_is_never_used_as_a_text_colour():
    """Regression, and the reason Layer 2 exists.

    `--dash` is a 1.7:1 hairline colour. Used for a border it is correct; used
    for the `—` in the action table it produced a glyph nobody could see. This
    greps the stylesheets for a border token in a `color:` position.
    """
    from ttr.web import picker

    offences = []
    from ttr.web.ingest_page import INGEST_PAGE

    sheets = (("theme.BASE", theme.BASE), ("picker._STYLE", picker._STYLE),
              ("ingest_page", INGEST_PAGE))
    for name, sheet in sheets:
        for token in _BORDER_ONLY:
            for match in re.finditer(rf"(?<!-)\bcolor:\s*var\({token}\)", sheet):
                line = sheet[:match.start()].count("\n") + 1
                offences.append(f"{name}:{line} uses {token} as a text colour")
    assert not offences, "; ".join(offences)


# --------------------------------------------------------------------------- #
# Layer 2 — the rendered page, through the real cascade
# --------------------------------------------------------------------------- #
#: Injected into the rendered page. Walks every text-bearing element, resolves
#: the computed colour against the nearest ancestor that actually paints a
#: background, and writes the failures into the DOM where `--dump-dom` will
#: bring them back. Large text takes the 3:1 threshold, per WCAG.
_AUDIT = """
<script>
(function () {
  function lum(c) {
    var p = c.match(/\\d+(\\.\\d+)?/g).map(Number).slice(0, 3).map(function (v) {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2];
  }
  function bgOf(el) {
    for (var n = el; n && n !== document.documentElement; n = n.parentElement) {
      var b = getComputedStyle(n).backgroundColor;
      if (b && !/rgba\\(0, 0, 0, 0\\)|transparent/.test(b)) return b;
    }
    return "rgb(255, 255, 255)";
  }
  function ratio(a, b) {
    var la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }
  var bad = [], checked = 0;
  var sel = "p,span,td,th,dt,dd,code,h1,h2,h3,a,li,figcaption,button,label";
  document.querySelectorAll(sel).forEach(function (el) {
    var own = Array.prototype.filter.call(el.childNodes, function (n) {
      return n.nodeType === 3 && n.textContent.trim();
    }).map(function (n) { return n.textContent.trim(); }).join(" ");
    if (!own) return;
    var cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none") return;
    if (el.closest(".vh")) return;                 // visually hidden on purpose
    checked++;
    var px = parseFloat(cs.fontSize);
    var large = px >= 24 || (px >= 18.66 && parseInt(cs.fontWeight, 10) >= 700);
    var need = large ? 3 : 4.5;
    var r = ratio(cs.color, bgOf(el));
    if (r < need) bad.push({text: own.slice(0, 48), px: px, ratio: +r.toFixed(2),
                            need: need, cls: String(el.className)});
  });
  var out = document.createElement("div");
  out.id = "contrast-audit";
  out.textContent = JSON.stringify({checked: checked, failures: bad});
  document.body.appendChild(out);
})();
</script>
"""


def _headless_audit(html: str) -> dict:
    """Render `html` in headless Chrome with the audit injected, return its JSON."""
    from ttr.web.browser import BrowserNotFound, find_browser

    try:
        executable = find_browser()
    except BrowserNotFound as exc:
        pytest.skip(f"no Chrome/Edge for the rendered-page audit: {exc}")

    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(html.replace("</body>", _AUDIT + "</body>"), encoding="utf-8")
        proc = subprocess.run(
            [str(executable), "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={Path(tmp) / 'profile'}",
             "--virtual-time-budget=4000", "--dump-dom", page.as_uri()],
            # UTF-8 EXPLICITLY. Chrome dumps the DOM as UTF-8; `text=True` alone
            # decodes with the locale codec, which is cp1252 on this machine and
            # throws on the first byte outside it — silently leaving stdout None
            # and turning a passing audit into a TypeError. The first two pages
            # happened to stay inside cp1252; the ingest page does not.
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)

    found = re.search(r'<div id="contrast-audit">(.*?)</div>', proc.stdout, re.S)
    if not found:
        pytest.skip("headless render produced no audit node; browser may be blocked")
    import html as _h
    return json.loads(_h.unescape(found.group(1)))


@pytest.fixture
def projects_page(tmp_path, monkeypatch) -> str:
    """The PROJECTS page, rendered over projects that exercise its states.

    Deliberately includes a project with events (so the chart, the rate line and
    the meta rows all render) and one with nothing (so the unknown states, which
    are the muted-ink ones, render too). A contrast audit over a page with no
    degraded states would miss exactly the text most at risk of being too light.
    """
    import datetime as dt

    from ttr.projects import paths as ppaths
    from ttr.projects.registry import Registry
    from ttr.projects.service import context_for, create_project, set_site
    from ttr.storage.repository import PersistedEvent
    from ttr.web import picker

    root = tmp_path / "contrast-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))

    full, _ = create_project("Example Site", "a@gmail.com", root=root,
                             skip_connection_test=True)
    set_site(full.id, name="Example Village, UK",
             latitude=51.5000, longitude=-0.1000, root=root)
    create_project("New Deployment", "b@gmail.com", root=root,
                   skip_connection_test=True)

    ctx = context_for(Registry.load(root).get(full.id), root)
    repo = ctx.open_repo()
    try:
        for day in (1, 2, 3, 6, 7):            # 4 and 5 are delivery gaps
            for i in range(4):
                repo.upsert_event(PersistedEvent(
                    source_type="email",
                    source_message_id=f"<c-{day}-{i}@x>",
                    ingested_at_utc=dt.datetime(2026, 7, 9, 12, 0,
                                                tzinfo=dt.timezone.utc),
                    canonical_binomial="Columba palumbus",
                    canonical_is_binomial=True,
                    event_time_utc=dt.datetime(2026, 7, day, 9, 0,
                                               tzinfo=dt.timezone.utc),
                    images_present="both", field_provenance={}))
    finally:
        repo.close()

    return picker.render(Registry.load(root), root)



@pytest.fixture
def reports_page(tmp_path, monkeypatch) -> str:
    """The MONITORING REPORTS page, in its pre-report state.

    Rendered exactly as the route renders it — both substitutions, so the window
    payload is real and the summary strip has something to preview. That matters
    for this audit specifically: the strip, the standing statements and the
    three-step captions are the page's smallest and lightest text, which is
    precisely where a contrast floor gets breached.
    """
    import datetime as dt

    from ttr.projects import paths as ppaths
    from ttr.projects.registry import Registry
    from ttr.projects.service import context_for, create_project, set_site
    from ttr.storage.repository import PersistedEvent
    from ttr.web import app as web_app
    from ttr.web.picker import PROJECT_SLOT, banner

    root = tmp_path / "reports-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    m, _ = create_project("Example Site", "a@gmail.com", root=root,
                          skip_connection_test=True)
    set_site(m.id, name="Example Village, UK",
             latitude=51.5000, longitude=-0.1000, root=root)

    ctx = context_for(Registry.load(root).get(m.id), root)
    repo = ctx.open_repo()
    try:
        for day in (1, 2, 3, 6, 7):
            for i in range(4):
                repo.upsert_event(PersistedEvent(
                    source_type="email",
                    source_message_id=f"<r-{day}-{i}@x>",
                    ingested_at_utc=dt.datetime(2026, 7, 9, 12, 0,
                                                tzinfo=dt.timezone.utc),
                    canonical_binomial="Columba palumbus",
                    canonical_is_binomial=True,
                    event_time_utc=dt.datetime(2026, 7, day, 9, 0,
                                               tzinfo=dt.timezone.utc),
                    images_present="both", field_provenance={}))
    finally:
        repo.close()

    return (web_app._PAGE
            .replace(PROJECT_SLOT, banner(ctx.name, ctx.id))
            .replace(web_app._WINDOW_SLOT, web_app._window_payload(ctx.db_path)))



@pytest.fixture
def ingest_page() -> str:
    """The INGEST page, in its idle state.

    Static markup only: the run states are painted by the client from
    `/api/ingest/state`, which this audit does not drive. What it covers is the
    part that is always on screen — the acts-on-your-data callout, the pipeline
    strip, the run controls and the reads/writes card — which is where this
    page's smallest text lives. The chips and card captions are exercised by
    `test_web_ingest_page.py` at the source level.
    """
    from ttr.web.ingest_page import INGEST_PAGE
    from ttr.web.picker import PROJECT_SLOT, banner

    return INGEST_PAGE.replace(PROJECT_SLOT, banner("Example Site", "a1b2c3d4-0000"))


@pytest.mark.skipif(sys.platform not in ("win32", "darwin", "linux"),
                    reason="browser discovery is platform-specific")
@pytest.mark.parametrize("which", ["projects_page", "reports_page", "ingest_page"])
def test_the_rendered_page_has_no_text_under_its_contrast_floor(which, request):
    """Every visible string on the real page, against the real cascade.

    This is the check that found the invisible dash. It is the one worth having:
    a token can be fine and still be applied to the wrong thing, and only the
    rendered page knows which background a given string actually sits on.
    """
    result = _headless_audit(request.getfixturevalue(which))

    # A smoke guard on the audit itself, not a claim about page content: if the
    # selector or the injection broke, `checked` collapses to single digits and
    # an empty `failures` would look like a pass. Set well clear of the real
    # counts (~40-60) so that ordinary edits do not trip it — it was `> 40`, and
    # hiding one button took the reports page to exactly 40.
    assert result["checked"] > 20, (
        f"{which}: the audit found almost no text ({result['checked']} elements); "
        f"check the selector and the injection")
    assert result["failures"] == [], "\n".join(
        f"{f['ratio']}:1 (needs {f['need']}) — {f['text']!r} [{f['cls']}]"
        for f in result["failures"])
