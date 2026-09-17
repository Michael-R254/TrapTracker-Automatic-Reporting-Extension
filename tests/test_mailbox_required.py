"""A project with no inbox refuses to ingest, and says so.

A mailbox-less project is a real state: one opened from a published extract has
no inbox behind it and reports on the rows it holds. What it cannot do is poll.

Until 2026-09-16 `ttr run` met that state inside the fetcher, as

    ValueError: EmailFetcher needs host, user and a password callable

- the three constructor arguments it was missing, which is an internal assertion
rather than an answer, printed as a traceback. The ingest PAGE had always said it
properly (`tests/test_extract_loader.py` pins that, and recorded this message as
worth improving). One raise site now serves every entry point.

The mailbox-less project here is made by blanking a normal project's [mailbox]
through the manifest, so these tests do not depend on how any particular command
creates one.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from ttr.cli import app
from ttr.projects import paths as ppaths
from ttr.projects.errors import MailboxNotConfigured, ProjectError
from ttr.projects.manifest import ProjectManifest
from ttr.projects.registry import Registry
from ttr.projects.service import context_for, create_project

runner = CliRunner()


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(r))
    return r


def _project(root, name="Configured"):
    manifest, _ = create_project(name, f"{name.lower()}@gmail.com", root=root,
                                 skip_connection_test=True)
    return context_for(Registry.load(root).get(manifest.id), root)


def _mailboxless(root, name="Extract Only"):
    """A project whose [mailbox] is empty, as an extract-loaded one is."""
    ctx = _project(root, name)
    manifest = ProjectManifest.read(ctx.dir)
    manifest.mailbox.address = ""
    manifest.mailbox.imap_host = ""
    manifest.mailbox.imap_user = ""
    manifest.write(ctx.dir)
    return context_for(Registry.load(root).get(ctx.id), root)


# --------------------------------------------------------------------------- #
# The state itself.
# --------------------------------------------------------------------------- #
def test_a_project_knows_whether_it_has_an_inbox(root):
    assert _project(root).mailbox_configured() is True
    assert _mailboxless(root).mailbox_configured() is False


def test_requiring_a_mailbox_is_silent_when_there_is_one(root):
    assert _project(root).require_mailbox() is None


def test_the_refusal_names_the_project_the_file_and_the_next_step(root):
    ctx = _mailboxless(root)

    with pytest.raises(MailboxNotConfigured) as exc:
        ctx.require_mailbox()

    message = str(exc.value)
    assert "Extract Only" in message
    assert "cannot ingest" in message
    assert "project.toml" in message
    assert "set-password" in message or "TTR_IMAP_PASSWORD" in message
    assert "EmailFetcher" not in message, "no internal assertion in a user message"


# --------------------------------------------------------------------------- #
# Every entry point.
# --------------------------------------------------------------------------- #
def test_the_pipeline_refuses_before_building_anything(root):
    from ttr.pipeline import build_pipeline_for

    with pytest.raises(MailboxNotConfigured):
        build_pipeline_for(_mailboxless(root), projects_in_scope=1)


def test_ttr_run_says_it_in_one_line(root):
    _mailboxless(root)

    result = runner.invoke(app, ["run", "--once"])

    assert result.exit_code == 2, result.output
    assert "error: No mailbox is configured" in result.output
    assert "Traceback" not in result.output
    assert "EmailFetcher needs host" not in result.output


def test_ttr_ingest_says_it_in_one_line(root):
    _mailboxless(root)

    result = runner.invoke(app, ["ingest"])

    assert result.exit_code == 2, result.output
    assert "error: No mailbox is configured" in result.output
    assert "Traceback" not in result.output


def test_the_ingest_console_shows_the_message_not_the_class_name(root):
    """`friendly_error` passes a project-layer error through: it is already one
    actionable line, and prefixing it with `MailboxNotConfigured:` would only
    add noise."""
    from ttr.web.ingest import friendly_error

    ctx = _mailboxless(root)
    try:
        ctx.require_mailbox()
    except ProjectError as exc:
        rendered = friendly_error(exc)

    assert rendered.startswith("No mailbox is configured")
    assert "MailboxNotConfigured" not in rendered
