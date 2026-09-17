"""Creating a project from the picker page.

Cut from v1 on the grounds that a mailbox app password must not be POSTed to an
endpoint any local process could reach. That exclusion was conditional and the
condition has been met: `ttr serve` is behind a session token
(`tests/test_ui_auth.py`), so the endpoint is no longer open to anything else on
the machine.

Two properties carry most of the weight here, and neither is cosmetic.

**The secret travels in the BODY.** uvicorn's access log records the full request
line including the query string and does not record bodies — audited by probe, not
assumed. A creation form built as a GET, or with the password in a query string,
would write it to the log in clear. The route is POST-only and that is asserted.

**Nothing is reimplemented.** The route calls the same `create_project` the CLI
calls, so the Gmail-only refusal, the validate-then-verify-then-write ordering and
the "a failure leaves nothing behind" guarantee are one implementation. The tests
below check the route honours them rather than re-testing the service.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ttr.projects import paths as ppaths
from ttr.projects.registry import Registry
from ttr.web import auth
from ttr.web.app import app

TOKEN = "a-test-token-for-the-create-form"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(tmp_path))
    auth.set_ui_token(TOKEN)
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, TOKEN)
    yield c
    auth.set_ui_token(None)


def _names(root):
    return [e.name for e in Registry.load(root).entries]


# --------------------------------------------------------------------------- #
# The gate comes first: this route writes.
# --------------------------------------------------------------------------- #
def test_creation_is_refused_without_the_session_token(tmp_path, monkeypatch):
    """The precondition for the whole feature existing."""
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(tmp_path))
    auth.set_ui_token(TOKEN)
    try:
        anonymous = TestClient(app)
        response = anonymous.post("/api/projects",
                                  json={"name": "Sneaky", "email": "s@gmail.com"})

        assert response.status_code == 401
        assert _names(tmp_path) == [], "an unauthenticated call created a project"
    finally:
        auth.set_ui_token(None)


def test_the_route_is_post_only(client):
    """A GET form would put the password in the query string, and uvicorn logs
    query strings. This must never become a GET."""
    assert client.get("/api/projects").status_code == 405


# --------------------------------------------------------------------------- #
# Creating.
# --------------------------------------------------------------------------- #
def test_a_project_is_created_without_a_password_and_says_what_is_missing(client, tmp_path):
    response = client.post("/api/projects", json={
        "name": "North Field", "email": "alerts@gmail.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "North Field"
    assert "NOT verified" in body["message"], "an unverified mailbox must be stated"
    assert "ttr project set-password" in body["message"], "and the next step given"
    assert _names(tmp_path) == ["North Field"]


def test_the_site_is_set_when_coordinates_are_given(client, tmp_path):
    client.post("/api/projects", json={
        "name": "Sited", "email": "s@gmail.com", "site_name": "North Field",
        "latitude": "54.9783", "longitude": "-1.6178"})

    entry = Registry.load(tmp_path).entries[0]
    from ttr.projects.manifest import ProjectManifest
    manifest = ProjectManifest.read(tmp_path / "projects" / entry.directory)
    assert manifest.site.name == "North Field"
    assert manifest.site.latitude == pytest.approx(54.9783)
    assert manifest.site.longitude == pytest.approx(-1.6178)


# --------------------------------------------------------------------------- #
# Refusals. The route must surface the service's reasons, not invent its own.
# --------------------------------------------------------------------------- #
def test_a_microsoft_address_is_refused_with_the_services_own_reason(client, tmp_path):
    response = client.post("/api/projects", json={
        "name": "MS", "email": "someone@outlook.com"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert "Microsoft" in error
    assert "16 September 2024" in error, "the route must not flatten the explanation"
    assert _names(tmp_path) == [], "a refused create left something behind"


def test_an_unknown_provider_is_refused(client, tmp_path):
    response = client.post("/api/projects", json={
        "name": "Odd", "email": "someone@wibble.example"})

    assert response.status_code == 400
    assert "Gmail" in response.json()["error"]
    assert _names(tmp_path) == []


def test_a_nameless_project_is_refused(client, tmp_path):
    response = client.post("/api/projects", json={"name": "  ", "email": "a@gmail.com"})

    assert response.status_code == 400
    assert _names(tmp_path) == []


@pytest.mark.parametrize("payload,expected", [
    ({"latitude": "54.9"}, "both latitude and longitude"),
    ({"longitude": "-1.6"}, "both latitude and longitude"),
    ({"latitude": "north", "longitude": "2"}, "must be numbers"),
])
def test_coordinate_errors_are_caught_before_anything_is_written(client, tmp_path,
                                                                 payload, expected):
    """Checked BEFORE `create_project`, so a typo in a coordinate does not leave a
    half-configured project to clean up."""
    response = client.post("/api/projects",
                           json={"name": "Coords", "email": "c@gmail.com", **payload})

    assert response.status_code == 400
    assert expected in response.json()["error"]
    assert _names(tmp_path) == [], "a coordinate typo created a project"


# --------------------------------------------------------------------------- #
# The page itself.
# --------------------------------------------------------------------------- #
def test_the_picker_now_offers_the_form(client):
    body = client.get("/").text

    assert "<form" in body and 'id="create-form"' in body
    assert 'type="password"' in body, "the password field is the point of the stage"
    assert 'autocomplete="new-password"' in body, (
        "autofill is the exposure this stage does NOT close; asking the browser not "
        "to offer a saved password is the little that can be done about it")


def test_the_form_posts_and_never_builds_a_query_string(client):
    """The audited property, asserted against the page that has to honour it."""
    body = client.get("/").text

    assert 'method: "POST"' in body
    assert "JSON.stringify(data)" in body, "the payload must be a body"
    assert "?password=" not in body and "&password=" not in body


def test_the_form_is_offered_even_with_no_projects_yet(client):
    """The empty state is exactly when someone needs it.

    It used to be a separate "No projects yet" panel. The redesigned page has no
    separate empty state at all: the create tile is the last cell of the grid in
    every case, so with nothing to list it is the only cell, and the hero and the
    browser-or-terminal panel are unconditional. Same guarantee — you can create a
    project from a page with no projects on it — asserted against what the page
    now actually shows.
    """
    body = client.get("/").text

    assert "Create a project" in body                  # the tile, now the empty state
    assert 'class="tile"' in body
    assert 'id="create-form"' in body
    assert "Browser or terminal?" in body, "the caveats are not conditional"
