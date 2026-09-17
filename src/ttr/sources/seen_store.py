"""Idempotency store: a persisted set of already-processed Message-IDs.

The source's job is to yield only NEW events (plan §5.1). This is a simple,
durable newline-delimited file — one Message-ID per line — loaded into memory on
open and appended on each new id, so a restart does not reprocess mail.
"""

from __future__ import annotations

from pathlib import Path


class SeenStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            with self._path.open("r", encoding="utf-8") as fh:
                self._seen = {line.strip() for line in fh if line.strip()}

    def __contains__(self, message_id: str) -> bool:
        return message_id in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def add(self, message_id: str) -> bool:
        """Record a Message-ID. Returns True if newly added, False if already seen."""
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(message_id + "\n")
        return True
