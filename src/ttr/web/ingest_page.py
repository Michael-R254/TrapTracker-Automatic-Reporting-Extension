"""The `/ingest` page: markup, style and the polling client.

Kept in its own module rather than beside its route (the house style elsewhere)
for two reasons: ``app.py`` is already ~600 lines, and physically separating this
page's CSS guarantees the report page's carefully-tuned layout and print rules
cannot be disturbed by an edit here.

The palette is no longer duplicated from ``app.py``: it comes from ``theme.py``,
along with the header bar, buttons and inputs every page shares. The reason this
module used to keep its own copy — that editing a shared stylesheet would mean
touching the report page's tuned print rules — was right about the risk and wrong
about the remedy, and ``theme.py`` says what was split off and what was left in
``app.py`` untouched.

Green is theme only, never a data verdict. The agreement flag is rendered in
words, never as a colour, here as everywhere.
"""

from __future__ import annotations

from . import theme as _theme

#: The markup above the run controls. A separate constant so the helper
#: below can inject the icons without the template growing a format call
#: for every brace in the CSS.
_HEAD = """<div class="wrap">
  <header class="app">
    <a class="brand" href="/"><span class="dot" aria-hidden="true"></span>TrapTracker Reporting Extension</a>
    <nav class="whose"><!--PROJECT--></nav>
  </header>
  <nav class="crumbs"><a href=".">&larr; Reports</a> <span aria-hidden="true">&middot;</span>
     <span aria-current="page">Ingest</span></nav>

  <div class="titlerow">
    <div class="titletext">
      <h1>Ingest new alerts</h1>
      <p class="note">Polls the dedicated alert mailbox, runs BioCLIP and the VLM over
         each image, and stores the result.</p>
    </div>
    <span class="posture"><!--LOCK-->Local demonstration tool &middot; session token</span>
  </div>

  <!-- First-class, not a clause in a paragraph. This is the one page in the app
       whose buttons write. -->
  <div class="warn actbox" role="note">
    <p class="actbox-h"><!--WARN-->These buttons do something</p>
    <p class="actbox-b">Unlike the report page, fetching writes new rows to the project
       database and saves images to disk. Nothing upstream is touched: the detection
       system is never called, and its class token is never overwritten.</p>
  </div>

  <!-- The same device as the reports page's fence: this page's version of the
       promise, closed by a dashed rule stating it. -->
  <section class="pipecard">
    <ol class="pipe"><!--PIPE--></ol>
    <p class="fence"><span>the upstream class token is never overwritten</span></p>
  </section>
"""


#: The three stages, in the order a message travels them. One sentence each,
#: describing what the pipeline actually does — not what it could do.
_PIPELINE = (
    ("mail", "Mailbox",
     "Unseen messages in the dedicated alert inbox, newest first."),
    ("sparkle", "BioCLIP + VLM",
     "Two independent reads per image. Neither sees the upstream label."),
    ("database", "One row per image",
     "Both timestamps, per-field provenance, and any stage error."),
)


def _pipe_html() -> str:
    """The pipeline strip. Static: it describes the code, not a run."""
    from .picker import _icon

    return "".join(
        f'<li><span class="pipe-ico">{_icon(name)}</span>'
        f'<p class="pipe-h">{head}</p><p class="pipe-b">{body}</p></li>'
        for name, head, body in _PIPELINE)


def _head_html() -> str:
    """Everything above the run controls: breadcrumb, title, the acts-on-your-data
    callout, and the pipeline strip."""
    from .picker import _icon

    return (_HEAD
            .replace("<!--LOCK-->", _icon("lock"))
            .replace("<!--WARN-->", _icon("warning"))
            .replace("<!--PIPE-->", _pipe_html()))


