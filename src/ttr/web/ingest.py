"""Live ingest: run the real pipeline from the browser and watch it work.

The report page is read-only — it renders what is already stored. This module is
the other half: a button that polls the dedicated alert mailbox, a console box
showing the SAME structured log lines the CLI prints, and a strip where each
image appears as it is processed, captioned with the upstream label, the BioCLIP
top-1 and the cross-check verdict (with, on a disagreement, how far off it was).

Three things shape the design.

**A poll is silent for minutes before it says anything.** ``EmailFetcher`` fetches
the whole mailbox into memory, closes the IMAP connection, and only then yields
(so a long enrichment pass never holds the session open). Between
``imap_poll_start`` and ``imap_poll_done`` the pipeline emits nothing at all, so
this module writes its OWN phase lines — otherwise the button looks broken.

**The job owns its own database connection.** ``storage.migrations.connect`` uses
``sqlite3.connect``'s default ``check_same_thread=True``, so the per-request
``get_repo`` dependency cannot be handed to a background thread. The job thread
builds its own bundle and closes it in a ``finally``.

**Everything here is email-derived and untrusted.** Labels,
message-ids, parse warnings and the formatted log lines all quote attacker-
controllable text. The report page defends with server-side ``nh3``; this page
defends by never treating the payload as markup at all — it is JSON, rendered
exclusively through ``textContent``.

Single-process by construction: ``RUNNER`` is a module-level singleton holding a
thread, so ``ttr serve`` must stay single-worker (it is — ``cli.py`` calls
``uvicorn.run`` with no ``workers``). Do not add ``--workers`` without moving the
job state out of process.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Literal, Optional
from uuid import uuid4

from ..logging import get_logger, key_value_formatter
from ..thumbnails import thumb_data_uri

logger = get_logger(__name__)

#: One shared instance for the runner's own phase lines (see IngestRunner._phase).
_PHASE_FORMATTER = key_value_formatter()

#: Ring size for the combined log+image stream. A run left going overnight evicts
#: early history; the client is TOLD how many entries it missed rather than being
#: quietly handed a partial replay.
_MAX_EVENTS = 2000

#: How many thumbnails stay resident. Data URIs are ~8 KB each, so older cards
#: keep their captions and lose only the picture.
_MAX_IMAGE_CARDS = 60

IngestState = Literal["idle", "running", "stopping", "done", "failed"]

#: Builds the pipeline INSIDE the job thread. A factory, not a pipeline, because
#: of the sqlite thread-affinity noted in the module docstring.
PipelineFactory = Callable[[Callable[[dict], None]], object]


class IngestBusy(RuntimeError):
    """A job is already running. Mapped to HTTP 409 — never a 500."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------- buffer
class IngestBuffer:
    """Thread-safe append-only ring with monotonic sequence numbers.

    The producer is the job thread; the consumers are request threads polling the
    state endpoint. One lock, two small critical sections. Log lines and image
    cards share ONE sequence so their interleaving is preserved — the line about
    an image and the card for it arrive in the order they happened.
    """

    def __init__(self, max_events: int = _MAX_EVENTS) -> None:
        self._max = max_events
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._next_seq = 1
        self._first_seq = 1          # seq of _entries[0]; rises as the ring evicts

    def append(self, kind: str, payload: dict) -> None:
        with self._lock:
            self._entries.append({"seq": self._next_seq, "kind": kind, "payload": payload})
            self._next_seq += 1
            if kind == "image":
                self._trim_thumbs_locked()
            while len(self._entries) > self._max:
                self._entries.pop(0)
                self._first_seq += 1

    def _trim_thumbs_locked(self) -> None:
        """Keep only the newest _MAX_IMAGE_CARDS thumbnails resident. Older cards
        keep their caption and drop the picture, so memory stays bounded without
        the history developing holes."""
        seen = 0
        for entry in reversed(self._entries):
            if entry["kind"] != "image":
                continue
            seen += 1
            if seen > _MAX_IMAGE_CARDS and entry["payload"].get("thumb") is not None:
                entry["payload"]["thumb"] = None

    def since(self, cursor: int) -> tuple[list[dict], int, int]:
        """Return ``(entries after cursor, new cursor, dropped)``.

        ``dropped`` counts entries evicted before the caller could read them, so
        the page can say "N earlier lines not shown" instead of silently losing
        them.
        """
        with self._lock:
            # Also correct for a fresh client at cursor 0 joining mid-run: it
            # really did miss the evicted lines, and saying so is the honest
            # answer rather than reporting a complete history.
            dropped = max(0, self._first_seq - 1 - cursor)
            out = [e for e in self._entries if e["seq"] > cursor]
            return out, self._next_seq - 1, dropped

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._first_seq = self._next_seq


