"""The session-token gate: what it closes, and what it does not.

`tests/conftest.py` gives every other web suite an authenticated client, so those
tests exercise routes rather than the gate. This file is the gate's own coverage —
and it is written to be specific about the bound, because a security control
described more broadly than it works is worse than none.

The claim being tested is narrow: **no route on this server answers without the
token.** Not that the UI is safe to expose, not that a password typed into it is
protected from the browser, not that plain HTTP on loopback is private. Those
belong to `web/auth.py`'s docstring and to the records, and two tests here pin the
parts that ARE code: that the token does not survive into a log line, and that it
leaves the address bar as soon as it has been exchanged.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from ttr.web import auth
from ttr.web.app import app

TOKEN = "a-test-token-that-is-not-a-real-session"

#: One of every SHAPE of route: the app-level picker, a project page, a read API,
#: and the two that WRITE. If the gate is ever bypassed it will not be uniformly.
ROUTES = [
    ("GET", "/"),
    ("GET", "/p/some-project/"),
    ("GET", "/p/some-project/api/species"),
    ("GET", "/p/some-project/ingest"),
    ("POST", "/p/some-project/api/report"),
    ("POST", "/p/some-project/api/ingest/start"),
    ("POST", "/p/some-project/api/ingest/stop"),
]


@pytest.fixture
def armed():
    """A server with a token configured, and a client holding nothing."""
    auth.set_ui_token(TOKEN)
    yield TestClient(app)
    auth.set_ui_token(None)


@pytest.fixture
def disarmed():
    auth.set_ui_token(None)
    yield TestClient(app)


# --------------------------------------------------------------------------- #
# What it closes: the endpoints, to an unauthenticated local caller.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("method,path", ROUTES)
def test_no_route_answers_without_the_token(armed, method, path):
    response = armed.request(method, path)

    assert response.status_code == 401, f"{method} {path} answered unauthenticated"
    assert "Session token required" in response.text


def test_the_write_endpoint_is_refused_before_it_can_write(armed):
    """The reason this stage exists, asserted by name.

    `/api/ingest/start` polls a real mailbox and stores what it finds. Before the
    token, any process on this machine could fire it.
    """
    response = armed.post("/p/some-project/api/ingest/start", json={})

    assert response.status_code == 401
    assert "Session token required" in response.text


def test_a_wrong_token_is_refused(armed):
    assert armed.get("/?token=not-the-token").status_code == 401

    armed.cookies.set(auth.SESSION_COOKIE, "not-the-token")
    assert armed.get("/").status_code == 401, "a forged cookie must not pass"


def test_an_unconfigured_server_refuses_everything_rather_than_serving_openly(disarmed):
    """Fail closed.

    Handing the app object to a bare ASGI server must not be a way to get the old
    unauthenticated behaviour back by accident.
    """
    response = disarmed.get("/")

    assert response.status_code == 503
    assert "no session token" in response.text


# --------------------------------------------------------------------------- #
# The exchange, and what it does with the token afterwards.
# --------------------------------------------------------------------------- #
def test_the_token_is_exchanged_for_a_cookie_and_leaves_the_url(armed):
    response = armed.get(f"/?token={TOKEN}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/", "the token must not survive the redirect"

    cookie = response.cookies.get(auth.SESSION_COOKIE)
    assert cookie == TOKEN
    set_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in set_cookie, "page script must not be able to read it"
    assert "samesite=strict" in set_cookie, "another origin must not be able to ride it"


def test_the_exchange_keeps_other_query_parameters(armed):
    """Only the token is stripped; a deep link still works."""
    response = armed.get(f"/p/x/print?species=fox&{auth.TOKEN_PARAM}={TOKEN}&start=2026-01-01",
                         follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    assert "species=fox" in location and "start=2026-01-01" in location
    assert auth.TOKEN_PARAM not in location


def test_the_session_persists_across_requests(armed):
    armed.get(f"/?token={TOKEN}")                    # follows the redirect, keeps the cookie

    assert armed.get("/").status_code == 200, "the cookie should carry the session"


# --------------------------------------------------------------------------- #
# The log. uvicorn records the full request line, query string included.
# --------------------------------------------------------------------------- #
def test_the_token_is_redacted_from_a_log_line():
    """Verified against the real shape of a uvicorn access record.

    uvicorn logs via `%s` args rather than a formatted message, so a filter that
    only rewrote `record.msg` would miss it entirely — which is why this asserts
    on args.
    """
    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg='%s - "%s %s HTTP/%s" %d', args=(
            "127.0.0.1:1234", "GET", f"/?token={TOKEN}", "1.1", 303), exc_info=None)

    assert auth.RedactTokenFilter().filter(record) is True
    rendered = record.getMessage()
    assert TOKEN not in rendered, "the token reached the log"
    assert "token=[redacted]" in rendered
    assert "GET" in rendered and "303" in rendered, "the useful part survives"


def test_redaction_leaves_an_ordinary_line_alone():
    assert auth.redact_token('GET /p/x/api/species HTTP/1.1') == 'GET /p/x/api/species HTTP/1.1'


def test_redaction_handles_the_token_among_other_parameters():
    out = auth.redact_token(f"/print?species=fox&token={TOKEN}&start=2026-01-01")

    assert TOKEN not in out
    assert "species=fox" in out and "start=2026-01-01" in out


# --------------------------------------------------------------------------- #
# The print ticket: the headless PDF browser's way in, and how far it reaches.
# --------------------------------------------------------------------------- #
PRINT_PATH = "/p/some-project/print"


@pytest.fixture
def tickets():
    auth._print_tickets.clear()
    yield
    auth._print_tickets.clear()


def _with_ticket(path, ticket):
    return f"{path}?species=fox&{auth.PRINT_TICKET_PARAM}={ticket}"


def test_a_print_ticket_opens_its_print_page_once(armed, tickets):
    ticket = auth.mint_print_ticket(PRINT_PATH)

    first = armed.get(_with_ticket(PRINT_PATH, ticket))
    # Past the gate: the project does not exist, so the ROUTE answers, not auth.
    assert first.status_code != 401 and "Session token required" not in first.text
    assert auth.SESSION_COOKIE not in first.cookies, "a ticket must never become a session"

    again = armed.get(_with_ticket(PRINT_PATH, ticket))
    assert again.status_code == 401, "a ticket is single-use"


@pytest.mark.parametrize("path", ["/", "/p/some-project/api/species",
                                  "/p/some-project/ingest", "/p/another-project/print"])
def test_a_print_ticket_opens_nothing_else(armed, tickets, path):
    ticket = auth.mint_print_ticket(PRINT_PATH)

    assert armed.get(_with_ticket(path, ticket)).status_code == 401


def test_a_ticket_presented_on_the_wrong_path_is_spent(armed, tickets):
    """Seen where it should not be, it is treated as compromised."""
    ticket = auth.mint_print_ticket(PRINT_PATH)

    armed.get(_with_ticket("/", ticket))
    assert armed.get(_with_ticket(PRINT_PATH, ticket)).status_code == 401


def test_a_print_ticket_does_not_authorise_a_write_method(armed, tickets):
    ticket = auth.mint_print_ticket(PRINT_PATH)

    assert armed.post(_with_ticket(PRINT_PATH, ticket)).status_code == 401


def test_an_expired_or_discarded_print_ticket_is_refused(armed, tickets):
    expired = auth.mint_print_ticket(PRINT_PATH, ttl=-1.0)
    assert armed.get(_with_ticket(PRINT_PATH, expired)).status_code == 401

    discarded = auth.mint_print_ticket(PRINT_PATH)
    auth.discard_print_ticket(discarded)
    assert armed.get(_with_ticket(PRINT_PATH, discarded)).status_code == 401


def test_a_forged_print_ticket_is_refused(armed, tickets):
    auth.mint_print_ticket(PRINT_PATH)

    assert armed.get(_with_ticket(PRINT_PATH, "not-a-ticket")).status_code == 401


def test_a_print_ticket_is_worthless_on_an_unconfigured_server(disarmed, tickets):
    ticket = auth.mint_print_ticket(PRINT_PATH)

    assert disarmed.get(_with_ticket(PRINT_PATH, ticket)).status_code == 503


def test_a_print_ticket_is_redacted_from_a_log_line():
    out = auth.redact_token(f"/p/x/print?species=fox&{auth.PRINT_TICKET_PARAM}=abc123XYZ")

    assert "abc123XYZ" not in out
    assert f"{auth.PRINT_TICKET_PARAM}=[redacted]" in out and "species=fox" in out


# --------------------------------------------------------------------------- #
# The token itself.
# --------------------------------------------------------------------------- #
def test_each_mint_is_a_fresh_high_entropy_token():
    tokens = {auth.mint_token() for _ in range(64)}

    assert len(tokens) == 64, "tokens repeated"
    assert all(len(t) >= 40 for t in tokens), "shorter than 32 bytes of entropy"


def test_a_fixed_token_is_read_from_the_environment():
    fixed = auth.mint_token()

    assert auth.fixed_token_from({auth.UI_TOKEN_ENV: f"  {fixed}\n"}) == fixed
    assert auth.fixed_token_from({}) is None
    assert auth.fixed_token_from({auth.UI_TOKEN_ENV: "   "}) is None


@pytest.mark.parametrize("weak", [
    "too-short",
    "a" * 31,
    "a" * 31 + "+",                    # becomes a space in a query string
    "a" * 32 + "&x=1",                 # would split the query string
])
def test_a_weak_or_unusable_fixed_token_is_refused_rather_than_ignored(weak):
    """A silent fallback to a minted token would strand the bookmark it was set for."""
    with pytest.raises(auth.InvalidFixedToken):
        auth.fixed_token_from({auth.UI_TOKEN_ENV: weak})


def test_comparison_is_constant_time_and_rejects_empties():
    auth.set_ui_token(TOKEN)
    try:
        assert auth._matches(TOKEN) is True
        assert auth._matches("") is False
        assert auth._matches(None) is False
        auth.set_ui_token(None)
        assert auth._matches(TOKEN) is False, "nothing matches when nothing is configured"
    finally:
        auth.set_ui_token(None)