INGEST_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrapTracker Reporting Extension — ingest</title>
<style>""" + _theme.BASE + """
  /* Page chrome comes from theme.py. What stays here is this page's own
     furniture: the run controls, the preflight box, the log console and the
     thumbnail strip. */

  /* ============================================ page shell, title, callout */
  body { padding: 0 clamp(20px, 8vw, 120px) 5rem; }
  .wrap { width: min(1200px, 100%); }
  header.app { padding: 0; height: 64px; margin-bottom: 0; }

  .crumbs { display: flex; align-items: center; gap: .45rem;
            margin: 1.8rem 0 .9rem; font-size: .88rem; color: var(--faint); }
  .crumbs a { font-weight: 600; text-decoration: none; }
  .crumbs a:hover { text-decoration: underline; text-underline-offset: 3px; }

  .titlerow { display: flex; align-items: flex-start; gap: 2rem; margin: 0 0 1.4rem; }
  .titletext { flex: 1 1 auto; min-width: 0; }
  .titlerow h1 { font-size: clamp(38px, 3.8vw, 56px); line-height: 1;
                 letter-spacing: -.035em; color: var(--ink); margin: 0 0 .6rem; }
  .titlerow .note { margin: 0; max-width: 56ch; font-size: 1rem; }
  .posture {
    flex: none; display: inline-flex; align-items: center; gap: .4rem;
    font-size: .82rem; color: var(--muted);
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 999px; padding: .34rem .8rem .34rem .65rem; white-space: nowrap;
  }
  .posture .ico { width: 15px; height: 15px; color: var(--faint); }

  /* --------------------------------------------------- "these buttons do something" */
  /* Full width and directly under the title, because it is the difference
     between this page and every other one. */
  .actbox { margin: 0 0 1.5rem !important; padding: 1rem 1.2rem;
            border-left-width: 1px !important; border-radius: var(--radius-panel); }
  .actbox-h { display: flex; align-items: center; gap: .5rem;
              margin: 0 0 .35rem !important; font-weight: 600; font-size: 1rem;
              color: var(--warn-ink) !important; }
  .actbox-h .ico { width: 18px; height: 18px; flex: none; }
  .actbox-b { margin: 0 !important; font-size: .93rem; line-height: 1.6;
              color: var(--warn-body) !important; max-width: 88ch; }

  /* ------------------------------------------------------------ pipeline strip */
  .pipecard { background: var(--surface); border: 1px solid var(--line);
              border-radius: var(--radius-card); padding: 1.2rem; margin: 0 0 1.5rem; }
  .pipe { list-style: none; display: grid; grid-template-columns: repeat(3, 1fr);
          margin: 0 0 1.1rem; padding: 0;
          border: 1px solid var(--line-soft); border-radius: var(--radius-panel);
          overflow: hidden; }
  .pipe li { position: relative; padding: 1.2rem 1.3rem; min-width: 0;
             border-right: 1px solid var(--line-soft); }
  .pipe li:last-child { border-right: 0; }
  .pipe li + li::before {
    content: ""; position: absolute; left: -5px; top: 50%;
    width: 9px; height: 9px; transform: translateY(-50%) rotate(45deg);
    border-top: 1.5px solid var(--dash); border-right: 1.5px solid var(--dash);
    background: var(--surface);
  }
  .pipe-ico { display: grid; place-items: center; width: 30px; height: 30px;
              border-radius: var(--radius-sm); background: var(--tint);
              color: var(--green); margin-bottom: .8rem; }
  .pipe-ico .ico { width: 17px; height: 17px; }
  .pipe-h { margin: 0 0 .25rem; font-weight: 600; font-size: 1rem; color: var(--ink); }
  .pipe-b { margin: 0; font-size: .88rem; line-height: 1.55; color: var(--muted); }

  /* The same fence as the reports page, making this page's promise. */
  .fence { display: flex; align-items: center; gap: 1rem; margin: 0; }
  .fence::before, .fence::after { content: ""; flex: 1;
                                  border-top: 2px dashed #c6d5c9; }
  .fence span { font-family: var(--font-mono); font-size: .78rem;
                color: var(--faint); white-space: nowrap; }
  /* ==================================== run controls + where it reads/writes */
  .twocol { display: grid; grid-template-columns: 1fr 1fr; gap: 24px;
            margin: 0 0 1.5rem; align-items: start; }
  .runcard, .readwrite {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--radius-card); padding: 1.4rem 1.5rem;
  }
  .runhead { display: flex; align-items: center; justify-content: space-between;
             gap: 1rem; margin-bottom: 1.1rem; }
  .runcard h2, .readwrite h2 { font-size: 22px; margin: 0; color: var(--ink);
                               letter-spacing: -.02em; }

  .pill {
    display: inline-flex; align-items: center; gap: .4rem;
    font-size: .68rem; font-weight: 700; letter-spacing: .07em;
    text-transform: uppercase; white-space: nowrap;
    border-radius: 999px; padding: .26rem .7rem;
    background: var(--tint); color: var(--green); border: 1px solid var(--tint-line);
  }
  .pill-dot { width: .4rem; height: .4rem; border-radius: 50%;
              background: currentColor; flex: none; }
  /* Pulses ONLY while a run is live — it marks activity, not status. */
  .pill-live .pill-dot { animation: beat 1.6s ease-in-out infinite; }
  @keyframes beat { 50% { opacity: .25; } }
  .pill-bad { background: var(--warn-bg); color: var(--warn-ink);
              border-color: var(--warn-line); }
  @media (prefers-reduced-motion: reduce) { .pill-live .pill-dot { animation: none; } }

  .runbtns { display: flex; flex-wrap: wrap; gap: .7rem; margin-bottom: 1.2rem; }
  .runbtns button { display: inline-flex; align-items: center; gap: .5rem;
                    padding: .8rem 1.3rem; }

  .switchrow { display: flex; flex-direction: column; gap: .35rem;
               padding-bottom: 1.2rem; border-bottom: 1px solid var(--line-soft); }
  .switch { display: inline-flex; align-items: center; gap: .65rem; cursor: pointer; }
  .switch input { position: absolute; opacity: 0; width: 0; height: 0; }
  .switch .track { position: relative; flex: none; width: 44px; height: 26px;
                   border-radius: 999px; background: #cbd6ce;
                   transition: background .16s ease; }
  .switch .track::after {
    content: ""; position: absolute; top: 3px; left: 3px; width: 20px; height: 20px;
    border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(16, 44, 32, .3);
    transition: transform .16s cubic-bezier(.2, .7, .3, 1);
  }
  .switch input:checked + .track { background: var(--green); }
  .switch input:checked + .track::after { transform: translateX(18px); }
  .switch input:focus-visible + .track { outline: 2px solid var(--green-mid);
                                         outline-offset: 2px; }
  .switchtext { font-weight: 600; color: var(--ink); }
  .fhelp { margin: 0; font-size: .86rem; line-height: 1.5; color: var(--muted);
           max-width: 46ch; }

  /* ------------------------------------------------------------- run state */
  #runstate { padding-top: 1.1rem; }
  .runline { display: flex; flex-wrap: wrap; align-items: baseline; gap: .3rem .9rem;
             margin: 0 0 .9rem; color: var(--muted); font-size: .95rem; }
  .runline .clock { color: var(--faint); font-size: .86rem; }
  .mono { font-family: var(--font-mono); }

  /* Indeterminate by construction: the run cannot know how many messages a poll
     will yield, so there is no denominator and the bar never claims one. */
  .bar { position: relative; height: 8px; border-radius: 999px;
         background: var(--hairline); overflow: hidden; margin: 0 0 1.1rem; }
  .bar span { position: absolute; inset: 0 auto 0 0; width: 34%;
              border-radius: 999px; background: var(--green); }
  .bar-indet span { animation: sweep 1.5s ease-in-out infinite; }
  @keyframes sweep {
    0%   { left: -34%; }
    100% { left: 100%; }
  }
  @media (prefers-reduced-motion: reduce) {
    .bar-indet span { animation: none; left: 0; width: 100%; opacity: .35; }
  }

  .counters { display: grid; grid-template-columns: repeat(auto-fit, minmax(7rem, 1fr));
              gap: .9rem 1.2rem; margin: 0 0 1.1rem; }
  .counter { display: flex; flex-direction: column; gap: .1rem; }
  .counter strong { font-family: var(--font-display); font-weight: 700;
                    font-size: 1.7rem; line-height: 1.1; letter-spacing: -.025em;
                    color: var(--ink); font-variant-numeric: tabular-nums; }
  .counter span { font-size: .8rem; color: var(--muted); }

  .runactions { display: flex; flex-wrap: wrap; align-items: center; gap: .7rem; }
  .stopbtn { display: inline-flex; align-items: center; gap: .45rem; }

  /* --------------------------------------------------- where it reads/writes */
  .rw { display: grid; grid-template-columns: max-content 1fr; gap: 0 1.2rem;
        margin: 0; }
  .rw dt, .rw dd { padding: .62rem 0; border-bottom: 1px solid var(--hairline);
                   margin: 0; font-size: .9rem; }
  .rw dt { color: var(--faint); }
  .rw dd { text-align: right; color: var(--ink); }
  .rw dd.mono { font-family: var(--font-mono); font-size: .84rem; }
  .rw-big { display: flex; align-items: baseline; justify-content: space-between;
            gap: 1rem; padding: 1rem 0 .4rem; }
  .rw-k { font-size: .9rem; color: var(--faint); }
  .rw-v { font-family: var(--font-display); font-weight: 700; font-size: 2.4rem;
          line-height: 1; letter-spacing: -.03em; color: var(--ink);
          font-variant-numeric: tabular-nums; }
  .rw-note { margin: 0; font-size: .86rem; line-height: 1.55; color: var(--muted); }
  .readwrite .callout { margin-top: 1rem !important; }
  .empty { color: var(--faint); font-size: .92rem; }

  /* ============================================================ image results */
  .results { margin: 0 0 1.5rem; }
  .resultshead { display: flex; flex-wrap: wrap; align-items: baseline;
                 justify-content: space-between; gap: .5rem 1.5rem;
                 margin-bottom: 1rem; }
  .results h2 { font-size: 26px; margin: 0; color: var(--ink); letter-spacing: -.025em; }
  .resultsnote { margin: 0; font-size: .86rem; color: var(--faint); }

  #strip { display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr));
           gap: 1rem; }
  .card { background: var(--surface); border: 1px solid var(--line);
          border-radius: var(--radius-panel); overflow: hidden;
          transition: transform .16s ease, box-shadow .16s ease; }
  .card:hover { transform: translateY(-3px); box-shadow: var(--shadow-card); }
  .card img { display: block; width: 100%; height: 8.5rem; object-fit: cover;
              background: var(--sunken); }
  .noimg { height: 8.5rem; display: grid; place-items: center; background: var(--sunken);
           color: var(--faint); font-size: .8rem; }
  .cap { padding: .8rem .9rem 1rem; }
  .cardpos { margin: 0 0 .4rem; font-family: var(--font-mono); font-size: .74rem;
             color: var(--faint); }
  .labrow { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem;
            margin-bottom: .45rem; }
  /* The upstream token, exactly as stored. Mono so it reads as a stored value
     rather than as prose the page chose. */
  .lab { font-family: var(--font-mono); font-size: .92rem; font-weight: 600;
         color: var(--ink); word-break: break-word; }
  .binom { margin: 0 0 .4rem; font-style: italic; font-size: .86rem;
           color: var(--muted); }
  .cardnote { margin: 0 0 .3rem; font-size: .82rem; line-height: 1.5;
              color: var(--warn-body); }
  .msgid { margin: .4rem 0 0; font-family: var(--font-mono); font-size: .7rem;
           color: var(--faint); overflow: hidden; text-overflow: ellipsis;
           white-space: nowrap; }

  /* Chips. `agrees` and `differs` share ONE style: they are two neutral
     outcomes of the same check, and giving either a colour the other lacks
     would let a reader infer a verdict from styling. Only a stage failure is
     amber, because only that is a problem. */
  .chip { display: inline-block; font-size: .7rem; font-weight: 600;
          border-radius: 999px; padding: .16rem .55rem; white-space: nowrap; }
  .chip-x { background: var(--tint); color: var(--green);
            border: 1px solid var(--tint-line); }
  .chip-warn { background: var(--warn-bg); color: var(--warn-ink);
               border: 1px solid var(--warn-line); }
  .card-notstored { border-color: var(--warn-line); }

  .stripempty { grid-column: 1 / -1; text-align: center; padding: 3rem 1.2rem;
                background: var(--surface); border: 1.5px dashed var(--dash);
                border-radius: var(--radius-card); }
  .stripempty-h { margin: 0 0 .4rem; font-family: var(--font-display);
                  font-weight: 700; font-size: 20px; color: var(--ink);
                  letter-spacing: -.02em; }
  .stripempty-b { margin: 0; color: var(--muted); font-size: .92rem; }

  .dropped { margin: 1rem 0 0; font-size: .86rem; color: var(--faint); }

  /* ============================================================ debug block */
  .debugwrap { margin: 0 0 1.5rem; }
  .term { background: var(--term-bg); border-radius: var(--radius-panel);
          padding: 1rem 0 1.1rem; }
  .term-bar { display: flex; align-items: center; justify-content: space-between;
              gap: 1rem; padding: 0 1.1rem .8rem;
              border-bottom: 1px solid rgba(214, 232, 220, .12);
              font-size: .68rem; font-weight: 700; letter-spacing: .1em;
              text-transform: uppercase; color: var(--term-prompt); }
  .term-bar > span:first-child { display: inline-flex; align-items: center; gap: .45rem; }
  .term-bar .ico { width: 15px; height: 15px; }
  .term-ctx { color: rgba(214, 232, 220, .45); font-weight: 500;
              letter-spacing: .04em; text-transform: none; }
  /* The console: the same structured lines the CLI prints, verbatim, on the
     terminal ground the other pages use for command blocks. */
  #console { margin: 0; padding: .9rem 1.1rem 0; max-height: 22rem;
             overflow: auto; background: none; border: 0;
             font-family: var(--font-mono); font-size: .78rem; line-height: 1.6;
             white-space: pre; color: var(--term-ink); }
  #console .ln { white-space: pre; }
  #console .lv-WARNING { color: #e7c887; }
  #console .lv-ERROR, #console .lv-CRITICAL { color: #f0a9a9; font-weight: 600; }
  #console .lv-DEBUG { color: rgba(214, 232, 220, .62); }

  /* ============================================================= responsive */
  /* One breakpoint, matching the other two pages. */
  @media (max-width: 860px) {
    header.app { height: auto; min-height: 64px; padding: .7rem 0; }
    header.app nav.whose { margin-left: 0; }

    .titlerow { display: block; }
    .titlerow h1 { font-size: clamp(34px, 9vw, 42px); }
    .titlerow .note { max-width: none; }
    .posture { margin-top: 1rem; }

    /* The pipeline strip stacks, and its connector turns to point down. */
    .pipe { grid-template-columns: 1fr; }
    .pipe li { border-right: 0; border-bottom: 1px solid var(--line-soft); }
    .pipe li:last-child { border-bottom: 0; }
    .pipe li + li::before { left: 50%; top: -5px;
                            transform: translateX(-50%) rotate(135deg); }
    .fence { flex-wrap: wrap; justify-content: center; gap: .5rem; }
    .fence::before { flex: 1 0 100%; }
    .fence::after { display: none; }

    .twocol { grid-template-columns: 1fr; gap: 16px; }
    .runbtns { display: grid; gap: .6rem; }
    .runbtns button { width: 100%; min-height: 44px; justify-content: center; }

    /* Counters in a 2-column grid. */
    .counters { grid-template-columns: 1fr 1fr; }
    .runactions .stopbtn { flex: 1; min-height: 44px; justify-content: center; }

    /* The reads/writes rows keep label and value on one line but let the value
       wrap under it rather than squeezing a path into a few characters. */
    .rw { grid-template-columns: 1fr; gap: 0; }
    .rw dt { padding-bottom: .15rem; border-bottom: 0; }
    .rw dd { text-align: left; padding-top: 0; word-break: break-all; }

    /* One image per column. */
    #strip { grid-template-columns: 1fr; }
    .card img, .noimg { height: 11rem; }

    /* The debug block scrolls inside its own container rather than widening
       the page. */
    #console { max-height: 16rem; }
  }
