"""One palette and one set of chrome styles, shared by every page.

**Why this exists, given that two module docstrings said not to build it.**
`picker.py` and `ingest_page.py` both recorded that they duplicate `app.py`'s
palette deliberately, so that editing them could not disturb the report page's
tuned layout and print rules. That reasoning was right about the risk and wrong
about the remedy: the thing worth protecting is `#report` and `@media print`, not
the seven colour variables, and three copies of a palette meant a colour could be
changed on one page and not the others — which is how a "consistent" interface
stops being one.

So the split is by what is actually fragile, rather than by file:

- **Here:** the palette, the type scale, and the chrome every page shares — the
  header bar, cards, buttons, inputs, badges, focus rings.
- **Still in `app.py`, untouched:** every `#report` rule and the whole
  `@media print` block. Those govern what the PDF looks like, they were tuned
  against real output, and nothing in this file targets `#report` or prints.

Each page composes ``BASE + <its own CSS>`` into a single ``<style>``. Page rules
come second, so a page can override a shared rule without fighting specificity.

**Green is theme only.** It signals prominence and nothing else — never a data
verdict, never agree/disagree. That constraint predates this file and survives it:
an agreement flag is rendered in words, and a reader must never be able to infer a
finding from a colour. There is deliberately no red/green success/failure pair in
this palette; problems are amber and say what they are in text.

**No dark mode.** The reports carry the agent's inline SVG charts with fixed
fills, and the print rules assume a light ground. A dark theme would either
invert those charts into unreadability or need a second set of chart colours,
which is a real piece of work rather than a media query — and doing it badly
would damage the one part of this interface that carries findings.
"""

from __future__ import annotations

#: Colours, spacing and type. Every page pulls from here; nothing redefines a
#: colour locally.
TOKENS = """
  :root {
    /* Theme greens. Prominence only, never a verdict — see the module docstring.

       --green stays #1b5e3f against the projects-page design, which asked for
       #1d5b3c. That green is not only a CSS value: the report agent writes it
       into its chart SVG fills as a literal, and `test_report.py` pins it there.
       Moving the token would leave the interface with two greens two points
       apart — invisible to a reader, and exactly the drift this file exists to
       stop. One green, and the design's is within rounding of it. */
    --green: #1b5e3f;
    --green-dark: #0f3325;
    --green-mid: #2a7d55;
    --tint: #e5efe8;
    --tint-line: #c9e0d3;

    /* Neutrals, darkest first. --muted is secondary text (7.8:1 on --bg);
       --faint is the third step down — meta-row labels and captions — and is
       the ONLY one near the floor, so it is checked rather than assumed:
       4.55:1 on --bg, 4.96:1 on --surface, 4.66:1 on --sunken.

       --faint is #63746a where the design said #6b7d72. That value measured
       4.01:1 on the page background, failing the 4.5:1 the same design demands
       of it; this is the same hue and saturation, three points darker, and it
       passes on all three grounds it is used against. */
    --ink: #13251b;
    --muted: #3d5146;
    --faint: #63746a;
    --line: #dde6df;
    --line-soft: #e6eee8;
    --hairline: #edf2ee;
    --dash: #b8cbbd;
    --bg: #f3f6f2;
    --surface: #ffffff;
    --sunken: #f5f9f5;

    /* Amber is the ONLY status colour, and it always appears with words. */
    --warn-ink: #5e420c;
    --warn-body: #6f5a30;
    --warn-bg: #fbf5e8;
    --warn-line: #efe0bd;
    --err-ink: #a3232f;

    /* The terminal block. Dark on purpose and only here: it depicts a terminal,
       it is not a dark theme creeping in. Nothing that carries a finding is
       rendered on it. */
    --term-bg: #0f2119;
    --term-ink: #d6e8dc;
    --term-prompt: #7fc79b;

    --radius: 12px;
    --radius-sm: 8px;
    --radius-panel: 16px;
    --radius-card: 22px;
    --shadow: 0 1px 2px rgba(16, 44, 32, .05), 0 4px 12px rgba(16, 44, 32, .05);
    --shadow-lift: 0 2px 4px rgba(16, 44, 32, .07), 0 10px 24px rgba(16, 44, 32, .08);
    --shadow-card: 0 18px 40px -18px rgba(19, 37, 27, .28);

    /* Three roles, system faces. The server is local, may be offline and is
       plain HTTP, so a font CDN is out; the app has no static mount and adding
       one for three families is a build step this page does not justify. */
    --font-display: "Bricolage Grotesque", "Avenir Next", system-ui, sans-serif;
    --font-body: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    --font-mono: ui-monospace, "Cascadia Mono", Consolas, monospace;

    font-family: var(--font-body);
    line-height: 1.6;
    color-scheme: light;
  }
"""

