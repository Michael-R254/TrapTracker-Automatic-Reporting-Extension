"""Render a URL to PDF with headless Chrome/Edge over the DevTools Protocol.

Uses ``Page.printToPDF`` — NOT the ``--print-to-pdf`` CLI flag, which cannot supply
a custom "Page X of Y" footer. A small, self-contained CDP client (over the
``websocket-client`` package) drives an already-installed browser; the report's own
HTML/CSS is untouched. The browser process and its temporary user-data directory
are always cleaned up, even on error.
"""

from __future__ import annotations

import base64
import contextlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from websocket import create_connection

from ..logging import get_logger
from .browser import find_browser

logger = get_logger(__name__)


class PdfRenderError(RuntimeError):
    """The browser could not be launched or the page could not be printed."""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CdpSession:
    """Minimal one-socket CDP client (flatten mode: one browser socket, per-target
    ``sessionId``). Command/response only — events seen while awaiting a response id
    are skipped, which is sufficient for the linear print flow."""

    def __init__(self, ws_url: str, timeout: float = 60.0) -> None:
        self._ws = create_connection(ws_url, timeout=timeout, max_size=None)
        self._id = 0

    def call(self, method: str, params: dict | None = None,
             session_id: str | None = None, timeout: float = 60.0) -> dict:
        self._id += 1
        msg: dict = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self._ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise PdfRenderError(f"CDP timeout waiting for {method}")
            self._ws.settimeout(remaining)
            data = json.loads(self._ws.recv())
            if data.get("id") == self._id:
                if "error" in data:
                    raise PdfRenderError(f"CDP {method} failed: {data['error']}")
                return data.get("result", {})
            # otherwise it is an event or another response — skip it

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ws.close()


def _wait_for_devtools(port: int, timeout: float) -> str:
    """Poll the DevTools /json/version endpoint until the browser WS URL is ready."""
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=1.0
            ) as resp:
                return json.load(resp)["webSocketDebuggerUrl"]
        except Exception as exc:  # not yet listening
            last_err = exc
            time.sleep(0.15)
    raise PdfRenderError(
        f"Browser DevTools endpoint on port {port} did not become ready "
        f"within {timeout:.0f}s ({last_err})")


#: Readiness probe, evaluated in the target tab:
#: [readyState, url, body-text length, HTTP status of the document].
#: ``innerText`` needs layout, so it also reports whether the page has actually
#: painted; ``textContent`` is the fallback if layout is unavailable. The status
#: comes from Navigation Timing's ``responseStatus`` — measured on Chrome 152 to
#: report 401 for a refused page — and reads 0 where the API is absent.
_READY_PROBE = (
    "JSON.stringify(["
    "  document.readyState,"
    "  location.href,"
    "  (document.body ? (document.body.innerText || document.body.textContent || '') : '')"
    "    .trim().length,"
    "  ((performance.getEntriesByType('navigation')[0] || {}).responseStatus || 0)"
    "])"
)


def _wait_until_rendered(cdp: _CdpSession, session_id: str, timeout: float,
                         *, require_content: bool = True) -> None:
    """Wait for the REQUESTED document to commit and finish loading, then for fonts
    and a short settle so inline SVG charts and embedded (data-URI) images have painted.

    ``document.readyState`` alone is NOT a readiness signal here. Until the server's
    response arrives the tab still holds its initial ``about:blank`` document — which
    is already ``"complete"``. Polling readyState therefore returned on the very first
    sample and ``Page.printToPDF`` printed the *empty* tab: a PDF with the footer but
    no report body. A page that answered within the settle window won that race by
    luck; a slower one (a report whose generation includes an LLM call) always lost it.

    Readiness is therefore three conditions, not one: the navigation has left
    ``about:blank``, the document is ``complete``, and — unless ``require_content`` is
    False — the body actually carries text. Exhausting ``timeout`` raises rather than
    printing whatever happens to be on screen, so a failure is reported instead of
    silently downloaded as a blank report.

    A committed document the server answered with an HTTP error is refused outright.
    An error page has text, so it passes every readiness condition — which is how
    the token-refusal page was downloaded as a report until 2026-09-15.
    """
    deadline = time.time() + timeout
    started = time.time()
    state = href = None
    text_len = status = 0
    while True:
        result = cdp.call("Runtime.evaluate",
                          {"expression": _READY_PROBE, "returnByValue": True},
                          session_id=session_id)
        raw = result.get("result", {}).get("value")
        if isinstance(raw, str):
            try:
                values = json.loads(raw)
                state, href, text_len = values[:3]
                status = values[3] if len(values) > 3 else 0
            except (ValueError, TypeError):        # pragma: no cover - defensive
                state = href = None
            committed = bool(href) and not str(href).startswith("about:")
            if committed and status >= 400:
                # The query string is dropped from the message: it can carry a
                # print ticket, and this text reaches a log and a response body.
                raise PdfRenderError(
                    f"the page answered HTTP {status} "
                    f"({str(href).split('?', 1)[0]}), so it was not printed. "
                    "No PDF was produced, rather than one of an error page.")
            if committed and state == "complete" and (text_len > 0 or not require_content):
                # DEBUG, so a normal run stays quiet but a blank-page report can be
                # settled from the log alone: what was on screen at the moment of print.
                logger.debug("pdf_page_ready",
                             extra={"url": href, "ready_state": state,
                                    "body_text_chars": text_len,
                                    "waited_s": round(time.time() - started, 2)})
                break
        if time.time() >= deadline:
            raise PdfRenderError(
                f"page did not finish rendering within {timeout:.0f}s "
                f"(readyState={state!r}, url={href!r}, body text={text_len} chars). "
                "No PDF was produced, rather than a blank one.")
        time.sleep(0.1)
    # Fonts (avoids a flash of fallback text in the PDF); tolerate absence of the API.
    with contextlib.suppress(Exception):
        cdp.call("Runtime.evaluate",
                 {"expression": "document.fonts ? document.fonts.ready.then(() => true) : true",
                  "awaitPromise": True, "returnByValue": True},
                 session_id=session_id, timeout=10.0)
    time.sleep(0.25)