</style></head>
<body>
""" + _head_html() + """
  <div class="twocol">
    <!-- Run controls. Stop is NOT here: it does not exist while idle, and it is
         rendered into the running panel instead of sitting disabled. -->
    <section class="runcard">
      <div class="runhead">
        <h2>Run</h2>
        <span class="pill" id="pill"><span class="pill-dot" aria-hidden="true"></span><span id="pill-text">Idle</span></span>
      </div>
      <div class="runbtns">
        <button id="once"><!--DOWN-->Fetch new alerts now</button>
        <button id="loop" class="secondary"><!--REFRESH-->Poll continuously</button>
      </div>
      <div class="switchrow">
        <label class="switch">
          <input type="checkbox" id="debug" aria-describedby="debug-help">
          <span class="track" aria-hidden="true"></span>
          <span class="switchtext">Capture DEBUG lines on the next run</span>
        </label>
        <p class="fhelp" id="debug-help">Verbose per-image logging, shown beneath the
           run and not stored.</p>
      </div>
      <div id="runstate"></div>
    </section>

    <section class="readwrite">
      <h2>Where it reads and writes</h2>
      <div id="preflight"><p class="empty">Checking configuration&hellip;</p></div>
    </section>
  </div>

  <section class="results">
    <div class="resultshead">
      <h2>Images processed this run</h2>
      <p class="resultsnote">Newest first &middot; the upstream label is shown as
         stored, never replaced</p>
    </div>
    <div id="strip"></div>
    <p id="dropped" class="dropped" hidden></p>
  </section>

  <section class="debugwrap" id="debugwrap" hidden>
    <div class="term">
      <div class="term-bar"><span><!--TERM-->Debug, this run</span>
        <span class="term-ctx">not stored &middot; cleared when the page reloads</span></div>
      <pre id="console" role="log" aria-live="polite" aria-label="Processing log"></pre>
    </div>
  </section>