#: Chrome shared by the picker, the report page and the ingest page.
#:
#: The column is fluid rather than a fixed 860px: the old width left two thirds of
#: a desktop window empty while still needing a horizontal scroll on a phone.
#: `clamp` gives a comfortable measure at every size without a media query.
BASE = TOKENS + """
  *, *::before, *::after { box-sizing: border-box; }

  body {
    margin: 0;
    padding: 0 clamp(1rem, 4vw, 2rem) 4rem;
    background: var(--bg);
    color: var(--ink);
    font-size: 1.02rem;
    -webkit-font-smoothing: antialiased;
  }

  .wrap { width: min(72rem, 100%); margin: 0 auto; }
  .wrap.narrow { width: min(56rem, 100%); }

  /* ---------------------------------------------------------------- header */
  /* The same bar on every page, so "which project am I in" is answered in the
     same place each time. The Stage 6 cache leak served one project's report
     under another's URL; a banner that moves around is a banner people stop
     reading. */
  header.app {
    display: flex; flex-wrap: wrap; align-items: center; gap: .5rem 1.1rem;
    padding: .9rem 0 .8rem;
    margin-bottom: 1.6rem;
    border-bottom: 1px solid var(--line);
  }
  header.app .brand {
    display: inline-flex; align-items: baseline; gap: .5rem;
    font-weight: 700; font-size: 1.02rem; color: var(--green-dark);
    letter-spacing: -.01em; text-decoration: none;
  }
  header.app .brand .dot {
    width: .6rem; height: .6rem; border-radius: 50%;
    background: var(--green-mid); flex: none;
  }
  header.app nav.whose {
    display: flex; flex-wrap: wrap; align-items: center; gap: .5rem .9rem;
    font-size: .92rem; color: var(--muted); margin-left: auto;
  }
  header.app nav.whose a { color: var(--green); }

  /* The project chip. Shared chrome, so it lives here rather than on one page:
     "which project is this" is answered in the same shape on the report page,
     the ingest page and anywhere else that renders inside a project. */
  .projchip {
    display: inline-flex; align-items: center; gap: .5rem;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 999px; padding: .3rem .75rem;
  }
  .projchip .pill-dot { width: .4rem; height: .4rem; border-radius: 50%;
                        background: var(--green); flex: none; }
  .projchip strong { color: var(--ink); font-weight: 600; }
  .projchip code { border: 0; background: none; padding: 0; color: var(--faint); }
  .allproj { font-weight: 600; text-decoration: none; white-space: nowrap; }
  .allproj:hover { text-decoration: underline; text-underline-offset: 3px; }
  header.app code, .idtag {
    font-family: var(--font-mono);
    font-size: .78rem; color: var(--muted);
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 5px; padding: .05rem .35rem;
  }

  /* ------------------------------------------------------------ typography */
  h1 { font-family: var(--font-display); font-weight: 700;
       font-size: clamp(1.5rem, 1.2rem + 1.1vw, 2rem); line-height: 1.25;
       color: var(--green-dark); margin: 0 0 .35rem; letter-spacing: -.02em; }
  h2 { font-family: var(--font-display); font-weight: 700; font-size: 1.2rem;
       color: var(--green-dark); margin: 2rem 0 .6rem; letter-spacing: -.02em; }
  p { margin: .55rem 0; }
  a { color: var(--green); }
  a:hover { color: var(--green-dark); }
  .note { color: var(--muted); font-size: .95rem; max-width: 62ch; }
  .unknown { color: var(--muted); font-style: italic; }
  code { font-family: var(--font-mono); font-size: .88em; }

  /* --------------------------------------------------------------- surface */
  .panel {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius); box-shadow: var(--shadow);
    padding: 1rem 1.2rem;
  }

  /* ---------------------------------------------------------------- badges */
  .mark {
    font-size: .68rem; font-weight: 700; text-transform: uppercase;
    letter-spacing: .05em; color: var(--muted);
    border: 1px solid var(--line); border-radius: 999px;
    padding: .1rem .55rem; background: var(--surface); white-space: nowrap;
  }
  .mark.active { color: #fff; background: var(--green); border-color: var(--green); }

  /* --------------------------------------------------------------- controls */
  button, .btn {
    font: inherit; font-weight: 600; cursor: pointer;
    display: inline-block; text-decoration: none;
    padding: .5rem 1.15rem; border-radius: var(--radius-sm);
    border: 1px solid transparent; background: var(--green); color: #fff;
    transition: background .12s ease, box-shadow .12s ease, transform .12s ease;
  }
  /* The rule above sets `display`, which outranks the user agent's
     `[hidden] { display: none }` — so `<button hidden>` stayed on screen. The
     report page's Download PDF was hidden that way and had been visible with
     nothing to download since the button gained the attribute: a control that
     does nothing, offered before it can work.

     Fixed here rather than on `#pdf` because the cause is this stylesheet, and
     the next hidden button would inherit the same bug. Same class of defect as
     `.drawer[hidden]` on the projects page; that one is fixed at its own rule
     because it is the element that sets `display: flex`. */
  button[hidden], .btn[hidden] { display: none !important; }
  button:hover, .btn:hover { background: var(--green-dark); }
  button:active, .btn:active { transform: translateY(1px); }
  button.secondary, .btn.secondary {
    background: var(--surface); color: var(--green-dark); border-color: var(--line);
  }
  button.secondary:hover, .btn.secondary:hover {
    background: var(--tint); border-color: var(--tint-line);
  }
  button:disabled, .btn:disabled {
    background: #b9cdc2; color: #f2f7f4; border-color: transparent;
    cursor: not-allowed; transform: none;
  }
  button.secondary:disabled { background: var(--bg); color: #9aaaa1; }

  input, select, textarea {
    font: inherit; color: var(--ink); background: var(--surface);
    padding: .48rem .6rem;
    border: 1px solid var(--line); border-radius: var(--radius-sm);
    transition: border-color .12s ease, box-shadow .12s ease;
  }
  input:hover, select:hover { border-color: var(--tint-line); }
  input::placeholder { color: #93a49a; }

  /* One visible focus treatment everywhere, and only for keyboard users. A
     control whose focus cannot be seen is unusable without a mouse. */
  :focus-visible {
    outline: 2px solid var(--green-mid); outline-offset: 2px;
    border-radius: var(--radius-sm);
  }
  input:focus-visible, select:focus-visible, textarea:focus-visible {
    outline-offset: 0; border-color: var(--green-mid);
    box-shadow: 0 0 0 3px rgba(42, 125, 85, .18);
  }

  /* --------------------------------------------------------------- notices */
  .problem, .warn {
    margin: .6rem 0 0; padding: .5rem .75rem;
    color: var(--warn-body); background: var(--warn-bg);
    border: 1px solid var(--warn-line); border-left: 4px solid #d9a441;
    border-radius: var(--radius-sm); font-size: .92rem;
  }
  .err { color: var(--err-ink); }

  /* An animation someone did not ask for is an animation they cannot stop. */
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: .001ms !important; animation-iteration-count: 1 !important;
      transition-duration: .001ms !important; scroll-behavior: auto !important;
    }
  }
"""


