"""Live-IMAP smoke test — SKIPPED by default (plan §6 Stage 1).

Runs only when real IMAP credentials are present in the environment. It proves
the one part that needs a live server (EmailFetcher) can connect and that the
fetcher -> parser path yields events end-to-end. In CI / normal runs it is
skipped, so ingestion is fully tested offline via the .eml fixtures.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live_imap

_HAS_CREDS = all(os.environ.get(k) for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"))


@pytest.mark.skipif(not _HAS_CREDS, reason="no live IMAP credentials in environment")
def test_live_fetch_and_parse_smoke():
    from ttr.config import Settings
    from ttr.sources.email_fetcher import EmailFetcher
    from ttr.sources.email_source import EmailSource
    from ttr.sources.seen_store import SeenStore

    settings = Settings()
    tmp = settings.db_path.parent / "seen_smoke.txt"
    source = EmailSource(EmailFetcher(settings), SeenStore(tmp))
    events = list(source.poll())
    # We can't assert content (unknown mailbox state), only that the path runs.
    for ev in events:
        assert ev.source_type == "email"
        assert ev.source_message_id
