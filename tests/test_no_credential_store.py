"""A machine with no OS credential store - a container, a headless host.

The env-var escape hatch has always existed (`credentials.resolve`), but
everything AROUND it assumed a keyring: creating a project with a password
verified the mailbox and then rolled the whole thing back, naming the
environment variable of a project that no longer existed; the project cards
reported "no mailbox credential" for a password supplied through the
environment; and every follow-up line named `ttr project set-password`, which
cannot succeed there.

`backend_available` is monkeypatched to False here. The suite's own keyring is an
in-memory one (`conftest._isolated_keyring`), so without this every test in the
file would run on a machine that HAS a store.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ttr.cli import app as cli_app
from ttr.projects import credentials
from ttr.projects import paths as ppaths
from ttr.projects.errors import ProjectError
from ttr.projects.registry import Registry
from ttr.projects.service import context_for, create_project, set_password
from ttr.web import auth, picker
from ttr.web.app import app

from conftest import mailbox_factory, project_client

runner = CliRunner()
GMAIL_SHAPED = "abcd efgh ijkl mnop"


@pytest.fixture
def no_store(monkeypatch):
    """This machine has no credential store."""
    monkeypatch.setattr(credentials, "backend_available", lambda: False)


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(r))
    return r


def _make(name, root, email=None):
    manifest, _ = create_project(name, email or f"{name.lower()}@gmail.com",
                                 root=root, skip_connection_test=True)
    return manifest


# --------------------------------------------------------------------------- #
# Creating: refused before the mailbox is contacted, and nothing left behind.
# --------------------------------------------------------------------------- #
def test_creating_with_a_password_is_refused_before_the_mailbox_is_contacted(
        root, no_store):
    record = []

    with pytest.raises(credentials.NoCredentialBackend) as exc:
        create_project("North Field", "alerts@gmail.com", GMAIL_SHAPED, root=root,
                       mailbox_factory=mailbox_factory(record=record))

    message = str(exc.value)
    assert "--skip-connection-test" in message, "the way forward must be named"
    assert "TTR_IMAP_PASSWORD__<ID>" in message
    assert "ttr project test-connection" in message
    assert record == [], "the mailbox was contacted for a password with nowhere to go"
    assert Registry.load(root).entries == [], "a refused create left a project behind"
    assert not (root / "projects").exists()


def test_creating_without_a_password_still_works_and_names_the_variable(root, no_store):
    manifest = _make("Offline", root)

    step = credentials.supply_step(manifest.id)
    assert credentials.env_var_for(manifest.id) in step
    assert "set-password" not in step, "a dead end must not be offered"


def test_the_cli_create_note_names_the_variable_not_set_password(root, no_store):
    result = runner.invoke(cli_app, ["project", "create", "--name", "Offline",
                                     "--email", "o@gmail.com",
                                     "--skip-connection-test"])

    assert result.exit_code == 0, result.output
    project_id = Registry.load(root).entries[0].id
    assert credentials.env_var_for(project_id) in result.output
    assert "set-password" not in result.output


def test_set_password_refuses_before_prompting_or_contacting_the_mailbox(root, no_store):
    manifest = _make("Offline", root)
    record = []

    # The CLI, which would otherwise ask for a secret it cannot keep. No input is
    # supplied: if it prompted, it would fail for a different reason than this.
    result = runner.invoke(cli_app, ["project", "set-password", "Offline"])
    assert result.exit_code != 0
    assert "no OS credential store" in result.output
    assert "test-connection" in result.output

    # And the service beneath it, so every caller gets the same answer.
    with pytest.raises(credentials.NoCredentialBackend):
        set_password(manifest.id, GMAIL_SHAPED, root=root,
                     mailbox_factory=mailbox_factory(record=record))
    assert record == []


# --------------------------------------------------------------------------- #
# The web form.
# --------------------------------------------------------------------------- #
TOKEN = "a-test-token-for-the-no-store-form"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(tmp_path))
    auth.set_ui_token(TOKEN)
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, TOKEN)
    yield c
    auth.set_ui_token(None)


def test_the_web_form_refuses_a_password_and_says_what_to_do_instead(client, tmp_path,
                                                                    no_store):
    response = client.post("/api/projects", json={
        "name": "North Field", "email": "alerts@gmail.com",
        "password": GMAIL_SHAPED})

    assert response.status_code == 400
    error = response.json()["error"]
    assert "leave the password empty" in error
    assert "TTR_IMAP_PASSWORD__<ID>" in error
    assert Registry.load(tmp_path).entries == [], "a refused create left a project"


def test_the_web_form_without_a_password_names_the_variable(client, tmp_path, no_store):
    response = client.post("/api/projects", json={
        "name": "North Field", "email": "alerts@gmail.com"})

    assert response.status_code == 200
    message = response.json()["message"]
    project_id = Registry.load(tmp_path).entries[0].id
    assert credentials.env_var_for(project_id) in message
    assert "set-password" not in message


# --------------------------------------------------------------------------- #
# What the pages say.
# --------------------------------------------------------------------------- #
def test_a_card_reports_a_credential_that_comes_from_the_environment(root, no_store,
                                                                    monkeypatch):
    """The keyring-only check said "no mailbox credential" for every project in a
    container, where the password arrives in the environment."""
    manifest = _make("Offline", root)
    monkeypatch.setenv(credentials.env_var_for(manifest.id), "from-the-environment")

    html = picker.render(Registry.load(root), root)

    assert "mailbox credential from the environment" in html
    assert "no mailbox credential" not in html


def test_a_card_with_no_credential_hints_the_variable_not_set_password(root, no_store):
    manifest = _make("Offline", root)

    html = picker.render(Registry.load(root), root)

    assert "no mailbox credential" in html
    assert credentials.env_var_for(manifest.id) in html


def test_the_preflight_tells_the_page_this_machine_cannot_store_a_password(
        tmp_path, monkeypatch, no_store):
    from conftest import make_web_project

    ctx = make_web_project(tmp_path, monkeypatch, name="No Store Site")
    data = project_client(app, ctx).get("/api/ingest/preflight").json()

    assert data["credential_store_available"] is False
    assert data["has_credential"] is False
    assert data["credential_hint"] == credentials.supply_command(ctx.id)
    assert credentials.env_var_for(ctx.id) in data["credential_hint"]


def test_the_preflight_on_a_machine_with_a_store_still_names_set_password(
        tmp_path, monkeypatch):
    from conftest import make_web_project

    ctx = make_web_project(tmp_path, monkeypatch, name="With Store Site")
    data = project_client(app, ctx).get("/api/ingest/preflight").json()

    assert data["credential_store_available"] is True
    assert "set-password" in data["credential_hint"]


def test_the_ingest_page_callout_reads_both_preflight_fields():
    """The callout is client-side, so the only thing a test can hold is that it
    reads what the preflight now sends rather than the hardcoded command."""
    from ttr.web.ingest_page import INGEST_PAGE

    assert "credential_store_available" in INGEST_PAGE
    assert "credential_hint" in INGEST_PAGE
    # ...and the old literal survives as the fallback for a machine WITH a store.
    assert "ttr project set-password" in INGEST_PAGE


def test_the_null_backend_is_not_reported_as_a_working_store(monkeypatch):
    """keyring's null backend class is ALSO called `Keyring`. In a container that
    bare name read as a working credential store, which is the opposite."""
    import keyring
    import keyring.backends.fail as fail_backend

    monkeypatch.setattr(keyring, "get_keyring", lambda: fail_backend.Keyring())

    assert credentials.backend_available() is False
    assert "no store" in credentials.backend_name()