#: Styling for the error page below. Deliberately tiny: an error page that needs
#: the whole stylesheet is an error page that can fail to render.
_ERROR_STYLE = BASE + """
  .oops { margin: 4rem auto 0; max-width: 34rem; text-align: center; }
  .oops .code { font-size: 3.2rem; font-weight: 800; letter-spacing: -.03em;
    color: var(--tint-line); line-height: 1; }
  .oops h1 { margin: .4rem 0 .6rem; }
  .oops p { color: var(--muted); }
  .oops .actions { margin-top: 1.6rem; }
"""


def error_page(status: int, heading: str, detail: str) -> str:
    """A readable page for a browser that asked for one.

    API clients still get JSON — this is only reached when the request said it
    accepts HTML. Someone who mistypes a project id in the address bar was being
    shown a raw JSON object, which tells them the server is working and nothing
    about what to do next; the way back to the project list is the useful part.

    `heading` and `detail` are escaped by the caller.
    """
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>TrapTracker Reporting Extension &mdash; {status}</title>"
            f"<style>{_ERROR_STYLE}</style></head><body><div class=\"wrap\">"
            f"{header()}"
            f'<div class="oops"><p class="code">{status}</p>'
            f"<h1>{heading}</h1><p>{detail}</p>"
            f'<p class="actions"><a class="btn" href="/">Back to projects</a></p>'
            "</div></div></body></html>")


def header(brand_href: str = "/", banner_html: str = "") -> str:
    """The top bar. `banner_html` is the caller's already-escaped project banner.

    Pages inside a project pass `picker.banner(...)`; the picker itself passes
    nothing and gets the bar without a project side.
    """
    whose = f'<nav class="whose">{banner_html}</nav>' if banner_html else ""
    return (f'<header class="app"><a class="brand" href="{brand_href}">'
            f'<span class="dot" aria-hidden="true"></span>TrapTracker Reporting Extension</a>'
            f'{whose}</header>')
