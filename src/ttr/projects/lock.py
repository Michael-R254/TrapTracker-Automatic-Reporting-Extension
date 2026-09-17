"""An advisory lock so two ingests cannot run against one project at once.

This is not speculative. Phase 1b traced 476 orphan image directories and roughly
121 MB of duplication in the existing image store to exactly this shape:

  * ``SeenStore`` loads its set once in ``__init__`` (``seen_store.py:17-22``) and
    never re-reads, so two processes hold two divergent ideas of what is done;
  * ``Pipeline._persist_images`` is the FIRST statement of ``process_event``
    (``pipeline.py:85``) and writes unconditionally;
  * ``upsert_event`` is ``ON CONFLICT DO NOTHING`` (``repository.py:264-265``).

So the second process re-downloads, re-enriches and re-writes images for rows it
then discards. Nothing is corrupted and nothing is logged - the cost is silent
waste and a store that grows without explanation.

WAL also allows only one writer, and ``migrations.connect`` passes no timeout, so
concurrent writers additionally hit SQLite's default 5-second busy timeout and
burn a full BioCLIP + VLM pass per collision.

**Read paths never take this lock.** WAL permits readers during a write, and
making ``ttr report`` block on an ingest would be a regression for the sake of a
guarantee reports do not need.

THE OPERATING SYSTEM DECIDES WHO HOLDS IT. Until 2026-09-15 the lock was a file
created with ``O_EXCL`` holding a pid and hostname, and liveness was inferred:
refuse if that pid is alive here, or if the hostname is some other machine's. In
a container both inferences fail. The process is PID 7 in every container, so a
lock left by one that was stopped always names a live pid; and a pinned hostname
makes every container "this machine". Measured: a plain ``docker stop`` left the
file behind, and every later ingest was refused until someone deleted it by hand.

Now the file is locked with ``flock`` (POSIX) or a one-byte ``msvcrt`` range
lock (Windows), and the kernel releases that lock when the holding process ends,
however it ends. Nothing is inferred from a pid or a hostname. Both locks belong
to the open file rather than the thread, so the web runner can release on the job
thread what it took on the request thread; and a second open of the file, even in
the same process, is refused. POSIX ``lockf``/``fcntl`` record locks would not
do: they belong to the process, and a ``holder()`` probe closing its own handle
would silently drop the real lock.

The file is permanent. Deleting it while another process has it open lets two
processes lock two different files at once. The holder writes its pid, host and
start time into it for the refusal message, and truncates it on a clean release.
The pid and host are information for a person, never evidence of liveness.

The guarantee is only as good as the filesystem's locks. Local disks and Docker
volumes honour them. A folder synced between machines does not. A filesystem
that refuses the lock call outright is refused here too, rather than ingesting
unprotected.
"""

from __future__ import annotations

import errno
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..logging import get_logger
from .errors import ProjectError

logger = get_logger(__name__)

LOCK_NAME = ".ttr-ingest.lock"

#: Windows locks one byte this far into the file, clear of the metadata. A locked
#: byte range cannot be read by other handles, so locking byte 0 would hide the
#: holder's details from the very message that reports them. Locking past the
#: end of the file is allowed and does not grow it (measured).
_WINDOWS_LOCK_OFFSET = 1 << 30

#: errno values meaning "someone else holds it", not "this cannot be locked".
#: Windows `msvcrt.locking` reports EACCES (measured); flock reports EWOULDBLOCK.
_HELD_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})

#: How long an acquire keeps trying before it refuses. `holder()` probes by
#: taking the lock for a moment; without a short retry, an ingest started at that
#: instant would be refused by a lock nobody was holding.
_ACQUIRE_WINDOW_SECONDS = 0.5
_POLL_SECONDS = 0.05


class IngestLocked(ProjectError):
    """Another ingest holds this project's lock."""


class IngestLockUnsupported(IngestLocked):
    """The filesystem refused to lock at all.

    A subclass of `IngestLocked` so every caller that already refuses cleanly on
    a held lock also refuses cleanly here, rather than meeting a traceback.
    """


