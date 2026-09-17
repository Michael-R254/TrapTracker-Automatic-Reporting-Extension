"""Session-token authentication for the local UI — the Jupyter pattern.

``ttr serve`` mints a single high-entropy token at startup and prints it in the
URL it tells you to open. The first request carrying ``?token=`` is exchanged for
an ``HttpOnly`` session cookie and redirected to the clean path; every route
refuses without that cookie. No accounts, no password, nothing stored.

WHAT THIS CLOSES. Before it, every route on ``127.0.0.1:8000`` was open to any
process running on the machine — and ``/api/ingest/start`` WRITES: it polls the
mailbox and stores what it finds. A local process could trigger a real ingest, or
read every report, with no credential at all. That is what the token ends.

WHAT IT DOES NOT CLOSE, stated because a security control that oversells itself is
worse than none:

* **Browser autofill and history are untouched.** They are browser-side
  behaviour. A password typed into a form in this UI can still be offered back by
  the browser on another page; a token in the address bar can still land in
  history. The redirect below removes the token from the address bar as soon as
  it has been exchanged, which is the only part of that this code can affect.
* **It does not authenticate a *person*.** Anyone holding the token is the user.
  Copying the URL out of the terminal into a chat window hands over access.
* **It is not transport security.** This is plain HTTP on loopback. The token is
  not protected in transit, which is acceptable bound to 127.0.0.1 and is not
  acceptable anywhere else — hence ``--i-understand-no-auth`` still gating a
  non-loopback bind.
* **A same-user process can still read the server's memory.** By default the
  token is held only in the serving process (never an environment variable, never
  a file), so it cannot be read from ``ps`` or a runtime directory — but a
  debugger attached as the same user defeats any of this.
* **A fixed token gives up some of that.** ``TTR_UI_TOKEN`` pins the token so a
  bookmark survives restarts. It is opt-in: the token then sits in an environment
  variable, readable by same-user processes and by ``docker inspect``, and stays
  valid until you change it rather than until the server stops.

FAIL CLOSED. With no token configured the app refuses every request rather than
serving openly. Importing ``ttr.web.app`` and handing it to a bare ASGI server is
therefore not a way to get the old unauthenticated behaviour back by accident.

THE PDF BROWSER GETS A PRINT TICKET, NOT THE SESSION. ``/api/report.pdf`` has a
headless browser fetch this server's own ``/print`` page. That browser holds no
cookie, so until 2026-09-15 it was refused like any other stranger — and printed
the refusal page, served as a 200 ``application/pdf``. Handing it the session
token would fix that and open something worse: the browser's DevTools port is
listening on 127.0.0.1 for the whole render, and any local process could connect
and read the cookie back. So it gets a ticket instead:

* **single use** — consumed by the request that presents it, match or not;
* **one path** — bound to that project's ``/print``, and to GET;
* **short-lived** — discarded when the render ends, and expiring on its own
  if the render never reaches the server.

Stolen off the DevTools port, a ticket buys at most one print of a report that is
already being printed.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from typing import Optional

from starlette.responses import HTMLResponse, RedirectResponse

#: Cookie the browser holds once the token has been exchanged. `HttpOnly` so page
#: script cannot read it, `SameSite=Strict` so another origin cannot ride it.
SESSION_COOKIE = "ttr_session"

#: Query parameter the token arrives on, once, at the start of a session.
TOKEN_PARAM = "token"

#: Set by `ttr serve`. None means "not configured", which means refuse.
_ui_token: Optional[str] = None


def mint_token() -> str:
    """A fresh session token. 32 bytes of `secrets` entropy, URL-safe."""
    return secrets.token_urlsafe(32)


# ------------------------------------------------------------------ fixed token
#: Environment variable that pins the token across restarts, so a bookmarked URL
#: keeps working (above all under Docker, where every `up` restarts the server).
#: Opt-in, and a trade-off: an environment variable is readable by other
#: processes running as this user, and from outside a container by anyone who
#: can run `docker inspect`. Unset, a fresh token is minted each start.
UI_TOKEN_ENV = "TTR_UI_TOKEN"

#: A fixed token is held to the same bar as a minted one: at least 32 URL-safe
#: characters (~190 bits), and only characters that survive a query string
#: unescaped - a `+` or `&` would reach the server as something else.
_FIXED_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,}")


class InvalidFixedToken(ValueError):
    """`TTR_UI_TOKEN` is set, but to something too weak or unusable in a URL."""


def fixed_token_from(environ) -> Optional[str]:
    """The token pinned by `TTR_UI_TOKEN`, or None when it is unset or empty.

    Refuses a weak value rather than quietly minting instead: someone who set it
    expects their bookmark to work, and a silent fallback would leave them at the
    "token required" page with no idea why.
    """
    value = (environ.get(UI_TOKEN_ENV) or "").strip()
    if not value:
        return None
    if not _FIXED_TOKEN.fullmatch(value):
        raise InvalidFixedToken(
            f"{UI_TOKEN_ENV} must be at least 32 characters of A-Z, a-z, 0-9, "
            f"'-' or '_'. Generate one with:\n"
            f"    python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    return value


def set_ui_token(token: Optional[str]) -> None:
    """Configure the token for this process. `None` disarms the server entirely."""
    global _ui_token
    _ui_token = token


def get_ui_token() -> Optional[str]:
    return _ui_token


def _matches(candidate: Optional[str]) -> bool:
    """Constant-time comparison, and never a match when nothing is configured."""
    if not _ui_token or not candidate:
        return False
    return secrets.compare_digest(candidate, _ui_token)


# ------------------------------------------------------------------ print tickets
#: Query parameter the headless PDF browser presents. See the module docstring.
PRINT_TICKET_PARAM = "print_ticket"

#: How long an unused ticket survives. It is spent the moment the browser's
#: request arrives, which is right after launch, so this only has to cover a
#: slow browser start (`render_url_to_pdf`'s launch timeout is 25s) — not report
#: generation, which happens after the ticket has already been used.
PRINT_TICKET_TTL_SECONDS = 60.0

#: ticket -> (the one path it opens, monotonic expiry)
_print_tickets: dict[str, tuple[str, float]] = {}
_print_tickets_lock = threading.Lock()


def mint_print_ticket(path: str, *, ttl: float = PRINT_TICKET_TTL_SECONDS) -> str:
    """A fresh single-use ticket that opens ``path`` once, by GET."""
    ticket = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _print_tickets_lock:
        for stale in [t for t, (_, expiry) in _print_tickets.items() if expiry <= now]:
            del _print_tickets[stale]
        _print_tickets[ticket] = (path, now + ttl)
    return ticket


def discard_print_ticket(ticket: str) -> None:
    """Revoke a ticket, used or not. The caller does this when its render ends."""
    with _print_tickets_lock:
        _print_tickets.pop(ticket, None)


def _consume_print_ticket(candidate: Optional[str], path: str) -> bool:
    """Spend a ticket. True only for a live ticket presented on its own path.

    The ticket is removed on ANY match of the ticket itself, including a wrong
    path: a ticket seen somewhere it should not be is treated as compromised, and
    the render it belonged to fails loudly rather than printing.
    """
    if not _ui_token or not candidate:
        return False
    with _print_tickets_lock:
        found = next((t for t in _print_tickets
                      if secrets.compare_digest(t, candidate)), None)
        if found is None:
            return False
        bound_path, expiry = _print_tickets.pop(found)
    return expiry > time.monotonic() and secrets.compare_digest(bound_path, path)


#: Matches the token or a print ticket wherever it appears in a URL query string,
#: so it can be removed from anything written to a log. See `redact_token`.
_TOKEN_IN_URL = re.compile(rf"([?&](?:{TOKEN_PARAM}|{PRINT_TICKET_PARAM})=)[^&\s\"']+")


def redact_token(text: str) -> str:
    """Replace a token (or print ticket) in a URL with a marker.

    Needed because uvicorn's access log records the full request line INCLUDING
    the query string — verified by probe, not assumed. Without this the one
    request that carries the token writes it to the log in clear, where it
    outlives the session it belongs to. A print ticket is spent before its log
    line is written, so it is redacted for tidiness rather than necessity.
    """
    return _TOKEN_IN_URL.sub(r"\1[redacted]", text)


class RedactTokenFilter:
    """Logging filter that strips the token from uvicorn's access log lines.

    Installed by `ttr serve`. A filter rather than turning the access log off:
    knowing which requests arrived is useful, and the only part that must not
    persist is the token itself.
    """

    def filter(self, record) -> bool:          # noqa: A003 - logging's own name
        if record.args:
            record.args = tuple(
                redact_token(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact_token(record.msg)
        return True


_NO_TOKEN_PAGE = """<!doctype html><meta charset="utf-8">
<title>TrapTracker Reporting Extension — not configured</title>
<body style="font-family:system-ui,'Segoe UI',sans-serif;max-width:40rem;margin:3rem auto;
padding:0 1rem;color:#1b1b1b">
<h1 style="color:#123c29;font-size:1.3rem">This server has no session token</h1>
<p>It was started without one, so it is refusing every request rather than serving
without authentication.</p>
<p>Start it with <code>ttr serve</code>, which mints a token and prints the URL to
open.</p>
</body>"""

_BAD_TOKEN_PAGE = """<!doctype html><meta charset="utf-8">
<title>TrapTracker Reporting Extension — token required</title>
<body style="font-family:system-ui,'Segoe UI',sans-serif;max-width:40rem;margin:3rem auto;
padding:0 1rem;color:#1b1b1b">
<h1 style="color:#123c29;font-size:1.3rem">Session token required</h1>
<p>This UI is open only to whoever holds the token <code>ttr serve</code> printed
when it started. Open the full URL from that terminal, including
<code>?token=…</code>.</p>
<p>Lost it? Stop the server and start it again — a new token is minted each time.</p>
</body>"""


async def session_token_middleware(request, call_next):
    """Refuse anything without the session cookie; exchange `?token=` for one.

    The exchange REDIRECTS to the same path with the parameter removed. That
    keeps the token out of the address bar, out of anything the page later links
    to as a referrer, and out of every subsequent access-log line — the one line
    that does carry it is redacted by `RedactTokenFilter`.
    """
    if _ui_token is None:
        return HTMLResponse(_NO_TOKEN_PAGE, status_code=503)

    if _matches(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)

    # The PDF browser's way in: no redirect and no cookie, just the one request.
    ticket = request.query_params.get(PRINT_TICKET_PARAM)
    if ticket is not None:
        if request.method == "GET" and _consume_print_ticket(ticket, request.url.path):
            return await call_next(request)
        return HTMLResponse(_BAD_TOKEN_PAGE, status_code=401)

    supplied = request.query_params.get(TOKEN_PARAM)
    if _matches(supplied):
        remaining = [(k, v) for k, v in request.query_params.multi_items()
                     if k != TOKEN_PARAM]
        target = request.url.path
        if remaining:
            from urllib.parse import urlencode
            target = f"{target}?{urlencode(remaining)}"
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            SESSION_COOKIE, _ui_token,
            httponly=True,        # page script cannot read it
            samesite="strict",    # another origin cannot ride it
            path="/",
            # NOT `secure`: this is plain HTTP on loopback by design. Setting it
            # would stop the cookie being sent at all and break the UI.
        )
        return response

    return HTMLResponse(_BAD_TOKEN_PAGE, status_code=401)