</div>
<script>
// Every value rendered below is EMAIL-DERIVED and untrusted (CLAUDE.md §6): labels,
// message ids, parse warnings and the formatted log lines all quote text an alert
// email controls. The report page's defence is server-side sanitising; this page's
// defence is that it never treats the payload as markup at all. textContent only —
// no innerHTML, no insertAdjacentHTML, anywhere on this page.
const $ = id => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;   // the one and only text sink
  return n;
};
const clear = n => { while (n.firstChild) n.removeChild(n.firstChild); };

const MAX_LINES = 500, MAX_CARDS = 60;
let cursor = 0, runId = null, showDebug = false, timer = null, idlePolls = 0;
let lastLevel = 'info', lastState = 'idle', hasCredential = true;

// Counters for THIS run, accumulated from the events actually received. Three,
// and each is a count of something the run reported:
//   seen    — image events received
//   written — those the pipeline stored (ok true)
//   errored — stored rows carrying a stage error (BioCLIP or the describer)
// A record that was NOT stored (ok false) is a different outcome; it is counted
// separately and said on its own card rather than folded into `errored`.
let runCount = { seen: 0, written: 0, errored: 0, notStored: 0 };
function resetCounts() { runCount = { seen: 0, written: 0, errored: 0, notStored: 0 }; }

