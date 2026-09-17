"""Stored image paths are store-relative, forward-slashed, and nothing else.

`_persist_images` returned `str(dest)` off an absolute store root, so every row
it ever wrote held an absolute path. Nothing failed: an absolute path resolves,
and `image_store_dir / <absolute>` yields the absolute path unchanged, so the
eleven events of the first live multi-project ingest resolved perfectly while
quietly costing the property the migration existed to establish.

What is lost is portability. `ttr project import` rewrites an adopted corpus to
store-relative form precisely so a project directory can be MOVED — copied to
another machine, relocated by ``--mode move`` — and still resolve. A row holding
an absolute path breaks the moment it is, and breaks silently on the machine that
wrote it, which is the last place anyone looks.

The importer and the writer must therefore produce ONE shape. The scan below pins
it structurally rather than by inspection, the same trick `test_isolation.py`'s
moved-setting scan uses and for the same reason: nobody remembers a convention,
and a convention nobody remembers is not one.
"""

from __future__ import annotations

import pathlib

from ttr.enrichment.base import DescriptionRead, TaxonomicRead
from ttr.pipeline import Pipeline
from ttr.sources.email_source import EmailSource
from ttr.sources.seen_store import SeenStore

from conftest import FakeDescriptionEnricher, FakeTaxonomicEnricher, load_eml


class _FixtureFetcher:
    def __init__(self, names):
        self._names = names

    def fetch_unseen(self):
        for name in self._names:
            msg = load_eml(name)
            yield (msg["Message-ID"] or "").strip(), msg


def _pipeline(repo, alias_map, tmp_path, names):
    return Pipeline(
        source=EmailSource(_FixtureFetcher(names), SeenStore(tmp_path / "seen.txt")),
        taxonomic=FakeTaxonomicEnricher(TaxonomicRead(
            provider="bioclip", model_name="fake", topk=[("Vulpes vulpes", 0.9)],
            embedding=[0.1], ok=True)),
        description=FakeDescriptionEnricher(DescriptionRead(
            provider="ollama", model_name="fake", text="A bird.", ok=True)),
        repo=repo, alias_map=alias_map, target_taxonomy=None,
        image_store_dir=tmp_path / "images")


def _is_portable(stored) -> bool:
    """Store-relative and POSIX-separated — resolvable from any CWD, on any OS.

    Absoluteness is checked under BOTH flavours deliberately. `PurePath` is
    `PureWindowsPath` here, and it does not consider `/var/x` absolute — so a
    Windows-only check would pass a POSIX absolute path straight through, and
    this corpus is written on Windows and may well be read on Linux.
    """
    if stored is None:
        return False
    return (not pathlib.PurePosixPath(stored).is_absolute()
            and not pathlib.PureWindowsPath(stored).is_absolute()
            and "\\" not in stored)


def _stored_paths(repo):
    rows = repo._conn.execute(
        "SELECT source_message_id, original_image_path, boxed_image_path "
        "FROM detection_events").fetchall()
    return [(r[0], col, p) for r in rows
            for col, p in (("original_image_path", r[1]), ("boxed_image_path", r[2]))
            if p is not None]


# --------------------------------------------------------------------------- #
# The structural guard.
# --------------------------------------------------------------------------- #
def test_no_stored_image_path_is_absolute_or_backslash_separated(repo, alias_map, tmp_path):
    """The scan that fails the build. One shape, produced in one place."""
    _pipeline(repo, alias_map, tmp_path, ["both_attachments.eml"]).run_once()

    paths = _stored_paths(repo)
    assert paths, "precondition: the fixture stored at least one image path"

    offenders = [f"{mid} {col}: {p}" for mid, col, p in paths if not _is_portable(p)]
    assert not offenders, (
        f"{len(offenders)} stored path(s) are not store-relative POSIX form — a "
        f"project carrying these stops resolving the moment its directory moves:\n  "
        + "\n  ".join(offenders))


def test_a_stored_path_resolves_against_the_image_store(repo, alias_map, tmp_path):
    """Relative is only useful if it still resolves. Both halves, together."""
    _pipeline(repo, alias_map, tmp_path, ["both_attachments.eml"]).run_once()

    store = tmp_path / "images"
    for mid, col, p in _stored_paths(repo):
        assert (store / p).is_file(), f"{mid} {col} does not resolve: {p}"


def test_the_stored_shape_matches_what_the_importer_produces(repo, alias_map, tmp_path):
    """`<message-id-dir>/<filename>` — the shape `_split_stored_path` expects.

    Pinned against the importer's own parser rather than a hand-written regex, so
    the two cannot drift apart without this failing.
    """
    from ttr.projects.importer import _split_stored_path

    _pipeline(repo, alias_map, tmp_path, ["both_attachments.eml"]).run_once()

    for mid, col, p in _stored_paths(repo):
        assert p.count("/") == 1, f"expected <dir>/<file>, got {p!r}"
        root, message_dir = _split_stored_path(p, "images")
        assert root == "", "a freshly written path has no extra root segment"
        assert message_dir, "the importer must still find a message-id directory"


def test_the_portability_check_would_still_catch_a_violation():
    """A guard on the guard: the shapes this once wrote must still be rejected."""
    assert not _is_portable(r"C:\store\abc\img.jpg"), "absolute Windows path"
    assert not _is_portable("/var/store/abc/img.jpg"), "absolute POSIX path"
    assert not _is_portable(r"abc\img.jpg"), "relative but backslash-separated"
    assert _is_portable("abc/img.jpg"), "the shape that is actually wanted"