# ---------------------------------------------------------------- log capture
class _JobLogHandler(logging.Handler):
    """Captures the ingest job thread's log records into the buffer.

    Scoped two ways so a concurrent ``/api/report`` request can never leak into
    the console box:

    * by THREAD — ``LogRecord.thread`` is ``threading.get_ident()`` at creation
      time, and the job runs on its own dedicated thread;
    * by ORIGIN — records from ``ttr.*``, plus WARNING+ from anything else, so a
      real ``imaplib``/``httpx`` failure is still visible without third-party
      DEBUG noise.

    Formats with the shared :func:`ttr.logging.key_value_formatter`, so a line in
    the browser is byte-identical to the line the CLI prints.
    """

    def __init__(self, buffer: IngestBuffer, thread_id: int) -> None:
        super().__init__(level=logging.DEBUG)
        self._buffer = buffer
        self._thread_id = thread_id
        self.setFormatter(key_value_formatter())

    def _wanted(self, record: logging.LogRecord) -> bool:
        # logging.logThreads can be disabled globally, making record.thread None.
        # Degrade to over-capture rather than showing an empty console.
        if record.thread is not None and record.thread != self._thread_id:
            return False
        if record.name.startswith("ttr"):
            return True
        return record.levelno >= logging.WARNING

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self._wanted(record):
                return
            self._buffer.append("log", {"level": record.levelname,
                                        "line": self.format(record)})
        except Exception:
            # A handler must never raise into the pipeline it is watching.
            self.handleError(record)


# --------------------------------------------------------------------- runner
def friendly_error(exc: BaseException, settings=None) -> str:
    """Turn a start-up failure into one actionable line.

    NEVER formats a Settings object or any credential: a pydantic ValidationError
    for a missing password would otherwise render the model, and an IMAP failure
    is reported with the HOST only.
    """
    name = type(exc).__name__
    text = str(exc)

    # Project-layer problems come back already actionable — `projects/errors.py`
    # opens by promising every one of them carries an actionable message, and
    # each does: install a backend, run set-password, scope the command, add a
    # mailbox. Passing them through beats re-summarising them into something
    # vaguer, and beats prefixing a good message with its own class name. This
    # replaced a branch that told the user to set IMAP_HOST, IMAP_USER and
    # IMAP_PASSWORD in .env: those keys are no longer read, so that advice would
    # now send someone to edit a file nothing consults.
    from ..projects.errors import ProjectError

    if isinstance(exc, ProjectError):          # CredentialError is one of these
        return text

    if isinstance(exc, ImportError):
        missing = str(getattr(exc, "name", "") or text)
        if any(k in missing for k in ("torch", "bioclip", "open_clip", "PIL", "Pillow")):
            return ("BioCLIP is not installed — install the enrich extra: "
                    'pip install -e ".[enrich]"')
        return f"A required package is missing: {text}"
    if "login" in text.lower() or "IMAP4" in name or "Mailbox" in name:
        host = getattr(settings, "imap_host", None) if settings is not None else None
        where = f" for host {host}" if host else ""
        return f"IMAP login failed{where}: {name}"
    return f"{name}: {text}"