// ---------------------------------------------------------------- preflight
async function loadPreflight() {
  const box = $('preflight');
  try {
    const d = await (await fetch('api/ingest/preflight', { cache: 'no-store' })).json();
    hasCredential = d.has_credential !== false;
    clear(box);

    const rows = el('dl', 'rw');
    const add = (k, v, mono) => {
      rows.appendChild(el('dt', null, k));
      rows.appendChild(el('dd', mono ? 'mono' : null, v));
    };
    add('Mailbox', d.imap_host ? d.imap_host + ' / ' + d.imap_folder : 'not configured', true);
    add('Writes to', d.db_path, true);
    add('Images', d.image_store_dir, true);
    add('Already ingested', d.seen_count + ' message ids', true);
    box.appendChild(rows);

    // The one number worth reading at a glance.
    const big = el('div', 'rw-big');
    big.appendChild(el('span', 'rw-k', 'Rows now'));
    big.appendChild(el('span', 'rw-v', String(d.db_rows)));
    box.appendChild(big);
    box.appendChild(el('p', 'rw-note',
      'Message ids already seen are skipped, so a re-run adds nothing and costs nothing.'));

    if (d.config_error) box.appendChild(el('p', 'warn callout', d.config_error));

    // §5: no credential stored — the fetch controls are disabled and the line
    // names the command that stores one, matching the projects page.
    if (!hasCredential) {
      const w = el('div', 'warn callout');
      w.appendChild(el('p', 'callout-h', 'No mailbox credential stored'));
      // A machine with no credential store (a container) is told the variable
      // instead: `set-password` cannot succeed there.
      const noStore = d.credential_store_available === false;
      const b = el('p', 'callout-b');
      b.appendChild(document.createTextNode('Fetching is disabled until one exists. ' +
        (noStore ? 'Set ' : 'Run ')));
      b.appendChild(el('code', null, d.credential_hint || 'ttr project set-password'));
      b.appendChild(document.createTextNode(noStore
        ? ' in this server’s environment and restart it. This machine has no operating-system credential store to keep a password in.'
        : ' to store it in your operating system’s credential store.'));
      w.appendChild(b);
      box.appendChild(w);
    }
    if (d.ingest_lock_held_by) {
      const w = el('div', 'warn callout');
      w.appendChild(el('p', 'callout-h', 'A run is already in progress elsewhere'));
      w.appendChild(el('p', 'callout-b',
        'Held by ' + d.ingest_lock_held_by + '. Starting one here would be refused.'));
      box.appendChild(w);
    }
    if (d.seen_count > 0 && d.db_rows === 0) {
      const w = el('div', 'warn callout');
      w.appendChild(el('p', 'callout-h', 'The seen-store and this database disagree'));
      w.appendChild(el('p', 'callout-b',
        'The seen-store already lists ' + d.seen_count + ' message ids, but this database ' +
        'is empty. Those messages will be skipped as already-ingested and nothing will be ' +
        'stored. Point the project at the database that seen-store belongs to, or clear ' +
        'the seen-store to re-ingest from scratch.'));
      box.appendChild(w);
    }
    applyControls(lastState);
  } catch (e) {
    clear(box);
    box.appendChild(el('p', 'err', 'Could not read configuration: ' + e));
  }
}

