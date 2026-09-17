"""Stage 1 verification: EmailSource wires fetcher -> seen-store -> parser, is
idempotent across polls, and ignores non-alert emails — all offline via a fake
fetcher over the .eml fixtures (no live inbox)."""

from __future__ import annotations

from ttr.sources.email_source import EmailSource
from ttr.sources.seen_store import SeenStore

from conftest import load_eml


class FakeFetcher:
    """Yields (message_id, EmailMessage) pairs from fixture .eml files."""

    def __init__(self, names):
        self._names = names

    def fetch_unseen(self):
        for name in self._names:
            msg = load_eml(name)
            yield (msg["Message-ID"] or "").strip(), msg


def _source(tmp_path, names):
    return EmailSource(FakeFetcher(names), SeenStore(tmp_path / "seen.txt"))


def test_poll_yields_alerts_and_ignores_test_email(tmp_path):
    # Uses STABLE reconstructed fixtures (not the swappable golden), so a future
    # golden swap never touches this test.
    src = _source(tmp_path, ["missing_boxed.eml", "test_email_ignore.eml", "both_attachments.eml"])
    events = list(src.poll())

    labels = {e.upstream_label for e in events}
    assert labels == {"CapreolusCapreolus", "MelesMeles"}   # test email excluded
    assert len(events) == 2


def test_seen_store_prevents_reprocessing_once_the_consumer_acknowledges(tmp_path):
    """Idempotency is ACKNOWLEDGED, not assumed at yield time.

    Polling alone must not retire a message: the consumer can still fail to store
    it, and an event retired before it was stored is an event silently lost.
    Retirement happens on ``mark_processed``, which the pipeline calls only after
    every durable write has succeeded.
    """
    seen_path = tmp_path / "seen.txt"
    names = ["missing_boxed.eml", "both_attachments.eml"]

    first = EmailSource(FakeFetcher(names), SeenStore(seen_path))
    events = list(first.poll())
    assert len(events) == 2
    for ev in events:                       # stand in for the pipeline storing them
        first.mark_processed(ev.source_message_id)

    # A second poll of the very same messages yields nothing (idempotent),
    # including across a fresh SeenStore that reloads from disk.
    second = EmailSource(FakeFetcher(names), SeenStore(seen_path))
    assert list(second.poll()) == []


def test_unacknowledged_messages_are_offered_again(tmp_path):
    """The durability half: nothing acknowledged means nothing retired, so the
    next poll re-offers the work rather than dropping it."""
    seen_path = tmp_path / "seen.txt"
    names = ["missing_boxed.eml", "both_attachments.eml"]

    first = EmailSource(FakeFetcher(names), SeenStore(seen_path))
    assert len(list(first.poll())) == 2     # consumer "crashes" — acknowledges none

    second = EmailSource(FakeFetcher(names), SeenStore(seen_path))
    assert len(list(second.poll())) == 2


def test_one_poll_does_not_yield_the_same_message_twice(tmp_path):
    """Deferred retirement must not let a duplicate inside a single batch through
    and cost two enrichment passes."""
    src = _source(tmp_path, ["both_attachments.eml", "both_attachments.eml"])
    assert len(list(src.poll())) == 1


def test_ignored_test_email_is_marked_seen(tmp_path):
    seen = SeenStore(tmp_path / "seen.txt")
    src = EmailSource(FakeFetcher(["test_email_ignore.eml"]), seen)
    assert list(src.poll()) == []
    # Its Message-ID was recorded so it is not re-evaluated next poll.
    assert "<test-email-9999@garden.trapcam.alerts>" in seen
