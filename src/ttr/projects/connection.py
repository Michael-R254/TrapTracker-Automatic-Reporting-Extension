"""The mailbox connection test.

Six things go wrong when a mailbox will not connect, and they have six different
fixes. Collapsing them into "authentication failed" is what makes a tool like
this miserable - the user cannot tell whether to generate a password, enable a
setting, check their typing, or check their network.

So this module maps the server's own response onto named outcomes, and where
Gmail does NOT distinguish two causes it says so rather than picking the more
likely one. ``ConnectionOutcome.SIGNIN_BLOCKED`` is the honest case: Google
answers a no-2SV account and a blocked sign-in attempt with the same ALERT, so
the message names both and does not assert which.

The message count on success is not decoration. A Gmail filter that labels and
archives the forwarded alerts leaves INBOX empty, and ingestion would then run
perfectly and store nothing - indistinguishable from "no alerts yet" until
someone goes looking. Setup is the cheap moment to notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from pydantic import SecretStr

from ..logging import get_logger
from . import credentials

logger = get_logger(__name__)


class ConnectionOutcome(str, Enum):
    OK = "ok"
    NO_BACKEND = "no_credential_backend"
    NO_ENTRY = "no_credential_stored"
    AMBIGUOUS_SCOPE = "ambiguous_credential_scope"
    APP_PASSWORD_REQUIRED = "app_password_required"
    SIGNIN_BLOCKED = "signin_blocked"
    IMAP_DISABLED = "imap_disabled"
    REJECTED = "credentials_rejected"
    FOLDER_MISSING = "folder_missing"
    NETWORK = "network_unreachable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ConnectionResult:
    outcome: ConnectionOutcome
    message: str
    #: Provenance only — the value is never carried here.
    credential_source: Optional[str] = None
    message_count: Optional[int] = None
    folder: Optional[str] = None
    #: The server's own words, kept for the unknown case. Never contains the
    #: password: IMAP servers echo the response, not the command arguments.
    server_detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome is ConnectionOutcome.OK


_ALL_MAIL = "[Gmail]/All Mail"

_EMPTY_FOLDER_NOTE = (
    "\n"
    "The folder is EMPTY. If alerts are already arriving, a Gmail filter is\n"
    "probably labelling and archiving them, which takes them out of {folder}.\n"
    "Ingestion would then run cleanly and store nothing, which looks identical\n"
    "to 'no alerts yet'.\n"
    "Either change the filter to keep them in {folder}, or point this project at\n"
    "{all_mail} by editing imap_folder in its project.toml."
)

_APP_PASSWORD_MESSAGE = (
    "Google rejected the password because it wants an APP PASSWORD.\n"
    "\n"
    "2-Step Verification is enabled on this account, so your ordinary Google\n"
    "password will not work over IMAP. Generate a 16-character app password at\n"
    "    https://myaccount.google.com/apppasswords\n"
    "and use that. It is shown as four groups of four; pasting it with the\n"
    "spaces is fine."
)

_SIGNIN_BLOCKED_MESSAGE = (
    "Google refused the sign-in and asked for a web login instead.\n"
    "\n"
    "Google returns this same response for two different causes and does not\n"
    "distinguish them, so both are listed rather than guessing:\n"
    "\n"
    "  1. 2-Step Verification is NOT enabled on the account. App passwords only\n"
    "     exist once 2SV is on, so there is no password that would work yet.\n"
    "     Enable 2SV first, then generate one at\n"
    "         https://myaccount.google.com/apppasswords\n"
    "\n"
    "  2. Google blocked this particular sign-in as suspicious. Check the\n"
    "     account's recent security activity and approve it.\n"
    "\n"
    "If 2SV is already on, it is the second."
)

_IMAP_DISABLED_MESSAGE = (
    "The account rejected IMAP access itself - the password was not the problem.\n"
    "\n"
    "Enable it in Gmail: Settings -> Forwarding and POP/IMAP -> Enable IMAP.\n"
    "On a Workspace account an administrator may need to allow it."
)

_REJECTED_MESSAGE = (
    "The mailbox rejected the credentials.\n"
    "\n"
    "The address and password did not authenticate. Most often this is an app\n"
    "password that has been revoked, or one belonging to a different account\n"
    "than the address given. Generate a fresh one at\n"
    "    https://myaccount.google.com/apppasswords\n"
    "and supply it again: re-run set-password, or replace the value of the\n"
    "environment variable it came from."
)

_NETWORK_MESSAGE = (
    "Could not reach the mail server.\n"
    "\n"
    "This is a network or DNS failure, not an authentication one - nothing about\n"
    "the address or password has been tested. Check connectivity and any proxy\n"
    "or firewall, then try again."
)


def _classify(detail: str) -> tuple[ConnectionOutcome, str]:
    """Map a server response onto an outcome.

    Matched on Gmail's own ALERT text. An unrecognised response is reported AS
    the server's words rather than forced into the nearest bucket - a confident
    wrong diagnosis is worse than an honest "here is what the server said".
    """
    lowered = detail.lower()
    if "application-specific password required" in lowered:
        return ConnectionOutcome.APP_PASSWORD_REQUIRED, _APP_PASSWORD_MESSAGE
    if "log in via your web browser" in lowered or "answer/78754" in lowered:
        return ConnectionOutcome.SIGNIN_BLOCKED, _SIGNIN_BLOCKED_MESSAGE
    if ("not enabled for imap" in lowered or "imap access is disabled" in lowered
            or "imap is disabled" in lowered):
        return ConnectionOutcome.IMAP_DISABLED, _IMAP_DISABLED_MESSAGE
    if ("invalid credentials" in lowered or "authenticationfailed" in lowered
            or "authentication failed" in lowered):
        return ConnectionOutcome.REJECTED, _REJECTED_MESSAGE
    return (ConnectionOutcome.UNKNOWN,
            "The mail server refused the connection, and its response does not "
            "match a case this tool recognises. Its own words were:\n\n"
            f"    {detail}")


def default_mailbox_factory(use_ssl: bool):
    """The real imap_tools mailbox class. Replaced wholesale in tests."""
    from imap_tools import MailBox, MailBoxUnencrypted

    return MailBox if use_ssl else MailBoxUnencrypted


def check_connection(*, host: str, user: str, folder: str, use_ssl: bool,
                     password: SecretStr,
                     credential_source: Optional[str] = None,
                     mailbox_factory: Optional[Callable] = None) -> ConnectionResult:
    """Open a session, select ``folder``, and count what is in it.

    Takes explicit parameters rather than a project, because ``create`` runs this
    BEFORE any project directory exists - nothing may be written until the
    mailbox has answered.
    """
    factory = mailbox_factory or default_mailbox_factory
    cls = factory(use_ssl)

    logger.info("connection_test_start",
                extra={"host": host, "folder": folder,
                       "credential_source": credential_source})

    mailbox = None
    try:
        mailbox = cls(host).login(user, password.get_secret_value(),
                                  initial_folder=folder)
        status = mailbox.folder.status(folder)
        count = int(status.get("MESSAGES", 0))
    except Exception as exc:
        name = type(exc).__name__
        detail = str(exc)
        if isinstance(exc, (OSError, TimeoutError)) and "Mailbox" not in name:
            # socket.gaierror, ConnectionRefusedError, timeouts — all OSError.
            outcome, message = ConnectionOutcome.NETWORK, _NETWORK_MESSAGE
        elif "FolderSelect" in name or "folder" in detail.lower() and "select" in detail.lower():
            outcome = ConnectionOutcome.FOLDER_MISSING
            message = (f"Connected, but the folder {folder!r} does not exist on "
                       f"this account.\n\nCheck imap_folder in the project's "
                       f"project.toml. For Gmail the usual values are 'INBOX' or "
                       f"'{_ALL_MAIL}'.")
        else:
            outcome, message = _classify(detail)
        logger.info("connection_test_failed",
                    extra={"host": host, "outcome": outcome.value, "error": name})
        return ConnectionResult(outcome=outcome, message=message, folder=folder,
                                credential_source=credential_source,
                                server_detail=detail)
    finally:
        if mailbox is not None:
            try:
                mailbox.logout()
            except Exception as exc:
                # By now the answer is in hand; a teardown failure must not turn
                # a successful test into a failed one (same reasoning as
                # EmailFetcher.fetch_unseen).
                logger.warning("connection_test_logout_failed",
                               extra={"error": repr(exc)})

    message = f"Connected to {host} as {user}; folder {folder!r} holds {count} message(s)."
    if count == 0:
        message += _EMPTY_FOLDER_NOTE.format(folder=folder, all_mail=_ALL_MAIL)
    logger.info("connection_test_ok",
                extra={"host": host, "folder": folder, "messages": count})
    return ConnectionResult(outcome=ConnectionOutcome.OK, message=message,
                            credential_source=credential_source,
                            message_count=count, folder=folder)


def check_project(ctx, *, projects_in_scope: int = 1,
                  mailbox_factory: Optional[Callable] = None) -> ConnectionResult:
    """Resolve this project's credential, then test it.

    A credential problem is reported as its own outcome rather than as a
    connection failure: "no keyring entry" and "wrong password" need different
    things done about them.
    """
    try:
        resolved = credentials.resolve(ctx.id, projects_in_scope=projects_in_scope)
    except credentials.NoCredentialBackend as exc:
        return ConnectionResult(ConnectionOutcome.NO_BACKEND, str(exc))
    except credentials.NoCredentialStored as exc:
        return ConnectionResult(ConnectionOutcome.NO_ENTRY, str(exc))
    except credentials.AmbiguousCredentialScope as exc:
        return ConnectionResult(ConnectionOutcome.AMBIGUOUS_SCOPE, str(exc))

    return check_connection(
        host=ctx.mailbox.imap_host, user=ctx.mailbox.imap_user,
        folder=ctx.mailbox.imap_folder, use_ssl=ctx.mailbox.imap_use_ssl,
        password=resolved.secret, credential_source=resolved.source,
        mailbox_factory=mailbox_factory)