// ------------------------------------------------------------------ render
// A DEBUG line is shown when the RUN captured debug, or when the reader has
// asked to see them. The checkbox requests capture for the NEXT run; tying
// display to it alone meant a run that did capture DEBUG hid its own lines
// after a reload, because reloading clears the checkbox and not the buffer.
function debugAllowed() { return showDebug || lastLevel === 'debug'; }

function appendLog(p) {
  if (!debugAllowed() && p.level === 'DEBUG') return;
  const box = $('console');
  // Measure BEFORE appending: only autoscroll if the reader was already at the
  // bottom, so scrolling back to read a line does not fight the stream.
  const pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.appendChild(el('div', 'ln lv-' + p.level, p.line));
  while (box.childElementCount > MAX_LINES) box.removeChild(box.firstChild);
  if (pinned) box.scrollTop = box.scrollHeight;
  $('debugwrap').hidden = box.childElementCount === 0;
}

// The cross-check chip. `agrees` and `differs` are two NEUTRAL outcomes with the
// same visual weight — neither is an error, and neither is styled as one. A
// disagreement additionally carries how far off it was, which turns a binary
// into something a reader can weigh: a near miss and a read of the vegetation
// are not the same finding.
function crossCheckChip(p) {
  if (p.agreement === 'agree') return el('span', 'chip chip-x', 'cross-check agrees');
  if (p.agreement === 'disagree') {
    let text = 'cross-check differs';
    if (p.taxonomic_distance && p.taxonomic_distance !== 'undetermined') {
      text += ' · ' + p.taxonomic_distance.replace(/_/g, ' ');
    }
    return el('span', 'chip chip-x', text);
  }
  if (p.agreement === 'not_evaluable') {
    return el('span', 'chip chip-x', 'not comparable · excluded from the count');
  }
  return el('span', 'chip chip-x', 'cross-check unavailable');
}

function appendCard(p) {
  const strip = $('strip');
  runCount.seen += 1;

  const notStored = p.ok === false;
  const stageError = !notStored && (p.vlm_ok === false || p.bioclip_ok === false);
  if (notStored) runCount.notStored += 1;
  else { runCount.written += 1; if (stageError) runCount.errored += 1; }

  const card = el('article', 'card' + (notStored ? ' card-notstored' : ''));

  // Only ever a data: URI the server built; anything else gets the placeholder.
  if (typeof p.thumb === 'string' && p.thumb.startsWith('data:image/')) {
    const img = el('img');
    img.src = p.thumb;
    img.alt = 'Camera trap image, upstream class ' + (p.upstream_label || 'unknown');
    card.appendChild(img);
  } else {
    card.appendChild(el('div', 'noimg', 'no thumbnail'));
  }

  const cap = el('div', 'cap');

  // Identified by POSITION IN THE RUN. The filename is deliberately unavailable
  // — `_on_progress` strips every filesystem path before anything reaches the
  // browser — and an arrival time formatted like a capture time would be a
  // quiet substitution. #12 is a real fact about the run.
  cap.appendChild(el('p', 'cardpos', '#' + runCount.seen));

  const labrow = el('div', 'labrow');
  // The upstream token EXACTLY as stored. Never re-resolved, never corrected.
  labrow.appendChild(el('span', 'lab', p.upstream_label || '(no label in email)'));
  if (notStored) {
    labrow.appendChild(el('span', 'chip chip-warn', 'not stored'));
  } else {
    if (stageError) labrow.appendChild(el('span', 'chip chip-warn', 'enrichment error'));
    labrow.appendChild(crossCheckChip(p));
  }
  cap.appendChild(labrow);

  if (notStored) {
    cap.appendChild(el('p', 'binom', '—'));
    cap.appendChild(el('p', 'cardnote',
      'This event was not stored. ' + (p.error || 'unknown error')));
  } else {
    cap.appendChild(el('p', 'binom', p.canonical_key || '—'));
    if (stageError) {
      const why = p.vlm_error || p.bioclip_error || 'a stage failed';
      cap.appendChild(el('p', 'cardnote', 'The row is stored with its error: ' + why));
    }
    if (p.parse_warnings && p.parse_warnings.length) {
      cap.appendChild(el('p', 'cardnote', 'warnings: ' + p.parse_warnings.join('; ')));
    }
  }
  cap.appendChild(el('p', 'msgid', p.message_id || ''));

  card.appendChild(cap);
  // Newest first.
  strip.insertBefore(card, strip.firstChild);
  while (strip.childElementCount > MAX_CARDS) strip.removeChild(strip.lastChild);
}

