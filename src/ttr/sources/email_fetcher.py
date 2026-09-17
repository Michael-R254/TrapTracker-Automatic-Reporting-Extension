"""IMAP fetching — the ONLY part of ingestion that needs a live server.

Isolated from parsing so the whole normalisation path is testable on saved
``.eml`` files (plan §5.1). Yields ``(message_id, EmailMessage)`` pairs, the
messages re-parsed under ``email.policy.default`` so headers decode cleanly and
``iter_attachments``/``walk`` behave the same as in the parser unit tests.

Connects to the dedicated recipient mailbox (Decision 6) using credentials from
config; never touches a shared or personal inbox.
"""

from __future__ import annotations

import email
import email.policy          # bind the submodule explicitly: `import email`
                             # alone does not, and this module's only use of it
                             # is at fetch time, where nothing else had.
from datetime import date, timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING, Callable, Iterator, Optional, Tuple

from imap_tools import AND, MailBox, MailBoxUnencrypted

from ..logging import get_logger

if TYPE_CHECKING:
    from pydantic import SecretStr

logger = get_logger(__name__)

#: UIDs per body-fetch command. An IMAP command line has a length limit, and a
#: backlog of thousands of new messages would otherwise build one huge SEARCH.
_UID_CHUNK = 200


class EmailFetcher:
    """Pure IMAP I/O. Idempotency is decided by the seen-store; this class only
    uses it to avoid *downloading* what has already been handled.

    Takes discrete mailbox settings plus a PASSWORD CALLABLE — never a password.
    The callable is invoked at login and its result is used as an argument and
    dropped, so no instance of this class ever holds the secret: it cannot reach
    a ``repr``, a traceback frame, a pickle, or a debug log, whether or not
    anyone remembered to check. (``Settings.imap_password`` is a ``SecretStr`` for
    the same reason; this closes the same hole one level further out.)

    It no longer accepts a ``Settings``. Mailbox configuration is per-deployment,
    so it now arrives from a project's manifest; the callers that passed a
    ``Settings`` object went with Stage 5.
    """

    def __init__(self, *,
                 host: str,
                 user: str,
                 password: Callable[[], "SecretStr"],
                 is_known: Optional[Callable[[str], bool]] = None,
                 folder: str = "INBOX",
                 use_ssl: bool = True,
                 lookback_days: Optional[int] = None) -> None:
        if not host or not user or password is None:
            raise ValueError(
                "EmailFetcher needs host, user and a password callable")

        self._host = host
        self._user = user
        self._folder = folder
        self._use_ssl = use_ssl
        self._lookback_days = lookback_days
        #: Resolved at login, never stored. See the class docstring.
        self._password = password
        # Predicate over Message-ID, supplied by the source that owns the
        # seen-store. Passed as a plain callable so the fetcher gains no
        # knowledge of how idempotency is stored — only whether to spend
        # bandwidth on a message.
        self._is_known = is_known or (lambda _message_id: False)

    def __repr__(self) -> str:
        """Names the mailbox, never the credential.

        Explicit rather than inherited: the default dataclass-ish repr of a
        future subclass, or a debugger's, would happily render whatever
        ``_password`` closed over.
        """
        return (f"EmailFetcher(host={self._host!r}, user={self._user!r}, "
                f"folder={self._folder!r})")

    def _mailbox_cls(self):
        if self._use_ssl:
            return MailBox
        return MailBoxUnencrypted

    def _criteria(self):
        """Server-side filter. A lookback bounds the header scan on a mailbox
        that has been accumulating for years; unset means the whole folder."""
        days = self._lookback_days
        if not days:
            return "ALL"
        return AND(date_gte=date.today() - timedelta(days=int(days)))

    def fetch_unseen(self) -> Iterator[Tuple[str, EmailMessage]]:
        """Fetch NEW messages, close the connection, THEN yield them.

        Two passes, because the two costs are wildly different. Pass 1 fetches
        HEADERS ONLY and asks the seen-store which Message-IDs are already
        handled. Pass 2 downloads full bodies for the remainder alone. An alert
        carries two full-resolution JPEGs, so the saving is the whole point:
        previously every poll re-downloaded the entire mailbox, attachments
        included, which grew without bound while the poll interval did not.
        A message with no Message-ID header cannot be matched, so it is always
        fetched and the parser synthesises a stable id.

        Materialising before yielding — rather than yielding inside the mailbox
        context — means the IMAP connection is open only for the fetch, not for
        the whole downstream enrichment loop. A long run (BioCLIP + VLM over
        hundreds of alerts, ~minutes each hundred) would otherwise hold the
        session open past the server's idle timeout and fail at ``logout``
        (observed with Gmail on a ~40-min pass). The logout is also
        error-swallowed: by then the messages are already in hand, so a dropped
        connection at teardown must not fail the run.

        Does NOT mark messages ``\\Seen`` (``mark_seen=False``): idempotency is
        the seen-store's job, and the server-side flag is the user's to control.
        """
        criteria = self._criteria()
        logger.info("imap_poll_start",
                    extra={"host": self._host, "folder": self._folder})

        fetched: list[Tuple[str, EmailMessage]] = []
        # The password is RESOLVED here and unwrapped here, and exists nowhere
        # else: not on the instance, not in a local that outlives this call, not
        # in a log record. See the class docstring.
        mailbox = self._mailbox_cls()(self._host).login(
            self._user, self._password().get_secret_value(),
            initial_folder=self._folder)
        try:
            # --- pass 1: headers only, to decide what is worth downloading ----
            scanned = 0
            wanted: list[str] = []
            for mm in mailbox.fetch(criteria, headers_only=True, bulk=True,
                                    mark_seen=False):
                scanned += 1
                message_id = (mm.headers.get("message-id") or ("",))[0].strip()
                if message_id and self._is_known(message_id):
                    continue
                wanted.append(mm.uid)
            logger.info("imap_scan_done",
                        extra={"scanned": scanned, "to_fetch": len(wanted)})

            # --- pass 2: full bodies for the new ones only --------------------
            for chunk in (wanted[i:i + _UID_CHUNK]
                          for i in range(0, len(wanted), _UID_CHUNK)):
                for mm in mailbox.fetch(AND(uid=chunk), bulk=True, mark_seen=False):
                    msg = email.message_from_bytes(mm.obj.as_bytes(),
                                                   policy=email.policy.default)
                    fetched.append(((msg["Message-ID"] or "").strip(), msg))
        finally:
            try:
                mailbox.logout()
            except Exception as exc:   # dropped/idle-timed-out session at teardown
                logger.warning("imap_logout_failed", extra={"error": repr(exc)})

        logger.info("imap_poll_done", extra={"fetched": len(fetched)})
        yield from fetched