def render_url_to_pdf(
    url: str,
    *,
    footer_html: str,
    header_html: str = "<span></span>",
    browser_override: str | None = None,
    launch_timeout: float = 25.0,
    render_timeout: float = 30.0,
    print_timeout: float = 60.0,
) -> bytes:
    """Print ``url`` to PDF bytes via headless Chrome/Edge and a controlled footer.

    ``render_timeout`` bounds the whole wait for the page: the server's own response
    time as well as loading and painting. When the page is generated on demand (the
    report's ``/print`` route runs the report generator, LLM call included), the
    caller must size this from that generation budget — see ``report_pdf``.

    Raises :class:`PdfRenderError` (or ``browser.BrowserNotFound``) on failure —
    including a page that never rendered, which is reported rather than returned as a
    blank PDF. The browser process and temp profile are always torn down.
    """
    executable = find_browser(browser_override)          # BrowserNotFound if none
    port = _free_port()
    user_data_dir = Path(tempfile.mkdtemp(prefix="ttr-pdf-"))
    args = [
        str(executable),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--hide-scrollbars",
        "--force-color-profile=srgb",
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",   # newer Chrome rejects CDP WS without this
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp: _CdpSession | None = None
    try:
        ws_url = _wait_for_devtools(port, launch_timeout)
        cdp = _CdpSession(ws_url, timeout=max(print_timeout, render_timeout))
        target = cdp.call("Target.createTarget", {"url": url})
        target_id = target["targetId"]
        attached = cdp.call("Target.attachToTarget",
                            {"targetId": target_id, "flatten": True})
        session_id = attached["sessionId"]
        cdp.call("Page.enable", session_id=session_id)
        _wait_until_rendered(cdp, session_id, render_timeout)
        result = cdp.call(
            "Page.printToPDF",
            {
                "displayHeaderFooter": True,
                "headerTemplate": header_html,
                "footerTemplate": footer_html,
                "printBackground": True,
                "preferCSSPageSize": True,
                "marginTop": 0.4,
                "marginBottom": 0.7,
                "marginLeft": 0.5,
                "marginRight": 0.5,
                "transferMode": "ReturnAsBase64",
            },
            session_id=session_id,
            timeout=print_timeout,
        )
        with contextlib.suppress(Exception):
            cdp.call("Target.closeTarget", {"targetId": target_id})
        return base64.b64decode(result["data"])
    finally:
        if cdp is not None:
            cdp.close()
        _terminate_tree(proc)
        _rmtree_retry(user_data_dir)


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Kill the browser AND its child processes. Chrome is multi-process, so a bare
    terminate() (Windows TerminateProcess) leaves renderer/gpu children orphaned —
    taskkill /T kills the whole tree. Never raises."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
    else:  # pragma: no cover - non-Windows
        with contextlib.suppress(Exception):
            proc.terminate()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)
    with contextlib.suppress(Exception):
        if proc.poll() is None:
            proc.kill()


def _rmtree_retry(path: Path, attempts: int = 5) -> None:
    """Delete a temp dir, retrying briefly for Windows file locks, never raising."""
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.2)
    shutil.rmtree(path, ignore_errors=True)