// ------------------------------------------------------------------ controls
function applyControls(state) {
  const running = state === 'running' || state === 'stopping';
  // Disabled while a run is in flight: a double-submit must be impossible from
  // the UI as well as from the server, which refuses it with a 409 anyway.
  $('once').disabled = running || !hasCredential;
  $('loop').disabled = running || !hasCredential;
}

const PILL = {
  idle:     ['Idle', ''],
  running:  ['Fetching', 'pill-live'],
  stopping: ['Stopping', 'pill-live'],
  done:     ['Done', ''],
  failed:   ['Failed', 'pill-bad'],
};

function applyState(d) {
  const running = d.state === 'running' || d.state === 'stopping';
  applyControls(d.state);

  let [label, cls] = PILL[d.state] || ['Idle', ''];
  if (d.state === 'done' && d.stopped_by_user) label = 'Stopped';
  $('pill').className = 'pill ' + cls;
  $('pill-text').textContent = label;

  const box = $('runstate');
  clear(box);

  if (d.state === 'idle') {
    box.appendChild(el('p', 'runline', 'Idle — nothing has run in this session.'));
    $('dropped').hidden = true;
    return;
  }

  // ---- the live line: position and elapsed, never a denominator.
  const line = el('p', 'runline');
  const mins = Math.floor(d.elapsed_s / 60), secs = Math.round(d.elapsed_s % 60);
  const clock = String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
  if (running) {
    // No "of N": the pipeline iterates a generator and never learns how many
    // messages a poll will yield, so any denominator here would be invented.
    line.appendChild(el('span', null,
      runCount.seen ? 'Processing image ' + runCount.seen : 'Polling the mailbox'));
    line.appendChild(el('span', 'mono clock', clock + ' elapsed'));
  } else if (d.state === 'done' && d.stopped_by_user) {
    line.appendChild(el('span', null,
      'Stopped after ' + clock + '. The counts below are partial — the run did not finish.'));
  } else if (d.state === 'done' && runCount.seen === 0 && d.stored === 0) {
    // §5 "nothing new" — a success, not an empty state.
    line.appendChild(el('span', null,
      'The mailbox held nothing new. No messages were unseen, so nothing was stored.'));
  } else if (d.state === 'done') {
    line.appendChild(el('span', null, 'Finished in ' + clock + '.'));
  } else if (d.state === 'failed') {
    line.appendChild(el('span', null, 'The run stopped after ' + clock + '.'));
  }
  box.appendChild(line);

  // ---- progress: indeterminate, because no total is knowable.
  if (running) {
    const bar = el('div', 'bar bar-indet');
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-busy', 'true');
    bar.setAttribute('aria-label', 'Processing images; the total is not known in advance');
    bar.appendChild(el('span'));
    box.appendChild(bar);
  }

  // ---- counters, when the run has reported any image at all.
  if (runCount.seen > 0) {
    const grid = el('div', 'counters');
    const add = (v, k) => {
      const c = el('div', 'counter');
      c.appendChild(el('strong', null, String(v)));
      c.appendChild(el('span', null, k));
      grid.appendChild(c);
    };
    add(runCount.seen, 'images this run');
    add(runCount.written, 'rows written');
    add(runCount.errored, 'stored with an error');
    if (runCount.notStored) add(runCount.notStored, 'not stored');
    box.appendChild(grid);
  }

  // ---- Stop exists only while a run does.
  if (running) {
    const actions = el('div', 'runactions');
    const stop = el('button', 'secondary stopbtn');
    stop.id = 'stop';
    stop.type = 'button';
    stop.textContent = 'Stop';
    stop.disabled = d.state === 'stopping';
    stop.addEventListener('click', doStop);
    actions.appendChild(stop);
    if (d.level === 'debug') actions.appendChild(el('span', 'chip chip-x', 'DEBUG capture on'));
    box.appendChild(actions);
  }

  // ---- §5 mailbox unreachable, and any other run failure.
  if (d.error) {
    const w = el('div', 'warn callout');
    w.appendChild(el('p', 'callout-h', 'The run could not finish'));
    // The runner's own message, not a rewrite of it: `friendly_error` already
    // turns the failure into one actionable line naming the command to run,
    // and it knows WHICH failure this was. Appending generic advice beside it
    // said the same thing twice.
    w.appendChild(el('p', 'callout-b', d.error));
    if (runCount.seen) {
      w.appendChild(el('p', 'callout-b',
        'Anything already collected in this run is still shown below.'));
    }
    box.appendChild(w);
  }

  // ---- the ring buffer reports what it evicted; so does the page.
  const dropped = $('dropped');
  if (d.dropped > 0) {
    dropped.textContent = d.dropped + ' earlier events in this run are no longer shown — ' +
      'the live buffer holds the most recent ones only.';
    dropped.hidden = false;
  } else {
    dropped.hidden = true;
  }
}