class IngestRunner:
    """One ingest job at a time, on a daemon background thread.

    Deliberately server-side: closing the browser does NOT stop a run. The job
    keeps going, the (bounded) buffer keeps filling, and reopening the page
    replays the history and adopts the job — the same semantics as leaving
    ``ttr run`` in a terminal.
    """

    def __init__(self, buffer: Optional[IngestBuffer] = None) -> None:
        self._buffer = buffer or IngestBuffer()
        self._lock = threading.RLock()
        self._state: str = "idle"
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._run_id = ""
        self._mode = ""
        self._level = "info"
        self._error: Optional[str] = None
        #: Whether THIS run ended because someone pressed Stop.
        #:
        #: `stop()` sets the state to "stopping" and the loop then finishes
        #: normally into "done", so the terminal state alone cannot tell a run
        #: that completed from one that was cut short. Those are different
        #: facts, and the counts under the second are partial — a page that
        #: labels them the same way overstates what was collected.
        #:
        #: Display only. Nothing branches on it, and the pipeline never sees it.
        self._stopped = False
        self._stored = 0
        self._polls = 0
        self._started_at: Optional[str] = None
        self._started_monotonic = 0.0
        self._poll_seconds = 0
        #: Which project this run belongs to. Without it, a second project's page
        #: would poll /api/ingest/state and be handed THIS project's live log and
        #: image strip as though they were its own.
        self._project_id: Optional[str] = None
        self._project_name: Optional[str] = None
        #: Holds the on-disk ingest lock for the job's lifetime, so this server
        #: and a `ttr run` in a terminal contend with each other rather than both
        #: polling the same mailbox.
        self._lock_stack = None

    # -- state ------------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set_state(self, state: str, *, error: Optional[str] = None) -> None:
        with self._lock:
            self._state = state
            if error is not None:
                self._error = error

    def _phase(self, message: str, level: str = "INFO") -> None:
        """A line the RUNNER writes, distinct from a pipeline log record. These
        exist because the mailbox fetch is silent for minutes (module docstring).

        Built as a real LogRecord and put through the SAME formatter, so a phase
        line is indistinguishable in shape, timestamp basis and quoting from the
        pipeline's own lines. Appending a hand-rolled string instead would put two
        different clocks and two different quoting rules in one console.
        """
        record = logging.makeLogRecord({
            "name": "ttr.web.ingest",
            "levelno": getattr(logging, level, logging.INFO),
            "levelname": level,
            "msg": message,
            "args": (),
        })
        self._buffer.append("log", {"level": level,
                                    "line": _PHASE_FORMATTER.format(record)})

    # -- control ----------------------------------------------------------
    def owns(self, project_id: str) -> bool:
        """Whether this runner's CURRENT job belongs to ``project_id``.

        Idle counts as owned by anyone: there is no run to confuse.
        """
        with self._lock:
            if self._state in ("running", "stopping"):
                return self._project_id == project_id
            return self._project_id in (None, project_id)

    def project(self) -> dict:
        with self._lock:
            return {"id": self._project_id, "name": self._project_name}

    def start(self, *, mode: str, level: str, factory: PipelineFactory,
              project_id: Optional[str] = None,
              project_name: Optional[str] = None,
              project_dir=None) -> dict:
        from contextlib import ExitStack

        from ..projects.lock import ingest_lock

        with self._lock:
            if self._state in ("running", "stopping"):
                raise IngestBusy("An ingest run is already in progress.")
            if self._thread is not None:
                self._thread.join(timeout=0)

            # Taken HERE, synchronously, so a lock held elsewhere is refused on
            # the button press rather than surfacing as a failed job seconds
            # later. Released in _run's finally, on the job thread — a file lock
            # is not thread-bound, so that is safe.
            stack = ExitStack()
            if project_dir is not None:
                stack.enter_context(ingest_lock(project_dir))   # may raise IngestLocked
            self._lock_stack = stack

            self._project_id = project_id
            self._project_name = project_name
            self._buffer.clear()
            self._run_id = uuid4().hex[:8]
            self._mode = mode
            self._level = level
            self._error = None
            # Reset with the rest of the per-run state: a run that follows a
            # stopped one is not itself a stopped run.
            self._stopped = False
            self._stored = 0
            self._polls = 0
            self._poll_seconds = 0
            self._started_at = _utc_now_iso()
            self._started_monotonic = time.monotonic()
            # A FRESH Event per job: reusing a set one would stop the next run
            # the instant it started.
            self._stop = threading.Event()
            self._state = "running"
            self._thread = threading.Thread(
                target=self._run, args=(mode, level, factory),
                name="ttr-ingest", daemon=True)
            self._thread.start()
        return self.snapshot()

    def stop(self) -> dict:
        with self._lock:
            if self._state == "running":
                self._state = "stopping"
                self._stopped = True
                self._stop.set()
                self._phase("stop_requested — will finish the current image first")
        return self.snapshot()

    def snapshot(self, cursor: int = 0) -> dict:
        events, new_cursor, dropped = self._buffer.since(cursor)
        with self._lock:
            elapsed = (round(time.monotonic() - self._started_monotonic, 1)
                       if self._started_monotonic else 0.0)
            return {
                "run_id": self._run_id,
                "project_id": self._project_id,
                "project_name": self._project_name,
                "state": self._state,
                "stopped_by_user": self._stopped,
                "mode": self._mode,
                "level": self._level,
                "cursor": new_cursor,
                "dropped": dropped,
                "stored": self._stored,
                "polls": self._polls,
                "started_at": self._started_at,
                "elapsed_s": elapsed,
                "poll_seconds": self._poll_seconds,
                "error": self._error,
                "events": events,
            }

    # -- the job ----------------------------------------------------------
    def _on_progress(self, payload: dict) -> None:
        """Called on the job thread, from ``Pipeline._notify``."""
        card = dict(payload)
        # Prefer the boxed frame (what the VLM saw), matching the report's
        # Appendix-A choice. None when Pillow is absent or the file is missing —
        # the card then renders caption-only.
        card["thumb"] = thumb_data_uri(
            card.get("boxed_image_path") or card.get("original_image_path"), max_px=200)
        # Filesystem paths never reach the browser (the same promise the report makes).
        card.pop("original_image_path", None)
        card.pop("boxed_image_path", None)
        self._buffer.append("image", card)

    def _run(self, mode: str, level: str, factory: PipelineFactory) -> None:
        handler = _JobLogHandler(self._buffer, threading.get_ident())
        root = logging.getLogger()
        prior_level = root.level
        # configure_logging() is never called in the uvicorn process, so root may
        # sit at WARNING and drop every INFO record BEFORE any handler sees it.
        # Raise it for the job's lifetime and restore it in the finally.
        root.setLevel(logging.DEBUG if level == "debug" else logging.INFO)
        root.addHandler(handler)
        bundle = None
        try:
            self._phase(f"ingest_run_start mode={mode} level={level}")
            self._phase("building_pipeline — loading config, alias table and models")
            bundle = factory(self._on_progress)
            with self._lock:
                self._poll_seconds = getattr(bundle, "poll_seconds", 0)
            while True:
                self._phase("polling_mailbox — the whole mailbox is fetched before "
                            "enrichment begins; this can take minutes")
                n = bundle.pipeline.run_once()
                with self._lock:
                    self._stored += n
                    self._polls += 1
                self._phase(f"poll_complete stored={n}")
                if mode == "once" or self._stop.is_set():
                    break
                self._phase(f"idle — next poll in {bundle.poll_seconds}s")
                if self._stop.wait(bundle.poll_seconds):   # interruptible sleep
                    break
            self._set_state("done")
            self._phase("ingest_run_done")
        except BaseException as exc:
            logger.exception("ingest_job_failed")
            message = friendly_error(exc)
            self._buffer.append("log", {"level": "ERROR", "line": message})
            self._set_state("failed", error=message)
        finally:
            root.removeHandler(handler)
            root.setLevel(prior_level)
            if bundle is not None:
                try:
                    bundle.close()
                except Exception:
                    logger.warning("ingest_repo_close_failed")
            stack, self._lock_stack = self._lock_stack, None
            if stack is not None:
                try:
                    stack.close()               # releases the on-disk lock
                except Exception:
                    logger.warning("ingest_lock_release_failed")


#: Module-level singleton, mirroring the ``_BODY_CACHE`` style in ``app.py`` —
#: except this one owns a thread, not just a dict.
RUNNER = IngestRunner()
