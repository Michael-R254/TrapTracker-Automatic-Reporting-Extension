"""Stage 2: credential storage, resolution, and the connection test.

Two things this suite must never do, both enforced rather than intended:

  * touch the developer's real credential store. `conftest._isolated_keyring` is
    autouse and installs an in-memory backend for every test in the session;
    `test_the_real_keyring_is_never_reachable` asserts that actually happened.
  * make a network call. Every connection path is driven through
    `conftest.mailbox_factory`, a fake mailbox class.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from ttr.cli import app
from ttr.projects import credentials
from ttr.projects import paths as ppaths
from ttr.projects.connection import (ConnectionOutcome, check_connection,
                                     check_project)
from ttr.projects.errors import ProjectError
from ttr.projects.manifest import ProjectManifest
from ttr.projects.registry import Registry
from ttr.projects.service import (context_for, create_project, delete_project,
                                  set_password)

from conftest import mailbox_factory

runner = CliRunner()

#: Obviously not a real password, so a leak assertion failing is unambiguous.
SENTINEL = "SENT1NEL-must-never-be-printed-4f9c"
GMAIL_SHAPED = "abcd efgh ijkl mnop"


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(r))
    return r


def make_project(name, email="x@gmail.com", *, root, password=GMAIL_SHAPED,
                 messages=7, **kw):
    kw.setdefault("mailbox_factory", mailbox_factory(messages=messages))
    return create_project(name, email, password, root=root, **kw)


def _ctx(root, manifest):
    return context_for(Registry.load(root).get(manifest.id), root)


# --------------------------------------------------------------------------- #
# The suite must not reach the real credential store
# --------------------------------------------------------------------------- #
def test_the_real_keyring_is_never_reachable(keyring_backend):
    """A guard, not a behaviour test.

    If the autouse fixture ever stops applying, this fails loudly rather than the
    suite quietly writing into Windows Credential Manager / Keychain / Secret
    Service.
    """
    import keyring

    from conftest import InMemoryKeyring

    assert isinstance(keyring.get_keyring(), InMemoryKeyring)
    assert type(keyring.get_keyring()).__module__ != "keyring.backends.Windows"

    credentials.store("guard-project", "value")
    # It went to the in-memory store and nowhere else.
    assert keyring_backend.store[(credentials.SERVICE, "guard-project")] == "value"


# --------------------------------------------------------------------------- #
# Whitespace normalisation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("entered,expected", [
    ("abcd efgh ijkl mnop", "abcdefghijklmnop"),      # exactly as Gmail shows it
    ("  abcd efgh ijkl mnop  ", "abcdefghijklmnop"),  # pasted with surrounding space
    ("abcdefghijklmnop", "abcdefghijklmnop"),         # already stripped
    ("abcd\tefgh\nijkl mnop", "abcdefghijklmnop"),    # pasted across a line break
    ("abcd  efgh", "abcdefgh"),                       # doubled space
])
def test_whitespace_is_stripped_from_a_pasted_app_password(entered, expected):
    assert credentials.normalise(entered) == expected


def test_the_stripped_password_is_what_reaches_the_server(root):
    """The four-group form is what Gmail displays and what people paste."""
    record = []
    make_project("Pasted", "p@gmail.com", root=root, password=GMAIL_SHAPED,
                 **{"mailbox_factory": mailbox_factory(record=record)})
    assert record[0]["password"] == "abcdefghijklmnop"


def test_an_empty_password_is_refused(root):
    manifest, _ = make_project("Empty", "e@gmail.com", root=root)
    with pytest.raises(ProjectError):
        set_password(manifest.id, "   ", root=root,
                     mailbox_factory=mailbox_factory())


# --------------------------------------------------------------------------- #
# Resolution order and provenance
# --------------------------------------------------------------------------- #
def test_keyring_is_used_when_no_env_var_is_set(root):
    manifest, _ = make_project("Keyed", "k@gmail.com", root=root)
    resolved = credentials.resolve(manifest.id)
    assert resolved.secret.get_secret_value() == "abcdefghijklmnop"
    assert resolved.source == f"keyring (traptracker-report:{manifest.id})"


def test_the_scoped_env_var_beats_the_keyring(root, monkeypatch):
    manifest, _ = make_project("Overridden", "o@gmail.com", root=root)
    var = credentials.env_var_for(manifest.id)
    monkeypatch.setenv(var, "from-the-environment")

    resolved = credentials.resolve(manifest.id)
    assert resolved.secret.get_secret_value() == "from-the-environment"
    assert resolved.source == f"environment ({var})"


def test_the_unscoped_env_var_works_for_a_single_project(root, monkeypatch):
    manifest, _ = make_project("Solo", "s@gmail.com", root=root)
    monkeypatch.setenv(credentials.ENV_UNSCOPED, "unscoped-secret")

    resolved = credentials.resolve(manifest.id, projects_in_scope=1)
    assert resolved.secret.get_secret_value() == "unscoped-secret"
    assert resolved.source == f"environment ({credentials.ENV_UNSCOPED})"


def test_the_unscoped_env_var_is_refused_when_several_projects_are_in_scope(
        root, monkeypatch):
    """Applying one password to several mailboxes would try project A's
    credential against project B's account. Refuse, don't pick."""
    manifest, _ = make_project("Multi", "m@gmail.com", root=root)
    monkeypatch.setenv(credentials.ENV_UNSCOPED, "unscoped-secret")

    with pytest.raises(credentials.AmbiguousCredentialScope) as exc:
        credentials.resolve(manifest.id, projects_in_scope=2)

    message = str(exc.value)
    assert credentials.ENV_UNSCOPED in message
    assert credentials.env_var_for(manifest.id) in message   # names the fix
    assert "--project" in message


def test_the_scoped_var_stays_valid_with_several_projects_in_scope(root, monkeypatch):
    manifest, _ = make_project("Scoped", "s@gmail.com", root=root)
    monkeypatch.setenv(credentials.env_var_for(manifest.id), "scoped-secret")
    resolved = credentials.resolve(manifest.id, projects_in_scope=5)
    assert resolved.secret.get_secret_value() == "scoped-secret"


def test_describe_source_follows_resolve_for_the_unscoped_var(root, monkeypatch):
    """A diagnostic must not show as available a password the run would refuse."""
    manifest, _ = make_project("Scoped", "s@gmail.com", root=root)   # keyring entry too
    monkeypatch.setenv(credentials.ENV_UNSCOPED, "unscoped-secret")

    assert (credentials.describe_source(manifest.id, projects_in_scope=1)
            == f"environment ({credentials.ENV_UNSCOPED})")
    # `resolve` refuses before it reaches the keyring, so nothing is available.
    assert credentials.describe_source(manifest.id, projects_in_scope=2) is None
    with pytest.raises(credentials.AmbiguousCredentialScope):
        credentials.resolve(manifest.id, projects_in_scope=2)


def test_env_var_name_is_derived_from_the_id():
    var = credentials.env_var_for("4fbc9958-08a6-4f35-bbfc-1e9ee80732e6")
    assert var == "TTR_IMAP_PASSWORD__4FBC9958_08A6_4F35_BBFC_1E9EE80732E6"
    assert var.startswith(credentials.ENV_PREFIX)


def test_the_provenance_line_is_logged_with_the_source_and_not_the_value(
        root, caplog):
    manifest, _ = make_project("Logged", "l@gmail.com", root=root)
    with caplog.at_level(logging.INFO):
        credentials.resolve(manifest.id)

    sources = [r for r in caplog.records if r.msg == "imap_credential_source"]
    assert len(sources) == 1
    assert sources[0].source == f"keyring (traptracker-report:{manifest.id})"
    assert "abcdefghijklmnop" not in caplog.text


# --------------------------------------------------------------------------- #
# Distinguishable failures (§3)
# --------------------------------------------------------------------------- #
def test_no_backend_and_no_entry_are_different_errors(root, monkeypatch):
    manifest, _ = make_project("Distinct", "d@gmail.com", root=root)

    # Entry removed, backend still present.
    credentials.delete(manifest.id)
    with pytest.raises(credentials.NoCredentialStored) as no_entry:
        credentials.resolve(manifest.id)

    # Backend gone entirely.
    monkeypatch.setattr(credentials, "backend_available", lambda: False)
    with pytest.raises(credentials.NoCredentialBackend) as no_backend:
        credentials.resolve(manifest.id)

    entry_msg, backend_msg = str(no_entry.value), str(no_backend.value)
    assert entry_msg != backend_msg
    # Each names its own fix.
    assert "set-password" in entry_msg
    assert credentials.env_var_for(manifest.id) in backend_msg
    assert "no file-based fallback" in backend_msg.lower() or \
           "file-based fallback" in backend_msg
    # The missing-entry message must not send someone hunting for a backend.
    assert "A credential store IS available" in entry_msg


def _login_failure(text):
    """A stand-in for imap_tools' MailboxLoginError, which carries the server's
    response text (verified against the installed package: it formats the
    RESPONSE, never the command arguments, so no password can appear in it)."""
    class MailboxLoginError(Exception):
        pass

    return MailboxLoginError(text)


@pytest.mark.parametrize("server_text,expected,must_mention", [
    ("[AUTHENTICATIONFAILED] Invalid credentials (Failure)",
     ConnectionOutcome.REJECTED, "revoked"),
    ("[ALERT] Application-specific password required: "
     "https://support.google.com/accounts/answer/185833",
     ConnectionOutcome.APP_PASSWORD_REQUIRED, "apppasswords"),
    ("[ALERT] Please log in via your web browser: "
     "https://support.google.com/mail/accounts/answer/78754",
     ConnectionOutcome.SIGNIN_BLOCKED, "2-Step Verification"),
    ("[ALERT] Your account is not enabled for IMAP use.",
     ConnectionOutcome.IMAP_DISABLED, "Forwarding and POP/IMAP"),
])
def test_each_login_failure_is_reported_as_its_own_problem(
        server_text, expected, must_mention):
    result = check_connection(
        host="imap.gmail.com", user="u@gmail.com", folder="INBOX", use_ssl=True,
        password=SecretStr("x"),
        mailbox_factory=mailbox_factory(login_error=_login_failure(server_text)))

    assert result.outcome is expected
    assert result.ok is False
    assert must_mention in result.message
    assert result.server_detail == server_text


def test_the_signin_blocked_message_names_both_causes_it_cannot_separate():
    """Google answers 'no 2SV' and 'blocked sign-in' identically. Saying which
    is a guess, so the message states both and says so."""
    result = check_connection(
        host="imap.gmail.com", user="u@gmail.com", folder="INBOX", use_ssl=True,
        password=SecretStr("x"),
        mailbox_factory=mailbox_factory(
            login_error=_login_failure("[ALERT] Please log in via your web browser")))

    # Normalised, because the message is hard-wrapped for the terminal and the
    # phrases under test span line breaks.
    message = " ".join(result.message.split())
    assert "does not distinguish them" in message
    assert "2-Step Verification is NOT enabled" in message
    assert "blocked this particular sign-in" in message
    assert "rather than guessing" in message


def test_a_network_failure_is_not_reported_as_an_auth_failure():
    result = check_connection(
        host="imap.gmail.com", user="u@gmail.com", folder="INBOX", use_ssl=True,
        password=SecretStr("x"),
        mailbox_factory=mailbox_factory(
            login_error=OSError("[Errno 11001] getaddrinfo failed")))

    assert result.outcome is ConnectionOutcome.NETWORK
    assert "not an authentication one" in result.message
    assert "nothing about" in result.message.lower()


def test_an_unrecognised_response_is_quoted_rather_than_guessed_at():
    weird = "[SYSTEMERROR] the mail server is having a day"
    result = check_connection(
        host="imap.gmail.com", user="u@gmail.com", folder="INBOX", use_ssl=True,
        password=SecretStr("x"),
        mailbox_factory=mailbox_factory(login_error=_login_failure(weird)))

    assert result.outcome is ConnectionOutcome.UNKNOWN
    assert weird in result.message
    assert "does not match a case this tool recognises" in result.message


def test_credential_failures_surface_through_check_project(root, monkeypatch):
    manifest, _ = make_project("Missing", "m@gmail.com", root=root)
    ctx = _ctx(root, manifest)

    credentials.delete(manifest.id)
    assert check_project(ctx).outcome is ConnectionOutcome.NO_ENTRY

    monkeypatch.setattr(credentials, "backend_available", lambda: False)
    assert check_project(ctx).outcome is ConnectionOutcome.NO_BACKEND


def test_ambiguous_scope_surfaces_through_check_project(root, monkeypatch):
    manifest, _ = make_project("Scoped", "s@gmail.com", root=root)
    monkeypatch.setenv(credentials.ENV_UNSCOPED, "secret")
    result = check_project(_ctx(root, manifest), projects_in_scope=3)
    assert result.outcome is ConnectionOutcome.AMBIGUOUS_SCOPE


# --------------------------------------------------------------------------- #
# Success, and the empty-folder warning
# --------------------------------------------------------------------------- #
def test_success_reports_the_message_count(root):
    manifest, _ = make_project("Counted", "c@gmail.com", root=root, messages=41)
    result = check_project(_ctx(root, manifest),
                           mailbox_factory=mailbox_factory(messages=41))
    assert result.ok
    assert result.message_count == 41
    assert "41 message(s)" in result.message
    assert result.credential_source.startswith("keyring")


def test_an_empty_folder_is_flagged_with_the_filter_explanation(root):
    """An empty INBOX means ingestion runs cleanly and stores nothing, which is
    indistinguishable from 'no alerts yet'. Setup is the cheap moment to notice."""
    manifest, _ = make_project("Empty Inbox", "e@gmail.com", root=root)
    result = check_project(_ctx(root, manifest),
                           mailbox_factory=mailbox_factory(messages=0))

    assert result.ok                      # connecting worked; this is a warning
    assert result.message_count == 0
    assert "The folder is EMPTY" in result.message
    assert "filter" in result.message
    assert "[Gmail]/All Mail" in result.message


def test_a_logout_failure_does_not_fail_a_successful_test(root):
    manifest, _ = make_project("Teardown", "t@gmail.com", root=root)

    factory = mailbox_factory(messages=3)
    cls = factory(True)

    def failing_logout(self):
        raise OSError("connection reset at teardown")

    cls.logout = failing_logout
    result = check_project(_ctx(root, manifest), mailbox_factory=lambda ssl: cls)
    assert result.ok and result.message_count == 3


# --------------------------------------------------------------------------- #
# create: validate -> verify -> write
# --------------------------------------------------------------------------- #
def test_a_failed_connection_test_leaves_nothing_behind(root, keyring_backend):
    with pytest.raises(ProjectError) as exc:
        create_project(
            "Never Made", "n@gmail.com", GMAIL_SHAPED, root=root,
            mailbox_factory=mailbox_factory(
                login_error=_login_failure("[AUTHENTICATIONFAILED] Invalid credentials")))

    assert "revoked" in str(exc.value)
    # No directory, no registry entry, no credential.
    assert not ppaths.projects_dir(root).exists() or \
           list(ppaths.projects_dir(root).glob("never-made-*")) == []
    assert Registry.load(root).entries == []
    assert keyring_backend.store == {}


def test_a_refused_domain_never_reaches_the_connection_test(root, keyring_backend):
    """Validation is step 1, so a bad domain costs no network call and no
    password prompt."""
    record = []
    with pytest.raises(ProjectError):
        create_project("MS", "someone@outlook.com", GMAIL_SHAPED, root=root,
                       mailbox_factory=mailbox_factory(record=record))
    assert record == []                   # login was never attempted
    assert keyring_backend.store == {}


def test_a_database_failure_rolls_back_the_stored_credential(
        root, monkeypatch, keyring_backend):
    """The credential is part of the project. A rolled-back creation must not
    leave one behind under an id that nothing references any more."""
    import ttr.storage.migrations as migrations

    monkeypatch.setattr(migrations, "connect",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("disk full")))

    with pytest.raises(RuntimeError):
        make_project("Rolled Back", "r@gmail.com", root=root)

    assert Registry.load(root).entries == []
    assert list(ppaths.projects_dir(root).glob("rolled-back-*")) == []
    assert keyring_backend.store == {}    # no orphan credential


def test_skip_connection_test_creates_without_verifying(root, keyring_backend):
    manifest, project_dir = create_project(
        "Offline", "o@gmail.com", root=root, skip_connection_test=True)

    assert project_dir.exists()
    # Nothing was verified, so nothing was stored -- an unset credential_ref is
    # an absent key, keeping Stage 1's distinction.
    assert manifest.mailbox.credential_ref is None
    assert keyring_backend.store == {}
    assert "credential_ref" not in (project_dir / "project.toml").read_text(
        encoding="utf-8").replace("# credential_ref: written by", "")


def test_creation_requires_a_password_unless_skipping(root):
    with pytest.raises(ProjectError) as exc:
        create_project("No Password", "n@gmail.com", root=root)
    assert "--skip-connection-test" in str(exc.value)


# --------------------------------------------------------------------------- #
# set-password
# --------------------------------------------------------------------------- #
def test_set_password_verifies_before_storing(root, keyring_backend):
    manifest, project_dir = create_project(
        "Later", "l@gmail.com", root=root, skip_connection_test=True)
    assert keyring_backend.store == {}

    with pytest.raises(ProjectError):
        set_password(manifest.id, GMAIL_SHAPED, root=root,
                     mailbox_factory=mailbox_factory(
                         login_error=_login_failure("[AUTHENTICATIONFAILED] nope")))

    # A password just shown not to work is not saved.
    assert keyring_backend.store == {}
    assert ProjectManifest.read(project_dir).mailbox.credential_ref is None


def test_set_password_stores_and_records_the_reference(root, keyring_backend):
    manifest, project_dir = create_project(
        "Storing", "s@gmail.com", root=root, skip_connection_test=True)

    updated, result = set_password(manifest.id, GMAIL_SHAPED, root=root,
                                   mailbox_factory=mailbox_factory(messages=5))

    assert result.ok and result.message_count == 5
    assert updated.mailbox.credential_ref == f"traptracker-report:{manifest.id}"
    assert ProjectManifest.read(project_dir).mailbox.credential_ref == \
        updated.mailbox.credential_ref
    assert keyring_backend.store[(credentials.SERVICE, manifest.id)] == \
        "abcdefghijklmnop"


def test_set_password_rotates_an_existing_credential(root, keyring_backend):
    manifest, _ = make_project("Rotating", "r@gmail.com", root=root)
    assert keyring_backend.store[(credentials.SERVICE, manifest.id)] == \
        "abcdefghijklmnop"

    set_password(manifest.id, "wxyz 1234 wxyz 1234", root=root,
                 mailbox_factory=mailbox_factory())

    assert keyring_backend.store[(credentials.SERVICE, manifest.id)] == \
        "wxyz1234wxyz1234"
    # One entry, replaced -- not two.
    assert len(keyring_backend.store) == 1


# --------------------------------------------------------------------------- #
# delete removes the credential
# --------------------------------------------------------------------------- #
def test_delete_removes_the_stored_credential(root, keyring_backend):
    manifest, _ = make_project("Disposable", "d@gmail.com", root=root)
    assert keyring_backend.store

    plan = delete_project(manifest.id, root=root)

    assert keyring_backend.store == {}
    assert plan.credential_removed is True
    assert plan.credential_error is None


def test_deleting_a_project_with_no_credential_is_not_an_error(root):
    manifest, _ = create_project("Bare", "b@gmail.com", root=root,
                                 skip_connection_test=True)
    plan = delete_project(manifest.id, root=root)
    assert plan.credential_removed is False
    assert plan.credential_error is None


def test_a_failed_credential_removal_is_reported_not_swallowed(root, monkeypatch):
    """A leftover credential is not dangerous, but the user is entitled to know
    it is still in their credential store."""
    manifest, _ = make_project("Stubborn", "s@gmail.com", root=root)
    monkeypatch.setattr(credentials, "delete",
                        lambda pid: (_ for _ in ()).throw(RuntimeError("vault locked")))

    plan = delete_project(manifest.id, root=root)

    assert plan.credential_error is not None
    assert "vault locked" in plan.credential_error
    assert not plan.project_dir.exists()      # the project still went


# --------------------------------------------------------------------------- #
# The secret must not appear anywhere
# --------------------------------------------------------------------------- #
def test_the_password_appears_in_no_log_record_no_repr_and_no_file(root, caplog):
    with caplog.at_level(logging.DEBUG):
        manifest, project_dir = create_project(
            "Sentinel", "s@gmail.com", SENTINEL, root=root,
            mailbox_factory=mailbox_factory(messages=2))
        ctx = _ctx(root, manifest)
        secret = ctx.imap_password(projects_in_scope=1)
        result = check_project(ctx, mailbox_factory=mailbox_factory(messages=2))

    # 1. Not in any log line, formatted the way the CLI prints them.
    assert SENTINEL not in caplog.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in str(record.__dict__)

    # 2. Not in the repr of anything that carries it.
    assert SENTINEL not in repr(secret)
    assert SENTINEL not in str(secret)
    assert SENTINEL not in repr(ctx)
    assert SENTINEL not in repr(manifest)
    assert SENTINEL not in repr(result)
    resolved = credentials.resolve(manifest.id)
    assert SENTINEL not in repr(resolved)

    # 3. Nowhere on disk in the project directory.
    for path in project_dir.rglob("*"):
        if path.is_file():
            blob = path.read_bytes()
            assert SENTINEL.encode() not in blob

    # ...and it really is the value, so the assertions above mean something.
    assert secret.get_secret_value() == SENTINEL


def test_a_resolved_credentials_repr_names_only_its_source(root):
    manifest, _ = make_project("Repr", "r@gmail.com", root=root)
    resolved = credentials.resolve(manifest.id)
    text = repr(resolved)
    assert "keyring" in text
    assert "abcdefghijklmnop" not in text


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
@pytest.fixture
def offline(monkeypatch):
    import ttr.projects.connection as connection

    monkeypatch.setattr(connection, "default_mailbox_factory",
                        mailbox_factory(messages=9))
    return connection


def _cli(root, *args, password=None, **kw):
    """Invoke `ttr project ...`.

    `password=` supplies the secret through TTR_IMAP_PASSWORD rather than
    a flag, because there is no longer a `--password` flag: a secret on a
    command line lands in shell history and the process list. The env var
    is the route a headless user actually takes, so the tests take it too.
    """
    env = {ppaths.ROOT_ENV_VAR: str(root)}
    if password is not None:
        env["TTR_IMAP_PASSWORD"] = password
    return runner.invoke(app, ["project", *args], env=env, **kw)


def test_cli_create_prompts_for_a_password_without_echoing_it(root, offline):
    result = _cli(root, "create", "--name", "Prompted", "--email", "p@gmail.com",
                  input=f"{GMAIL_SHAPED}\n")
    assert result.exit_code == 0
    assert GMAIL_SHAPED not in result.output          # hide_input
    assert "credential stored" in result.output


def test_cli_create_validates_the_domain_before_asking_for_a_password(root, offline):
    """A user with an outlook.com address should not type a 16-character secret
    before being told the provider cannot work."""
    result = _cli(root, "create", "--name", "MS", "--email", "me@outlook.com")
    assert result.exit_code == 2
    assert "16 September 2024" in result.output
    assert "app password" not in result.output.split("16 September")[0].lower()


def test_cli_test_connection_reports_source_and_count(root, offline):
    _cli(root, "create", "--name", "Tested", "--email", "t@gmail.com",
         password=GMAIL_SHAPED)
    result = _cli(root, "test-connection", "Tested")

    assert result.exit_code == 0
    assert "credential: keyring (traptracker-report:" in result.output
    assert "9 message(s)" in result.output
    assert GMAIL_SHAPED not in result.output


def test_cli_test_connection_exits_nonzero_on_failure(root, monkeypatch, offline):
    _cli(root, "create", "--name", "Broken", "--email", "b@gmail.com",
         password=GMAIL_SHAPED)

    import ttr.projects.connection as connection
    monkeypatch.setattr(connection, "default_mailbox_factory",
                        mailbox_factory(login_error=_login_failure(
                            "[ALERT] Your account is not enabled for IMAP use.")))

    result = _cli(root, "test-connection", "Broken")
    assert result.exit_code == 1
    assert "Forwarding and POP/IMAP" in result.output


def test_cli_skip_connection_test_warns_loudly(root):
    result = _cli(root, "create", "--name", "Unverified", "--email", "u@gmail.com",
                  "--skip-connection-test")
    assert result.exit_code == 0
    assert "WARNING" in result.output
    assert "NOT verified" in result.output
    assert "set-password" in result.output


def test_cli_set_password_does_not_echo_and_reports_the_reference(root, offline):
    _cli(root, "create", "--name", "Rotate", "--email", "r@gmail.com",
         "--skip-connection-test")
    result = _cli(root, "set-password", "Rotate", input=f"{GMAIL_SHAPED}\n")

    assert result.exit_code == 0
    assert GMAIL_SHAPED not in result.output
    assert "traptracker-report:" in result.output
    assert "not in the project directory" in result.output


def test_cli_delete_reports_that_the_credential_went(root, offline):
    _cli(root, "create", "--name", "Gone", "--email", "g@gmail.com",
         password=GMAIL_SHAPED)
    result = _cli(root, "delete", "Gone", "--yes")
    assert result.exit_code == 0
    assert "stored mailbox credential removed" in result.output
