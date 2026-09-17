"""PDF endpoint + adapter robustness (headless-Chrome PDF path).

Most of this stays offline with the renderer mocked: correct MIME type, a safe
download filename, the render target pinned to this local server (no Host-header
arbitrary navigation), clean error mapping, and the cleanup/util helpers.

Two further groups guard the blank-PDF regression, in which every report whose
generation was slower than the browser's settle printed as a footer with no body:

* the readiness wait is unit-tested against a scripted CDP session, so the exact
  failure (returning on the initial ``about:blank`` document, which is already
  ``readyState === "complete"``) is caught with no browser at all;
* one end-to-end test drives a REAL headless browser against a deliberately slow
  page and reads the text back out of the PDF. It skips itself when no browser (or
  no PDF reader) is available, so CI without either still passes.

The endpoint tests are parametrised over all three report types. They previously
covered only the BNG sentinel — which is precisely the one type fast enough to have
kept working, so the bug was invisible to the suite.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ttr.web.pdf as pdfmod
from ttr.web import app as appmod
from ttr.web.app import app
from ttr.web.browser import BrowserNotFound
from ttr.web.pdf import PdfRenderError, _free_port, _rmtree_retry, _terminate_tree

#: The three report types the UI offers: the two sentinels plus an ordinary species
#: term. Every endpoint-level assertion runs against all three.
REPORT_TYPES = [
    pytest.param(appmod.ALL_SPECIES, id="all-species"),
    pytest.param(appmod.BNG_ALIGNED, id="bng-aligned"),
    pytest.param("badger", id="single-species"),
]


@pytest.fixture(autouse=True)
def _clear_body_cache():
    """The rendered-body cache is module state; no test may inherit another's."""
    appmod._BODY_CACHE.clear()
    yield
    appmod._BODY_CACHE.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from conftest import make_web_project, project_client

    ctx = make_web_project(tmp_path, monkeypatch, name="PDF Test Site")
    yield project_client(app, ctx)
    app.dependency_overrides.clear()


def _capture_render(monkeypatch):
    """Replace the real browser render with a recorder that returns fake PDF bytes."""
    seen = {}

    def fake_render(url, *, footer_html, **kw):
        seen["url"] = url
        seen["footer"] = footer_html
        return b"%PDF-1.4\nfake"

    monkeypatch.setattr(pdfmod, "render_url_to_pdf", fake_render)
    return seen


