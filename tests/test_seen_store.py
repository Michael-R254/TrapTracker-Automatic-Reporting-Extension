"""Stage 1 verification: the seen-store is idempotent and durable."""

from __future__ import annotations

from ttr.sources.seen_store import SeenStore


def test_add_reports_new_then_seen(tmp_path):
    store = SeenStore(tmp_path / "seen.txt")
    mid = "<abc@example.com>"

    assert store.add(mid) is True     # newly added
    assert store.add(mid) is False    # already seen
    assert mid in store
    assert len(store) == 1


def test_contains_false_for_unknown(tmp_path):
    store = SeenStore(tmp_path / "seen.txt")
    assert "<never@example.com>" not in store


def test_persists_across_instances(tmp_path):
    path = tmp_path / "nested" / "seen.txt"
    first = SeenStore(path)
    first.add("<one@example.com>")
    first.add("<two@example.com>")

    # A fresh instance reloads the same ids from disk.
    second = SeenStore(path)
    assert "<one@example.com>" in second
    assert "<two@example.com>" in second
    assert len(second) == 2
    assert second.add("<one@example.com>") is False