def lock_path(project_dir: Path) -> Path:
    return Path(project_dir) / LOCK_NAME


# --------------------------------------------------------------- OS primitive
if os.name == "nt":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _HELD_ERRNOS:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _HELD_ERRNOS:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _open(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | (os.O_CREAT if create else 0)
    return os.open(path, flags, 0o644)


# -------------------------------------------------------------------- reading
def read_lock(project_dir: Path) -> Optional[dict]:
    """The details the last holder wrote, or None if there are none.

    Details only: a crashed holder leaves them behind, so their presence says
    nothing about whether anyone holds the lock. Ask `holder` for that.
    """
    path = lock_path(project_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    info: dict = {}
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        if key:
            info[key.strip()] = value.strip()
    if not info:
        return None
    try:
        info["pid"] = int(info.get("pid", "0"))
    except ValueError:
        info["pid"] = 0
    return info


def holder(project_dir: Path) -> Optional[dict]:
    """The details of whoever holds this project's ingest lock, or None.

    Decided by trying the lock, not by reading the file: if it can be taken,
    nobody holds it, whatever the file says.
    """
    path = lock_path(project_dir)
    try:
        fd = _open(path, create=False)            # a probe never creates the file
    except OSError:
        return None
    try:
        deadline = time.monotonic() + _ACQUIRE_WINDOW_SECONDS
        while True:
            try:
                free = _try_lock(fd)
            except OSError:
                # A filesystem that cannot lock cannot be ingesting either:
                # `ingest_lock` refuses on it.
                return None
            if free:
                _unlock(fd)
                return None
            info = read_lock(project_dir)
            if info and info.get("pid"):
                return info
            # Held, but no details yet: a holder between locking and writing,
            # or another probe. Give either a moment before answering.
            if time.monotonic() >= deadline:
                return info or {"pid": 0}
            time.sleep(_POLL_SECONDS)
    finally:
        os.close(fd)


def describe(info: dict) -> str:
    started = info.get("started", "an unknown time")
    host = info.get("host", "this machine")
    pid = info.get("pid") or "unknown"
    return (f"an ingest is already running for this project "
            f"(pid {pid}, started {started}, on {host})")


# ------------------------------------------------------------------- holding
@contextmanager
def ingest_lock(project_dir: Path):
    """Hold this project's ingest lock for the duration of the block.

    Raises `IngestLocked` when another process, or another open of the file in
    this one, holds it; `IngestLockUnsupported` when the filesystem cannot lock.
    """
    path = lock_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _open(path, create=True)
    try:
        deadline = time.monotonic() + _ACQUIRE_WINDOW_SECONDS
        while True:
            try:
                acquired = _try_lock(fd)
            except OSError as exc:
                raise IngestLockUnsupported(
                    f"the filesystem holding {path.parent} refused a file lock "
                    f"({exc}). Refusing to ingest without one: nothing would stop a "
                    f"second ingest running against this project at the same "
                    f"time.") from exc
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise IngestLocked(describe(read_lock(project_dir) or {})) from None
            time.sleep(_POLL_SECONDS)
    except BaseException:
        os.close(fd)
        raise

    try:
        # Details still in the file mean the last holder did not release
        # cleanly: killed, crashed, or the machine lost power.
        leftover = read_lock(project_dir)
        if leftover:
            logger.warning("ingest_lock_stale_reclaimed",
                           extra={"pid": leftover.get("pid"),
                                  "host": leftover.get("host"), "path": str(path)})
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, (f"pid={os.getpid()}\n"
                      f"host={socket.gethostname()}\n"
                      f"started={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                      ).encode("utf-8"))
    except BaseException:
        os.close(fd)                               # closing releases the lock
        raise

    logger.info("ingest_lock_acquired", extra={"path": str(path)})
    try:
        yield path
    finally:
        try:
            os.ftruncate(fd, 0)    # a clean release leaves no details behind
            _unlock(fd)
        except OSError as exc:
            logger.warning("ingest_lock_not_released",
                           extra={"path": str(path), "error": repr(exc)})
        finally:
            os.close(fd)           # releases the OS lock whatever happened above