@pytest.mark.parametrize("species", REPORT_TYPES)
def test_report_pdf_returns_pdf_mime_and_attachment(client, monkeypatch, species):
    _capture_render(monkeypatch)
    r = client.get("/api/report.pdf",
                   params={"species": species, "start": "2026-06-28", "end": "2026-08-05"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("attachment; filename=")
    assert r.content.startswith(b"%PDF")


@pytest.mark.parametrize("species", REPORT_TYPES)
def test_report_pdf_target_is_pinned_to_localhost_not_host_header(client, monkeypatch, species):
    seen = _capture_render(monkeypatch)
    # A spoofed Host header must NOT redirect the headless browser off-box.
    client.get("/api/report.pdf",
               params={"species": species, "start": "2026-06-28", "end": "2026-08-05"},
               headers={"host": "evil.example.com"})
    assert seen["url"].startswith("http://127.0.0.1:")
    assert "evil.example.com" not in seen["url"]
    assert "/print?" in seen["url"]


def test_report_pdf_filename_is_sanitised(client, monkeypatch):
    _capture_render(monkeypatch)
    # Hostile date params must not break out of the Content-Disposition header.
    r = client.get("/api/report.pdf",
                   params={"species": "*bng*", "start": 'a"b\r\nX', "end": "2026-08-05"})
    cd = r.headers["content-disposition"]
    assert '"' not in cd.split("filename=")[1].strip('"')   # no stray quote in the name
    assert "\r" not in cd and "\n" not in cd


@pytest.mark.parametrize("species", REPORT_TYPES)
def test_report_pdf_footer_has_page_numbering_and_period(client, monkeypatch, species):
    seen = _capture_render(monkeypatch)
    client.get("/api/report.pdf",
               params={"species": species, "start": "2026-06-28", "end": "2026-08-05"})
    assert 'class="pageNumber"' in seen["footer"]
    assert 'class="totalPages"' in seen["footer"]
    assert "2026-06-28 to 2026-08-05" in seen["footer"]


def _cookieless_print(monkeypatch):
    """A render stand-in that fetches the print URL TWICE as a client with no
    session cookie — which is exactly what the headless browser is."""
    from conftest import FakeLLM
    from ttr.web import auth

    app.dependency_overrides[appmod.get_llm] = lambda: FakeLLM("A steady week.")
    seen = {}

    def fake_render(url, *, footer_html, **kw):
        parts = urllib.parse.urlsplit(url)
        stranger = TestClient(app)
        seen["ticket"] = dict(urllib.parse.parse_qsl(parts.query))[auth.PRINT_TICKET_PARAM]
        seen["first"] = stranger.get(f"{parts.path}?{parts.query}")
        seen["again"] = stranger.get(f"{parts.path}?{parts.query}")
        seen["live_during_render"] = seen["ticket"] in auth._print_tickets
        return b"%PDF-1.4\nfake"

    monkeypatch.setattr(pdfmod, "render_url_to_pdf", fake_render)
    return seen


@pytest.mark.parametrize("species", REPORT_TYPES)
def test_report_pdf_browser_reaches_print_without_a_session(client, monkeypatch, species):
    """The token bug itself, offline: the browser has no cookie, and before the
    print ticket its /print request was refused and the refusal page printed."""
    from ttr.web import auth

    seen = _cookieless_print(monkeypatch)
    r = client.get("/api/report.pdf",
                   params={"species": species, "start": "2026-06-28", "end": "2026-08-05"})

    assert r.status_code == 200
    assert seen["first"].status_code == 200, seen["first"].text[:200]
    assert "Session token required" not in seen["first"].text
    assert "id='report'" in seen["first"].text
    assert seen["again"].status_code == 401, "the ticket must be single-use"
    assert seen["ticket"] not in auth._print_tickets


def test_report_pdf_discards_an_unused_ticket_when_the_render_fails(client, monkeypatch):
    from ttr.web import auth

    minted = {}

    def boom(url, **kw):
        minted["ticket"] = dict(urllib.parse.parse_qsl(
            urllib.parse.urlsplit(url).query))[auth.PRINT_TICKET_PARAM]
        raise PdfRenderError("browser crashed before navigating")

    monkeypatch.setattr(pdfmod, "render_url_to_pdf", boom)
    client.get("/api/report.pdf",
               params={"species": "*bng*", "start": "2026-06-28", "end": "2026-08-05"})

    assert minted["ticket"] not in auth._print_tickets


@pytest.mark.parametrize("species", REPORT_TYPES)
def test_report_pdf_missing_browser_returns_503(client, monkeypatch, species):
    def boom(*a, **k):
        raise BrowserNotFound("no browser")
    monkeypatch.setattr(pdfmod, "render_url_to_pdf", boom)
    r = client.get("/api/report.pdf",
                   params={"species": species, "start": "2026-06-28", "end": "2026-08-05"})
    assert r.status_code == 503
    assert "browser" in r.text.lower()


@pytest.mark.parametrize("species", REPORT_TYPES)
def test_report_pdf_render_failure_returns_500(client, monkeypatch, species):
    def boom(*a, **k):
        raise PdfRenderError("navigation timeout")
    monkeypatch.setattr(pdfmod, "render_url_to_pdf", boom)
    r = client.get("/api/report.pdf",
                   params={"species": species, "start": "2026-06-28", "end": "2026-08-05"})
    assert r.status_code == 500
    assert "failed" in r.text.lower()


# ---- readiness wait (the blank-PDF regression) ------------------------------
class _ScriptedCdp:
    """CDP stand-in returning one scripted probe answer per poll (last one repeats).

    Each state is ``(readyState, location.href, body-text length)`` — what the tab
    would report at that moment.
    """

    #: What the tab reports while the server is still generating the page: the
    #: navigation has not committed, so the initial blank document — already
    #: "complete" — is what any readyState check sees.
    PENDING = ("complete", "about:blank", 0)
    #: The report page, committed and painted.
    READY = ("complete", "http://127.0.0.1:8000/print?species=*all*", 4200)

    def __init__(self, *states):
        self._states = list(states)
        self.polls = 0

    def call(self, method, params=None, session_id=None, timeout=60.0):
        if (params or {}).get("awaitPromise"):          # the document.fonts wait
            return {"result": {"value": True}}
        self.polls += 1
        state = self._states[min(self.polls - 1, len(self._states) - 1)]
        return {"result": {"value": json.dumps(list(state))}}


def test_wait_does_not_accept_the_initial_blank_document():
    """The regression itself: a page whose response is still in flight must NOT be
    treated as rendered just because the blank tab reports readyState 'complete'."""
    cdp = _ScriptedCdp(_ScriptedCdp.PENDING, _ScriptedCdp.PENDING, _ScriptedCdp.PENDING,
                       _ScriptedCdp.READY)
    pdfmod._wait_until_rendered(cdp, "session-1", timeout=10.0)
    assert cdp.polls >= 4          # it kept waiting through every pending sample


def test_wait_returns_once_the_report_page_has_committed():
    cdp = _ScriptedCdp(_ScriptedCdp.READY)
    pdfmod._wait_until_rendered(cdp, "session-1", timeout=10.0)
    assert cdp.polls == 1


def test_wait_raises_instead_of_printing_a_blank_page():
    """A page that never arrives must fail loudly — a blank PDF download is the one
    outcome that must not happen silently."""
    cdp = _ScriptedCdp(_ScriptedCdp.PENDING)
    with pytest.raises(PdfRenderError) as exc:
        pdfmod._wait_until_rendered(cdp, "session-1", timeout=0.3)
    assert "about:blank" in str(exc.value)


def test_wait_rejects_a_committed_but_empty_body():
    """Committed and complete, but nothing on the page — still not printable."""
    empty = ("complete", "http://127.0.0.1:8000/print", 0)
    with pytest.raises(PdfRenderError) as exc:
        pdfmod._wait_until_rendered(_ScriptedCdp(empty), "session-1", timeout=0.3)
    assert "0 chars" in str(exc.value)
    # ...unless the caller explicitly prints a page that may legitimately be empty.
    pdfmod._wait_until_rendered(_ScriptedCdp(empty), "session-1", timeout=0.3,
                                require_content=False)


def test_wait_refuses_to_print_an_error_page():
    """An error page has text, so it passes every readiness condition. It must be
    refused on its status instead — at once, not after the timeout."""
    denied = ("complete", "http://127.0.0.1:8000/p/x/print?species=fox&print_ticket=SPENT", 350, 401)
    cdp = _ScriptedCdp(denied)
    with pytest.raises(PdfRenderError) as exc:
        pdfmod._wait_until_rendered(cdp, "session-1", timeout=10.0)
    assert "401" in str(exc.value)
    assert "SPENT" not in str(exc.value), "the query string must not reach the message"
    assert cdp.polls == 1


def test_wait_accepts_a_successful_status():
    ok = ("complete", "http://127.0.0.1:8000/print", 4200, 200)
    cdp = _ScriptedCdp(ok)
    pdfmod._wait_until_rendered(cdp, "session-1", timeout=10.0)
    assert cdp.polls == 1


def test_wait_tolerates_a_document_still_loading():
    loading = ("loading", "http://127.0.0.1:8000/print", 0)
    cdp = _ScriptedCdp(loading, loading, _ScriptedCdp.READY)
    pdfmod._wait_until_rendered(cdp, "session-1", timeout=10.0)
    assert cdp.polls == 3


# ---- end-to-end through a real headless browser -----------------------------
class _SlowPageHandler(BaseHTTPRequestHandler):
    """Serves the marker page only after a delay — a stand-in for the LLM-backed
    report types, whose /print route generates before it responds."""

    delay = 2.0
    marker = "REPORTBODYMARKERTEXT"

    def do_GET(self):
        time.sleep(self.delay)
        body = (f"<!doctype html><html><head><meta charset='utf-8'><title>t</title></head>"
                f"<body><main id='report'><h1>{self.marker}</h1>"
                f"<p>Alert events: 787</p></main></body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):        # keep the test output clean
        pass


@pytest.mark.browser
def test_slow_page_body_reaches_the_pdf():
    """The end-to-end guard: a page that takes seconds to answer must still print
    with its body, not as a footer over blank paper.

    Skipped where no Chrome/Edge or no PDF reader is available — it is the only
    check here that needs a real browser, and the unit tests above cover the same
    condition offline.
    """
    fitz = pytest.importorskip("fitz", reason="pymupdf is needed to read the PDF back")
    from ttr.web.browser import find_browser
    try:
        find_browser(None)
    except BrowserNotFound as exc:
        pytest.skip(f"no headless browser available: {exc}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowPageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        pdf_bytes = pdfmod.render_url_to_pdf(
            f"http://127.0.0.1:{server.server_address[1]}/print",
            footer_html=("<div style='font-size:8px'>FOOTERMARKER "
                         "<span class='pageNumber'></span></div>"),
            render_timeout=30.0)
    finally:
        server.shutdown()

    text = "\n".join(page.get_text() for page in fitz.open(stream=pdf_bytes, filetype="pdf"))
    assert _SlowPageHandler.marker in text, (
        "the report body is missing from the PDF — the renderer printed before the "
        "page arrived (the blank-PDF regression)")
    assert "Alert events: 787" in text          # body content, not just the heading
    assert "FOOTERMARKER" in text               # the footer still works


class _RefusingHandler(BaseHTTPRequestHandler):
    """Answers every request 401 with a readable page, like the token gate."""

    def do_GET(self):
        body = b"<!doctype html><body><h1>Session token required</h1></body>"
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _require_browser():
    from ttr.web.browser import find_browser
    try:
        find_browser(None)
    except BrowserNotFound as exc:
        pytest.skip(f"no headless browser available: {exc}")


@pytest.mark.browser
def test_a_real_browser_refuses_to_print_an_error_page():
    _require_browser()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RefusingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(PdfRenderError) as exc:
            pdfmod.render_url_to_pdf(f"http://127.0.0.1:{server.server_address[1]}/print",
                                     footer_html="<span></span>", render_timeout=20.0)
    finally:
        server.shutdown()
    assert "401" in str(exc.value)


@pytest.mark.browser
def test_report_pdf_prints_the_report_not_the_token_refusal(tmp_path, monkeypatch):
    """The end-to-end regression: the real app behind its real session gate, a
    real browser, and the text of the PDF that comes back.

    Every other PDF test mocks the renderer or serves its own page, which is how a
    PDF of the "Session token required" page shipped as a 200 application/pdf.
    """
    fitz = pytest.importorskip("fitz", reason="pymupdf is needed to read the PDF back")
    _require_browser()
    import httpx
    import uvicorn

    from conftest import _TEST_UI_TOKEN, FakeLLM, make_web_project
    from ttr.web import auth

    ctx = make_web_project(tmp_path, monkeypatch, name="PDF Browser Site")
    app.dependency_overrides[appmod.get_llm] = lambda: FakeLLM("A steady week.")
    auth.set_ui_token(_TEST_UI_TOKEN)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    params = {"species": appmod.ALL_SPECIES, "start": "2026-06-28", "end": "2026-07-05"}
    cookies = {auth.SESSION_COOKIE: _TEST_UI_TOKEN}
    try:
        deadline = time.time() + 20
        while not server.started:
            assert time.time() < deadline, "test server did not start"
            time.sleep(0.05)
        base = f"http://127.0.0.1:{port}/p/{ctx.id}"
        printed = httpx.get(f"{base}/print", params=params, cookies=cookies, timeout=60)
        response = httpx.get(f"{base}/api/report.pdf", params=params, cookies=cookies,
                             timeout=180)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.clear()
        auth.set_ui_token(None)

    assert response.status_code == 200, response.text[:300]
    assert response.headers["content-type"] == "application/pdf"
    squash = lambda s: " ".join(s.split())
    text = squash(" ".join(p.get_text() for p in fitz.open(stream=response.content,
                                                            filetype="pdf")))
    assert "token required" not in text.lower(), "the PDF is the auth refusal page"

    heading = re.search(r"<h[12][^>]*>(.*?)</h[12]>", printed.text, re.S)
    assert heading, "the print page has no heading to look for"
    expected = squash(html.unescape(re.sub(r"<[^>]+>", "", heading.group(1))))
    assert expected and expected in text, f"report heading {expected!r} missing from the PDF"
    assert not auth._print_tickets, "the render's ticket outlived it"


# ---- low-level helpers -----------------------------------------------------
def test_free_port_returns_a_bindable_port():
    p = _free_port()
    assert isinstance(p, int) and 1 <= p <= 65535


def test_rmtree_retry_deletes_and_never_raises(tmp_path):
    d = tmp_path / "profile"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "f.txt").write_text("x")
    _rmtree_retry(d)
    assert not d.exists()
    _rmtree_retry(d)                       # already gone — must not raise


def test_terminate_tree_on_exited_process_is_noop():
    proc = subprocess.Popen(["cmd", "/c", "exit", "0"] if _is_windows() else ["true"])
    proc.wait()
    _terminate_tree(proc)                  # already exited — must not raise


def _is_windows() -> bool:
    import sys
    return sys.platform == "win32"
