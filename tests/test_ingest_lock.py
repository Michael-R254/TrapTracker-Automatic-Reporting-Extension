"""The ingest lock: held while an ingest runs, and ONLY while one runs.

The lock used to infer liveness from a pid and a hostname written into a file.
In a container that inference is wrong both ways: every container's process is
PID 7, and compose pins the hostname. A `docker stop` left a lock that named a
live pid, and every later ingest was refused until the file was deleted by hand.
The kernel now decides. These tests pin that, including with a real second
process that is killed without running its `finally`.
"""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import ExitStack

import pytest

from ttr.projects import lock
from ttr.projects.lock import (IngestLocked, IngestLockUnsupported, holder,
                               ingest_lock, lock_path, read_lock)


def _refused(project_dir) -> IngestLocked:
    with pytest.raises(IngestLocked) as exc:
        with ingest_lock(project_dir):
            pass
    return exc.value


# --------------------------------------------------------------------------- #
# In one process.
# --------------------------------------------------------------------------- #
def test_a_project_that_never_ingested_has_no_holder(tmp_path):
    assert holder(tmp_path) is None
    assert not lock_path(tmp_path).exists(), "a probe must not create the file"


def test_holding_the_lock_names_the_holder(tmp_path):
    with ingest_lock(tmp_path):
        info = holder(tmp_path)
        assert info["pid"] == os.getpid()
        assert info["host"] == socket.gethostname()
    assert holder(tmp_path) is None


def test_a_second_acquire_is_refused_while_held(tmp_path):
    """Two opens in ONE process conflict: the web test holds the lock in-process
    exactly as `ttr run` would from a terminal."""
    with ingest_lock(tmp_path):
        error = str(_refused(tmp_path))
    assert "an ingest is already running for this project" in error
    assert f"pid {os.getpid()}" in error


def test_probing_for_the_holder_does_not_release_the_lock(tmp_path):
    """A record lock belonging to the process would be dropped when the probe
    closed its own handle. This lock must survive any number of probes."""
    with ingest_lock(tmp_path):
        for _ in range(3):
            assert holder(tmp_path) is not None
        _refused(tmp_path)


def test_the_lock_can_be_released_from_another_thread(tmp_path):
    """The web runner takes the lock on the request thread and releases it on the
    job thread."""
    stack = ExitStack()
    stack.enter_context(ingest_lock(tmp_path))
    closer = threading.Thread(target=stack.close)
    closer.start()
    closer.join(timeout=10)

    assert holder(tmp_path) is None
    with ingest_lock(tmp_path):
        pass


def test_a_clean_release_leaves_no_details_behind(tmp_path):
    with ingest_lock(tmp_path):
        pass
    assert lock_path(tmp_path).exists(), "the file is permanent by design"
    assert read_lock(tmp_path) is None


# --------------------------------------------------------------------------- #
# The file is information, never evidence.
# --------------------------------------------------------------------------- #
def test_a_file_naming_a_live_pid_on_this_host_is_not_a_holder(tmp_path):
    """The container failure, reproduced without a container: the details name a
    process that is alive on this very host (this one), but nobody holds the OS
    lock. The old lock refused here forever."""
    lock_path(tmp_path).write_text(
        f"pid={os.getpid()}\nhost={socket.gethostname()}\n"
        f"started=2026-09-15T00:00:00+00:00\n", encoding="utf-8")

    assert holder(tmp_path) is None
    with ingest_lock(tmp_path):
        assert holder(tmp_path)["started"] != "2026-09-15T00:00:00+00:00"


def test_a_file_from_another_host_is_not_a_holder(tmp_path):
    """The old rule treated another hostname as live, which a recreated container
    with a new random hostname always is."""
    lock_path(tmp_path).write_text(
        "pid=7\nhost=some-other-container\nstarted=2026-09-15T00:00:00+00:00\n",
        encoding="utf-8")

    assert holder(tmp_path) is None
    with ingest_lock(tmp_path):
        pass


def test_a_legacy_or_empty_lock_file_is_not_a_holder(tmp_path):
    lock_path(tmp_path).write_text("", encoding="utf-8")
    assert holder(tmp_path) is None
    with ingest_lock(tmp_path):
        pass


# --------------------------------------------------------------------------- #
# Across processes, including one that dies without cleaning up.
# --------------------------------------------------------------------------- #
_HOLDER = textwrap.dedent("""
    import os, sys, time
    from ttr.projects.lock import ingest_lock
    with ingest_lock(sys.argv[1]):
        print("held", os.getpid(), flush=True)
        time.sleep(120)
""")


def _spawn_holder(project_dir):
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(project_dir)],
                            stdout=subprocess.PIPE, text=True)
    word, _, pid = proc.stdout.readline().strip().partition(" ")
    if word != "held":
        proc.kill()
        pytest.fail("the holder process did not take the lock")
    # The child reports its own pid: on Windows a venv's python.exe is a
    # launcher, so `proc.pid` is not the interpreter holding the lock.
    return proc, int(pid)


def test_another_process_holding_the_lock_is_seen_and_refused(tmp_path):
    proc, pid = _spawn_holder(tmp_path)
    try:
        assert holder(tmp_path)["pid"] == pid
        assert f"pid {pid}" in str(_refused(tmp_path))
    finally:
        proc.kill()
        proc.wait()


def test_a_killed_holder_releases_the_lock(tmp_path):
    """What `docker stop`, an OOM kill or a power cut does: the process ends
    without its `finally`, so the details stay in the file. The lock must not."""
    proc, _pid = _spawn_holder(tmp_path)
    proc.kill()
    proc.wait()

    deadline = time.monotonic() + 10
    while holder(tmp_path) is not None:
        assert time.monotonic() < deadline, "the OS never released a dead process's lock"
        time.sleep(0.05)
    assert read_lock(tmp_path) is not None, "a killed holder leaves its details"
    with ingest_lock(tmp_path):
        pass


# --------------------------------------------------------------------------- #
# When the filesystem cannot lock at all.
# --------------------------------------------------------------------------- #
def test_a_filesystem_that_cannot_lock_is_refused_not_ingested_unprotected(tmp_path,
                                                                         monkeypatch):
    def no_locks(fd):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(lock, "_try_lock", no_locks)

    error = _refused(tmp_path)
    assert isinstance(error, IngestLockUnsupported)
    assert "refused a file lock" in str(error)
    assert holder(tmp_path) is None


# --------------------------------------------------------------------------- #
# The permanent file does not get in the way of deleting a project.
# --------------------------------------------------------------------------- #
def test_a_project_that_has_ingested_can_still_be_deleted(tmp_path, monkeypatch):
    from conftest import make_web_project
    from ttr.projects.errors import ProjectError
    from ttr.projects.service import delete_project

    ctx = make_web_project(tmp_path, monkeypatch, name="Lock Delete Site")
    with ingest_lock(ctx.dir):
        with pytest.raises(ProjectError, match="Stop it before deleting"):
            delete_project(ctx.id)

    assert lock_path(ctx.dir).exists()
    delete_project(ctx.id)                  # no --force: the lock file is ours
    assert not ctx.dir.exists()