function renderEmptyStrip() {
  const strip = $('strip');
  clear(strip);
  const empty = el('div', 'stripempty');
  empty.appendChild(el('p', 'stripempty-h', 'No images processed yet'));
  empty.appendChild(el('p', 'stripempty-b',
    'Each image appears here as it is stored, with both model reads.'));
  strip.appendChild(empty);
}

// -------------------------------------------------------------------- poll
async function poll() {
  timer = null;
  let d;
  try {
    d = await (await fetch('api/ingest/state?cursor=' + cursor, { cache: 'no-store' })).json();
  } catch (e) {
    schedule(2500);
    return;
  }
  // A different run means our cursor belongs to history: reset rather than
  // silently hiding the new run's output behind a stale sequence number.
  if (d.run_id && d.run_id !== runId) {
    runId = d.run_id; cursor = 0;
    resetCounts();
    clear($('console'));
    renderEmptyStrip();
    d = await (await fetch('api/ingest/state?cursor=0', { cache: 'no-store' })).json();
  }
  lastLevel = d.level || 'info';
  const hadCards = d.events.some(ev => ev.kind === 'image');
  if (hadCards && $('strip').querySelector('.stripempty')) clear($('strip'));
  for (const ev of d.events) {
    if (ev.kind === 'log') appendLog(ev.payload); else appendCard(ev.payload);
  }
  cursor = d.cursor;
  lastState = d.state;
  applyState(d);
  const running = d.state === 'running' || d.state === 'stopping';
  idlePolls = running ? 0 : idlePolls + 1;
  // Keep watching briefly after a run ends (another tab may start one), then rest.
  if (running) schedule(800);
  else if (idlePolls < 8) schedule(2500);
}
function schedule(ms) {           // setTimeout chain, never setInterval — no overlap
  if (timer === null) timer = setTimeout(poll, ms);
}
function wake() { idlePolls = 0; if (timer !== null) { clearTimeout(timer); timer = null; } poll(); }

async function doStop() {
  const b = $('stop');
  if (b) b.disabled = true;
  try { await fetch('api/ingest/stop', { method: 'POST' }); } catch (e) { /* poll reports it */ }
  wake();
}

async function start(mode) {
  $('once').disabled = $('loop').disabled = true;
  try {
    const r = await fetch('api/ingest/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: mode, level: $('debug').checked ? 'debug' : 'info' })
    });
    if (r.status === 409) applyState(await r.json());
  } catch (e) {
    const box = $('runstate');
    clear(box);
    box.appendChild(el('p', 'err', 'Could not start: ' + e));
  }
  wake();
}

$('once').addEventListener('click', () => start('once'));
$('loop').addEventListener('click', () => start('continuous'));
// DEBUG capture is decided when a run STARTS (capturing it always would let a
// large mailbox's skip_already_seen lines evict everything else from the ring), so
// this box applies to the next run. It still re-filters the current buffer, and
// says so plainly when there is nothing extra to reveal.
$('debug').addEventListener('change', async () => {
  showDebug = $('debug').checked;
  const had = lastLevel === 'debug';
  runId = null; cursor = 0;
  if (showDebug && !had && lastState !== 'idle') {
    appendLog({ level: 'WARNING', line:
      'DEBUG lines were not captured for this run — start another run to record them.' });
  }
  wake();
});

renderEmptyStrip();
loadPreflight();
poll();          // immediate, so a run started in another tab is adopted on load
</script>
</body></html>
"""


def _with_icons(page: str) -> str:
    """Fill the body's icon slots.

    The head has its own pass in `_head_html`; these three sit in the template
    below it. Slots rather than an f-string because the template is mostly CSS
    and `str.format` would choke on every brace in it.
    """
    from .picker import _icon

    return (page
            .replace("<!--DOWN-->", _icon("arrow-right", size=17))
            .replace("<!--REFRESH-->", _icon("refresh", size=17))
            .replace("<!--STOPICO-->", _icon("stop", size=16))
            .replace("<!--TERM-->", _icon("terminal", size=15)))


INGEST_PAGE = _with_icons(INGEST_PAGE)
