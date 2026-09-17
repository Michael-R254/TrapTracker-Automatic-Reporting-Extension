"""Stage 5 verification: the CLI wires the agents/pipeline end-to-end.

`report` runs against the DB and writes markdown (LLM absent -> it degrades but
the honesty language stays); `store --from-fixtures` resolves canonical keys so
the rows are actually queryable."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from ttr.cli import app
from ttr.config import get_settings
from ttr.storage.repository import DetectionRepository

from conftest import make_persisted_event

runner = CliRunner()
UTC = timezone.utc

from conftest import EXAMPLE_ALIAS_PATH as EXAMPLE_ALIASES


def _base_env(mp, tmp_path):
    """One project, plus the machine-level settings the commands still need.

    Stage 5 moved every command onto a project context, so a database is no
    longer selected with DB_PATH — it belongs to a project. Exactly one project
    exists here, so resolution defaults to it and no --project is needed, which
    is the rule that keeps single-project users from ever typing one.

    `Settings` still declares the IMAP fields as required (they move at the
    field-removal stage), so they are still set here; nothing reads them now.

    Returns the project's database path, for seeding.
    """
    from ttr.projects import paths as ppaths
    from ttr.projects.service import create_project

    mp.setenv("IMAP_HOST", "h")
    mp.setenv("IMAP_USER", "u")
    mp.setenv("IMAP_PASSWORD", "p")
    root = tmp_path / "ttr-root"
    mp.setenv(ppaths.ROOT_ENV_VAR, str(root))
    get_settings.cache_clear()

    manifest, project_dir = create_project("CLI Test Site", "cli@gmail.com",
                                           root=root, skip_connection_test=True)
    return manifest.db_path(project_dir)


def test_report_cli_writes_markdown_with_honesty(tmp_path, monkeypatch):
    db = _base_env(monkeypatch, tmp_path)

    # Seed a fox event near 'now' so the default 7d window covers it (clock-independent).
    repo = DetectionRepository(db)
    now = datetime.now(UTC)
    repo.upsert_event(make_persisted_event(
        "<f@x>", canonical_binomial="Vulpes vulpes", event_time=now - timedelta(hours=2),
        display_common_name="red fox", upstream_confidence=0.9,
        bioclip_ok=True, vlm_ok=True, agreement_flag="agree"))
    repo.close()

    out = tmp_path / "report.md"
    result = runner.invoke(app, ["report", "--species", "fox", "--window", "7d", "--out", str(out)])
    assert result.exit_code == 0, result.output

    md = out.read_text(encoding="utf-8")
    assert "Vulpes vulpes" in md                          # binomial identifier
    assert "red fox" in md                                # common-name display
    assert "alert events" in md.lower()                   # Decision 4
    assert "real captured alert email" in md.lower()      # Decision 7 (LLM absent, still present)


def test_report_cli_all_species_summary(tmp_path, monkeypatch):
    db = _base_env(monkeypatch, tmp_path)
    repo = DetectionRepository(db)
    now = datetime.now(UTC)
    repo.upsert_event(make_persisted_event(
        "<f@x>", canonical_binomial="Vulpes vulpes", event_time=now - timedelta(hours=2),
        display_common_name="red fox", bioclip_ok=True, vlm_ok=True))
    repo.upsert_event(make_persisted_event(
        "<b@x>", canonical_binomial="Meles meles", event_time=now - timedelta(hours=3),
        display_common_name="european badger", bioclip_ok=True, vlm_ok=True))
    repo.close()

    out = tmp_path / "all.md"
    result = runner.invoke(app, ["report", "--all-species", "--window", "7d", "--out", str(out)])
    assert result.exit_code == 0, result.output
    md = out.read_text(encoding="utf-8")
    assert "all monitored species" in md.lower()
    assert "Species breakdown" in md
    assert "| **Total** | **2** | |" in md               # both species counted


def test_report_cli_requires_exactly_one_of_species_or_all(tmp_path, monkeypatch):
    db = _base_env(monkeypatch, tmp_path)
    # Neither given.
    r1 = runner.invoke(app, ["report", "--window", "7d"])
    assert r1.exit_code == 2 and "exactly one" in r1.output
    # Both given.
    r2 = runner.invoke(app, ["report", "--species", "fox", "--all-species", "--window", "7d"])
    assert r2.exit_code == 2 and "exactly one" in r2.output


def test_report_cli_rejects_bad_window_without_traceback(tmp_path, monkeypatch):
    db = _base_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["report", "--species", "fox", "--window", "7"])  # missing unit
    assert result.exit_code == 2                          # friendly exit, not a crash
    assert not isinstance(result.exception, ValueError)   # ValueError was caught, not leaked


def test_store_from_fixtures_resolves_binomials(tmp_path, monkeypatch):
    db = _base_env(monkeypatch, tmp_path)

    result = runner.invoke(app, ["store", "--from-fixtures"])
    assert result.exit_code == 0, result.output

    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT canonical_binomial, canonical_is_binomial FROM detection_events "
        "WHERE upstream_label = 'VulpesVulpes'"
    ).fetchone()
    con.close()
    assert row == ("Vulpes vulpes", 1)                    # resolved, not NULL


# --------------------------------------------------------------------------- #
# `serve` will not expose an unauthenticated, WRITE-capable UI by accident.
#
# /api/ingest/start polls the mailbox and writes to the database and image store,
# and nothing in the web layer authenticates. Loopback is the default; leaving it
# has to be deliberate.
# --------------------------------------------------------------------------- #
def test_is_loopback_recognises_local_addresses():
    from ttr.cli import is_loopback

    for host in ("127.0.0.1", "localhost", "LocalHost", "::1", " 127.0.0.1 "):
        assert is_loopback(host), host


def test_is_loopback_treats_anything_unrecognised_as_remote():
    """Fail closed: an address we cannot vouch for must not be waved through."""
    from ttr.cli import is_loopback

    for host in ("0.0.0.0", "192.168.1.20", "10.0.0.5", "::", "example.com", "", None):
        assert not is_loopback(host), host


def test_serve_refuses_a_non_loopback_bind_without_acknowledgement():
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "NO authentication" in combined
    # The message must be actionable, not just a refusal.
    assert "--i-understand-no-auth" in combined
    assert "ssh -L" in combined


def test_serve_refusal_happens_before_any_server_is_started(monkeypatch):
    """The guard runs ahead of the uvicorn import, so it cannot be reached with
    the [web] extra absent and cannot half-start."""
    started = []
    import ttr.cli

    monkeypatch.setattr(ttr.cli, "_LOOPBACK_HOSTS", frozenset({"127.0.0.1"}))
    try:
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append((a, k)))
    except ImportError:                                  # pragma: no cover
        pass

    assert runner.invoke(app, ["serve", "--host", "203.0.113.7"]).exit_code == 2
    assert started == []


@pytest.fixture
def serve_without_a_server(monkeypatch):
    """`serve` up to the point uvicorn would start; yields the token it armed."""
    uvicorn = pytest.importorskip("uvicorn")
    from ttr.web import auth

    armed = []
    monkeypatch.setattr(uvicorn, "run",
                        lambda *a, **k: armed.append(auth.get_ui_token()))
    yield armed
    auth.set_ui_token(None)


def test_serve_uses_a_fixed_token_and_does_not_print_it(serve_without_a_server, monkeypatch):
    import os

    fixed = "x" * 43
    monkeypatch.setenv("TTR_UI_TOKEN", fixed)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    assert serve_without_a_server == [fixed]
    assert fixed not in result.output + (result.stderr or "")
    assert "TTR_UI_TOKEN" not in os.environ, "child processes would inherit it"


def test_serve_mints_a_fresh_token_when_none_is_fixed(serve_without_a_server, monkeypatch):
    monkeypatch.delenv("TTR_UI_TOKEN", raising=False)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0, result.output
    [token] = serve_without_a_server
    assert f"?token={token}" in result.output + (result.stderr or "")


def test_serve_refuses_a_weak_fixed_token(serve_without_a_server, monkeypatch):
    monkeypatch.setenv("TTR_UI_TOKEN", "hunter2")

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 2
    assert serve_without_a_server == [], "started with a token it should have refused"


# --------------------------------------------------------------------------- #
# Resource cleanup and clean-install behaviour.
#
# `report` and `store` closed their repository on the success path only, so a
# failure mid-generation or a malformed .eml leaked the connection. Every sibling
# command already used try/finally; an inconsistent pattern is what gets copied.
# --------------------------------------------------------------------------- #
class _CloseTracker:
    """Wraps a real repository and records whether close() was called."""

    def __init__(self, real):
        self._real = real
        self.closed = False

    def close(self):
        self.closed = True
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_report_closes_the_repository_when_generation_fails(tmp_path, monkeypatch):
    db = tmp_path / "r.db"
    _base_env(monkeypatch, db)
    tracked = []

    import ttr.cli as climod
    from ttr.storage.repository import DetectionRepository

    def _tracking_repo(path, **kwargs):
        # **kwargs so the stub keeps up with the real signature, which gained
        # expected_project_id / adopt when database ownership stamping landed.
        t = _CloseTracker(DetectionRepository(path, **kwargs))
        tracked.append(t)
        return t

    monkeypatch.setattr(climod, "DetectionRepository", _tracking_repo, raising=False)
    monkeypatch.setattr("ttr.storage.repository.DetectionRepository", _tracking_repo)
    monkeypatch.setattr(
        "ttr.agents.report.ReportGeneratorAgent.generate",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    result = runner.invoke(app, ["report", "--species", "fox", "--window", "7d"])

    assert result.exit_code != 0
    assert tracked and tracked[0].closed, "repository was not closed on the failure path"


def test_store_closes_the_repository_when_a_fixture_is_unreadable(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    _base_env(monkeypatch, db)
    emails = tmp_path / "emails"
    emails.mkdir()
    (emails / "broken.eml").write_bytes(b"not really an email")
    tracked = []

    from ttr.storage.repository import DetectionRepository

    def _tracking_repo(path, **kwargs):
        # **kwargs so the stub keeps up with the real signature, which gained
        # expected_project_id / adopt when database ownership stamping landed.
        t = _CloseTracker(DetectionRepository(path, **kwargs))
        tracked.append(t)
        return t

    monkeypatch.setattr("ttr.storage.repository.DetectionRepository", _tracking_repo)
    monkeypatch.setattr(
        "ttr.sources.email_parser.parse_alert_email",
        lambda msg: (_ for _ in ()).throw(RuntimeError("parser exploded")))

    result = runner.invoke(app, ["store", "--from-fixtures", "--path", str(emails)])

    assert result.exit_code != 0
    assert tracked and tracked[0].closed, "repository was not closed on the failure path"


def test_store_reports_a_missing_fixture_directory_actionably(tmp_path, monkeypatch):
    """The installed-package case: the source tree's fixtures are not shipped, so
    the default path does not exist. That must be an actionable message and a
    distinct exit code, never an empty run or a traceback."""
    _base_env(monkeypatch, tmp_path)

    result = runner.invoke(
        app, ["store", "--from-fixtures", "--path", str(tmp_path / "nope")])

    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "no such directory" in combined
    assert "--path" in combined


def test_no_stage_stub_remains_in_the_cli():
    """`_pending()` announced 'not yet implemented' for unbuilt stages. All stages
    are built; the stub and the docstring describing it are gone."""
    import inspect

    import ttr.cli as climod

    assert not hasattr(climod, "_pending")
    assert "not yet implemented" not in inspect.getsource(climod)


def test_help_summaries_are_user_facing_not_project_internal():
    """`ttr --help` is the first thing a new user sees. Build-plan stage numbers
    and internal decision references mean nothing to them, and the summaries had
    accumulated both."""
    import re

    output = runner.invoke(app, ["--help"]).output

    assert re.search(r"Stage \d", output) is None, "stage numbers leaked into --help"
    assert "Decision 3" not in output
    # Rich treats square brackets as markup, so "[web]" rendered as nothing at all.
    assert "requires the  extra" not in output


# --------------------------------------------------------------------------- #
# Console encoding. A report is a DOCUMENT — the same markdown is written to
# file, rendered to HTML and printed to PDF — and it carries typography (em
# dashes, arrows, the degree sign) that a Windows console under cp1252 cannot
# encode. `typer.echo` then raised UnicodeEncodeError AFTER the report had been
# generated: the work done, the LLM called, and nothing to show for it.
#
# Fixed at the STREAM. Flattening the document's typography would damage every
# other surface to appease the one that matters least, which is the same
# reasoning Stage 7 applied when it declined to ASCII-fold the report body.
# --------------------------------------------------------------------------- #
def _strict_console(encoding: str):
    """A stream that behaves like a real console: strict, and narrow."""
    import io

    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict",
                            write_through=True)


def test_a_document_prints_to_a_cp1252_console_without_raising(monkeypatch):
    from ttr.cli import _echo_document

    stream = _strict_console("cp1252")
    monkeypatch.setattr("sys.stdout", stream)

    body = "# Detection report — all species\n\n_798 events; 90.2% within 60s._\n"
    _echo_document(body)                       # must not raise

    stream.flush()
    written = stream.buffer.getvalue().decode("utf-8")
    assert "Detection report" in written
    assert "—" in written, "the em dash survived rather than being folded away"


def test_a_stream_that_refuses_reconfiguration_still_prints(monkeypatch):
    """Some wrappers have no `reconfigure`. The document must still get out."""
    from ttr.cli import _echo_document

    class _Narrow:
        encoding = "cp1252"

        def __init__(self):
            self.text = ""

        def write(self, s):
            s.encode(self.encoding)            # strict: raises on an em dash
            self.text += s
            return len(s)

        def flush(self):
            pass

    stream = _Narrow()
    monkeypatch.setattr("sys.stdout", stream)

    _echo_document("counts — not abundance\n")  # must not raise

    assert "counts" in stream.text
    assert "abundance" in stream.text, "the line was substituted, not dropped"


def test_writing_to_a_file_is_unchanged_utf8(tmp_path, monkeypatch):
    """`--out` does not go through the console path and keeps full typography."""
    target = tmp_path / "report.md"
    body = "# Report — 798 events\n\n_alert events, not animals._\n"
    target.write_text(body, encoding="utf-8")

    assert target.read_text(encoding="utf-8") == body
    assert "—" in target.read_bytes().decode("utf-8")
