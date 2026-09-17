"""Stage 4: the isolation guarantees, written BEFORE the wiring that provides them.

They all pass now. They did not when they were written, and that ordering is the
point. The design goal of the whole multi-project feature is one sentence --

    a retrieval or report operation performed inside Project A must have no path
    through which it can accidentally query Project B's detection database

-- and an isolation guarantee that is not tested is a hope. This one fails
SILENTLY: the leak documented below produces a complete, well-formed, entirely
wrong report with nothing raised and nothing logged.

Each test was held as ``xfail(strict=True)`` until the stage named in its reason
string landed. Without ``strict`` an unexpected pass is reported as ``xpass``, the
suite stays green, and there is no signal at the moment the guarantee starts
holding; with it, an unexpected pass FAILS the suite, which is the notification
that the wiring is done. Stages 5 and 6 supplied that wiring and every marker came
off. What remains is a plain regression suite: these are the tests that go red if
the isolation is ever undone.

Nothing here imports a symbol that did not exist when it was written: a missing
import is a collection error, which would have turned the suite red instead of
xfailing. Everything is driven through interfaces that already existed -- CliRunner,
the FastAPI TestClient, and the repository -- so a missing feature failed at
RUNTIME.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import importlib.util
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import ttr.web.app as web_app
from ttr.agents.retrieval import RetrievalAgent
from ttr.agents.window import TimeWindow
from ttr.cli import app as cli_app
from ttr.projects import paths as ppaths
from ttr.projects.manifest import ProjectManifest
from ttr.projects.registry import Registry
from ttr.projects.service import context_for, create_project
from ttr.species.aliases import SpeciesAliasMap
from ttr.web.app import app as fastapi_app
from ttr.web.app import get_alias_map, get_llm, get_repo

from conftest import FakeLLM, mailbox_factory, make_persisted_event

UTC = dt.timezone.utc
runner = CliRunner()

#: A window both projects have data in, so "same species, same window" is a real
#: comparison rather than one project simply having nothing.
WINDOW_START = dt.date(2026, 7, 1)
WINDOW_END = dt.date(2026, 7, 31)

ALIASES = SpeciesAliasMap({
    "Columba palumbus": {"raw_tokens": ["ColumbaPalumbus"],
                         "common_names": ["wood pigeon", "woodpigeon"]},
    "Vulpes vulpes": {"raw_tokens": ["VulpesVulpes"], "common_names": ["red fox"]},
    "Meles meles": {"raw_tokens": ["MelesMeles"], "common_names": ["european badger"]},
})

#: The two datasets differ in every way a report would show: shared species with
#: DIFFERENT counts, an exclusive species each, and different date ranges. If B's
#: figures could be mistaken for A's, none of these tests would prove anything.
ALPHA = {"Columba palumbus": 12, "Vulpes vulpes": 4}
BETA = {"Columba palumbus": 3, "Meles meles": 7}
ALPHA_ONLY = "Vulpes vulpes"
BETA_ONLY = "Meles meles"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(r))
    return r


def _seed(ctx, mix: dict, *, day_offset: int) -> None:
    """Seed a project's database directly through its repository.

    Not through email fixtures: this stage is about isolation, not parsing, and
    synthetic rows keep it fast and deterministic.
    """
    repo = ctx.open_repo()
    try:
        n = 0
        for binomial, count in mix.items():
            for i in range(count):
                when = dt.datetime(2026, 7, 2 + day_offset, 9, 0, tzinfo=UTC) \
                    + dt.timedelta(days=i % 5)
                repo.upsert_event(make_persisted_event(
                    f"<{ctx.slug}-{n}@x>", canonical_binomial=binomial,
                    event_time=when, upstream_label=binomial.replace(" ", ""),
                    display_common_name=ALIASES.display_common_name(binomial),
                    # No agreement_flag and no BioCLIP top-k: these rows carry no
                    # cross-check verdict, so `recompute-crosscheck` has nothing
                    # to reverse. Seeding "agree" without the evidence behind it
                    # would make the command fail loudly -- correctly -- on a
                    # verdict reversal it was right to notice.
                    upstream_confidence=0.9, vlm_ok=True))
                n += 1
    finally:
        repo.close()


@pytest.fixture
def two_projects(root):
    """Project A and Project B, seeded, isolated, and deliberately dissimilar."""
    a_manifest, _ = create_project("Alpha Site", "alpha@gmail.com",
                                   "abcd efgh ijkl mnop", root=root,
                                   mailbox_factory=mailbox_factory())
    b_manifest, _ = create_project("Beta Site", "beta@gmail.com",
                                   "abcd efgh ijkl mnop", root=root,
                                   mailbox_factory=mailbox_factory())
    registry = Registry.load(root)
    a = context_for(registry.get(a_manifest.id), root)
    b = context_for(registry.get(b_manifest.id), root)
    _seed(a, ALPHA, day_offset=0)
    _seed(b, BETA, day_offset=10)
    return a, b


@pytest.fixture(autouse=True)
def _clean_web_state():
    """Clear FastAPI overrides and the report-body cache around every test.

    `_BODY_CACHE` is reached through getattr rather than imported, so that when
    Stage 6 renames or replaces it this file does not become a collection error.
    """
    fastapi_app.dependency_overrides.clear()
    cache = getattr(web_app, "_BODY_CACHE", None)
    if cache is not None:
        cache.clear()
    yield
    fastapi_app.dependency_overrides.clear()
    cache = getattr(web_app, "_BODY_CACHE", None)
    if cache is not None:
        cache.clear()


def _client_for(ctx):
    """A TestClient bound to one project's /p/{id}/ prefix.

    The prefix is what carries project identity into the cache key, the print URL
    and the logs — so the client that exercises those routes must carry it too.
    Only the alias map and the LLM are overridden; the repository comes from the
    real `get_project` -> `get_repo` chain, which is the thing under test.
    """
    from conftest import project_client

    fastapi_app.dependency_overrides[get_alias_map] = lambda: ALIASES
    fastapi_app.dependency_overrides[get_llm] = lambda: FakeLLM(
        "A deterministic narrative describing the period's alert events.")
    return project_client(fastapi_app, ctx)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _window() -> TimeWindow:
    return TimeWindow.from_dates(WINDOW_START, WINDOW_END)


def _report_body(ctx) -> str:
    from ttr.agents.report import ReportGeneratorAgent

    repo = ctx.open_repo()
    try:
        agent = ReportGeneratorAgent(RetrievalAgent(repo, ALIASES), FakeLLM(),
                                     site_name=ctx.site.name)
        return agent.generate_all_species(_window())
    finally:
        repo.close()


# =========================================================================== #
# Report scoping -- the headline requirement
# =========================================================================== #
def test_retrieval_returns_only_the_projects_own_rows(two_projects):
    """PASSES ALREADY. Stage 3 gave each project its own database, and the
    retrieval agent takes a repository rather than finding one, so a correctly
    scoped repository is a correctly scoped retrieval. Recorded here as the
    baseline the rest of this file builds on."""
    a, b = two_projects

    a_repo, b_repo = a.open_repo(), b.open_repo()
    try:
        a_rows = RetrievalAgent(a_repo, ALIASES).find("wood pigeon", _window())
        b_rows = RetrievalAgent(b_repo, ALIASES).find("wood pigeon", _window())

        assert len(a_rows) == ALPHA["Columba palumbus"] == 12
        assert len(b_rows) == BETA["Columba palumbus"] == 3

        a_ids = {r.source_message_id for r in a_rows}
        b_ids = {r.source_message_id for r in b_rows}
        assert a_ids.isdisjoint(b_ids)
        assert all("alpha-site" in i for i in a_ids)
        assert all("beta-site" in i for i in b_ids)

        # The exclusive species never crosses.
        assert RetrievalAgent(a_repo, ALIASES).find("european badger", _window()) == []
        assert RetrievalAgent(b_repo, ALIASES).find("red fox", _window()) == []
    finally:
        a_repo.close()
        b_repo.close()


def test_a_rendered_report_contains_only_its_own_projects_figures(two_projects):
    """PASSES ALREADY, at the agent level -- the leak this stage is really about
    is above the agent, in the web cache.

    Discriminated on the EXCLUSIVE species and on the event totals, not on the
    site name: `site_name` reaches the body only through the weather caption, so
    with no weather rows it is absent from both reports and would prove nothing.
    """
    a, b = two_projects
    a_body, b_body = _report_body(a), _report_body(b)

    assert ALPHA_ONLY in a_body and BETA_ONLY not in a_body
    assert BETA_ONLY in b_body and ALPHA_ONLY not in b_body

    # Totals differ (16 vs 10), so a swapped body could not pass unnoticed.
    assert str(sum(ALPHA.values())) in a_body
    assert str(sum(BETA.values())) in b_body


# =========================================================================== #
# The report-body cache -- the silent leak found in Phase 1
# =========================================================================== #
def test_the_screen_report_path_does_not_serve_a_cached_body(two_projects):
    """PASSES ALREADY — and the reason is worth recording, because Phase 1 got
    the mechanism slightly wrong.

    Phase 1 described the leak as "request the same species and window in B and
    the cache hits, returning A's report". It does not: `_render_report_body`
    reads the cache ONLY under `reuse_rendered`, which just the print path sets
    (web/app.py:204, :249). `/api/report` always regenerates and merely WRITES
    the cache.

    So the screen path is already safe, and the exposure is narrower than
    recorded but not smaller in consequence: it runs screen -> print, which is
    the path that produces a downloadable PDF. See the next test.
    """
    a, b = two_projects
    payload = {"species": web_app.ALL_SPECIES,
               "start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}

    a_body = _client_for(a).post("/api/report", json=payload).text
    b_body = _client_for(b).post("/api/report", json=payload).text

    assert ALPHA_ONLY in a_body and BETA_ONLY not in a_body
    assert BETA_ONLY in b_body and ALPHA_ONLY not in b_body


def test_the_report_body_cache_is_keyed_per_project(two_projects):
    """The structural requirement, independent of which paths read the cache.

    Two projects rendering the identical species/window must occupy two entries.
    Today they collide on one key and the second overwrites the first, which is
    what makes the print path serve the wrong project's body. Asserting on the
    KEY rather than on a symptom means this keeps holding however the read paths
    change.
    """
    a, b = two_projects
    cache = getattr(web_app, "_BODY_CACHE", None)
    assert cache is not None, "no report body cache to check"

    payload = {"species": web_app.ALL_SPECIES,
               "start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}
    _client_for(a).post("/api/report", json=payload)
    _client_for(b).post("/api/report", json=payload)

    assert len(cache) == 2, (
        f"two projects rendered the same window and produced {len(cache)} cache "
        f"entr(y/ies); the key carries no project identity")


def test_the_print_path_does_not_inherit_another_projects_cached_body(two_projects):
    """`/print` deliberately reuses a body rendered moments ago for the screen,
    so the wrong project's report is not merely displayed -- it is rendered to
    PDF, downloaded, and potentially filed as a record of the wrong site."""
    a, b = two_projects
    params = {"species": web_app.ALL_SPECIES,
              "start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}

    _client_for(a).post("/api/report", json=params)          # warms the cache
    printed = _client_for(b).get("/print", params=params).text

    assert BETA_ONLY in printed
    assert ALPHA_ONLY not in printed


def test_a_report_request_names_its_project_in_the_url(two_projects):
    """Project identity belongs in the URL, not in server state: it is what makes
    a bookmarked or shared report link still mean the same thing tomorrow, and
    what lets the cache key carry a project without anyone remembering to add it.
    """
    a, _ = two_projects
    client = _client_for(a)
    # "//" escapes the client's own prefixing, so this asserts the literal URL
    # shape rather than the fixture's convenience.
    response = client.post(
        f"//p/{a.id}/api/report",
        json={"species": "wood pigeon", "start": WINDOW_START.isoformat(),
              "end": WINDOW_END.isoformat()})
    assert response.status_code == 200

    # An unknown project is a 404, never a fallback to some other project's data.
    assert client.post("//p/does-not-exist/api/report",
                       json={"species": "wood pigeon",
                             "start": WINDOW_START.isoformat(),
                             "end": WINDOW_END.isoformat()}).status_code == 404


# =========================================================================== #
# CLI commands act only on the named project
# =========================================================================== #
#: Each command with the exit code it should give for project A. Two legitimately
#: refuse rather than succeed, and that is still a project-scoped outcome: the
#: point of the test is that B is untouched either way, not that everything runs.
#: The crop path imports numpy directly ([detect], or [enrich] via torch).
_needs_crop_stack = pytest.mark.skipif(
    importlib.util.find_spec("numpy") is None,
    reason="needs the [detect] or [enrich] extra")

DB_TOUCHING_COMMANDS = [
    pytest.param(["report", "--species", "wood pigeon", "--window", "30d"], 0,
                 id="report"),
    pytest.param(["backfill-capture-time", "--dry-run"], 0,
                 id="backfill-capture-time"),
    # Alpha has no coordinates, so this refuses (exit 2) -- per project now,
    # which is the behaviour being checked.
    pytest.param(["backfill-weather", "--dry-run"], 2, id="backfill-weather"),
    pytest.param(["recompute-crosscheck", "--dry-run", "--no-backfill-lineage"], 0,
                 id="recompute-crosscheck"),
    # These two need the crop stack. Without it the command REFUSES, which is
    # correct behaviour and has its own coverage - but it is a different code
    # path from the one this test is about. Skipping keeps each case asserting
    # one thing, rather than branching the expected exit code on the
    # environment; the isolation property itself is still covered here by the
    # four commands above, which need no optional extra.
    pytest.param(["recompute-crops", "--dry-run", "--limit", "1"], 0,
                 id="recompute-crops", marks=_needs_crop_stack),
    pytest.param(["recompute-crop-crosscheck", "--dry-run"], 0,
                 id="recompute-crop-crosscheck", marks=_needs_crop_stack),
    pytest.param(["store", "--from-fixtures"], 0, id="store"),
]


@pytest.mark.parametrize("command,expected_exit", DB_TOUCHING_COMMANDS)
def test_a_command_scoped_to_one_project_never_touches_the_other(
        two_projects, root, command, expected_exit):
    """Each DB-touching command, run against A, must leave B byte-unchanged.

    `--project` precedes the subcommand because it is a GLOBAL option declared
    once on the callback -- one implementation of the precedence rules rather
    than eleven.
    """
    a, b = two_projects
    before = _digest(b.db_path)

    result = runner.invoke(cli_app,
                           ["--project", "Alpha Site", *command],
                           env={ppaths.ROOT_ENV_VAR: str(root)})

    assert result.exit_code == expected_exit, result.output
    assert 'using project "Alpha Site"' in result.output
    assert "Beta Site" not in result.output
    assert _digest(b.db_path) == before


def test_a_command_states_which_project_it_operated_on(two_projects, root):
    """A wrong-project run must be VISIBLE, not merely recorded. The provenance
    suffix is what turns 'I forgot which context I was in' into a fact on screen.
    """
    result = runner.invoke(
        cli_app, ["--project", "Alpha Site",
                  "report", "--species", "wood pigeon", "--window", "30d"],
        env={ppaths.ROOT_ENV_VAR: str(root)})

    assert result.exit_code == 0
    assert "Alpha Site" in result.output
    assert "[from --project]" in result.output


# =========================================================================== #
# Ingest writes to one project only
# =========================================================================== #
def test_ingest_is_scoped_to_one_project(two_projects, root):
    """`ttr run` resolves and announces ONE project, and cannot touch the other.

    The poll itself fails here -- there is no reachable mailbox in a test -- and
    that is fine: what is being checked is that the command bound itself to
    project A and left B's database and seen-store alone. That ingest WRITES to
    only its own project is proven at the pipeline level, where it can be driven
    end to end, in test_pipeline_project.py.
    """
    a, b = two_projects
    db_before = _digest(b.db_path)
    seen_before = b.seen_store_path.exists()

    result = runner.invoke(cli_app, ["--project", "Alpha Site", "run", "--once"],
                           env={ppaths.ROOT_ENV_VAR: str(root)})

    assert 'using project "Alpha Site"' in result.output
    assert "[from --project]" in result.output
    assert "Beta Site" not in result.output
    assert _digest(b.db_path) == db_before
    assert b.seen_store_path.exists() == seen_before


# =========================================================================== #
# A mis-pointed database is refused
# =========================================================================== #
def test_a_manifest_pointing_at_another_projects_database_is_refused(two_projects):
    """The failure this catches writes plausible-looking rows into the wrong
    deployment's record, undetectably and irreversibly. A manifest edited by
    hand, a folder copied and half-renamed, a symlink -- all land here.
    """
    a, b = two_projects
    manifest = ProjectManifest.read(a.dir)
    manifest.storage.database = str(b.db_path)
    manifest.storage.database_external = True
    manifest.write(a.dir)

    mispointed = context_for(Registry.load(a.dir.parent.parent).get(a.id),
                             a.dir.parent.parent)
    with pytest.raises(Exception):
        repo = mispointed.open_repo()
        repo.close()


def test_a_mispointed_project_does_not_report_the_other_projects_rows(
        two_projects, root):
    """Expressed through a command that exists TODAY and reads a project's
    database: `project delete --dry-run` prints the row count. Pointed at B's
    database, project A reports B's 10 events as its own.
    """
    a, b = two_projects
    manifest = ProjectManifest.read(a.dir)
    manifest.storage.database = str(b.db_path)
    manifest.storage.database_external = True
    manifest.write(a.dir)

    result = runner.invoke(cli_app, ["project", "delete", "Alpha Site", "--dry-run"],
                           env={ppaths.ROOT_ENV_VAR: str(root)})

    beta_total = sum(BETA.values())
    assert f"{beta_total} detection events" not in result.output


# =========================================================================== #
# Nothing outside config.py reads the moved settings
# =========================================================================== #
#: The fields that become project metadata. `config.py` declares them, so it is
#: excluded; everything else must stop reading them before they can be removed.
MOVED_SETTINGS = (
    "db_path", "image_store_dir", "site_name",
    "weather_latitude", "weather_longitude",
    "imap_host", "imap_user", "imap_password", "imap_folder",
    "imap_use_ssl", "imap_lookback_days",
)
#: Identifiers a Settings object is bound to across the codebase.
_SETTINGS_NAMES = r"(?:settings|s|app_settings|_settings|get_settings\(\))"
#: TWO shapes, because catching only the first is how a dead read survived.
#:
#: An ATTRIBUTE read — `settings.db_path` — is what this scan was written for.
#: A METADATA lookup — `Settings.model_fields["imap_folder"]`, or `.get(...)` —
#: names the same moved field and was invisible to it. `projects/service.py` did
#: exactly that inside a bare `except`, so it raised `KeyError('imap_folder')` on
#: every `ttr project create` for months while this test stayed green and the
#: field looked unreferenced. A field is "still read" either way.
#: Mapping attributes that expose a model's declared fields.
_FIELD_MAPPINGS = {"model_fields", "__fields__"}
_FIELDS = r"(?:model_fields|__fields__)"
_MOVED_NAMES = "|".join(MOVED_SETTINGS)
_MOVED_RE = re.compile(
    rf"\b{_SETTINGS_NAMES}\.({_MOVED_NAMES})\b"
    rf"|{_FIELDS}\s*(?:\[\s*|\.get\s*\(\s*)[\"']({_MOVED_NAMES})[\"']")

SRC = Path(__file__).resolve().parents[1] / "src" / "ttr"


def _field_metadata_reads(tree: "ast.Module") -> list[tuple[int, str]]:
    """(line, description) for `Settings.model_fields["<moved>"]`, alias included.

    The receiver is what separates a violation from ordinary code, so this cannot
    be a line regex. `mailbox.get("imap_folder", ...)` in `manifest.py` reads the
    PROJECT'S OWN TOML, where that key genuinely lives, and is correct. What is
    not correct is reading the same name out of `Settings`' metadata after the
    field has moved off `Settings`.

    The real defect bound the mapping to a local first —

        fields = Settings.model_fields
        m.imap_folder = fields["imap_folder"].default

    — so the subscript and the `model_fields` attribute were never on one line.
    Aliases are therefore resolved before the subscripts are examined.
    """
    aliases = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute)
                and node.value.attr in _FIELD_MAPPINGS):
            aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))

    def _is_field_mapping(value: "ast.AST") -> bool:
        if isinstance(value, ast.Attribute) and value.attr in _FIELD_MAPPINGS:
            return True
        return isinstance(value, ast.Name) and value.id in aliases

    found = []
    for node in ast.walk(tree):
        key = receiver = None
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            key, receiver = node.slice.value, node.value
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "get" and node.args
              and isinstance(node.args[0], ast.Constant)):
            key, receiver = node.args[0].value, node.func.value
        if key in MOVED_SETTINGS and _is_field_mapping(receiver):
            found.append((node.lineno, f'Settings field metadata ["{key}"]'))
    return found


def _moved_setting_reads() -> list[str]:
    hits = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "config.py":
            continue                       # declares them; that is its job
        rel = path.relative_to(SRC.parent.parent)
        source = path.read_text(encoding="utf-8")
        for n, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue                   # a comment naming a field is not a read
            for match in _MOVED_RE.finditer(line):
                hits.append(f"{rel}:{n}  {match.group(0)}")
        for n, what in _field_metadata_reads(ast.parse(source, filename=str(path))):
            hits.append(f"{rel}:{n}  {what}")
    return hits


def test_no_module_outside_config_reads_a_moved_setting():
    """The test that makes the later field removal a verified no-op.

    Until this passes, deleting those fields from `Settings` is a leap. When it
    passes, it is a deletion nothing can be reading.
    """
    hits = _moved_setting_reads()
    assert hits == [], (
        f"{len(hits)} module(s) still read per-deployment values off Settings:\n"
        + "\n".join(f"  {h}" for h in hits))


def test_the_moved_setting_scan_would_still_catch_a_violation(tmp_path):
    """A guard on the guard, now that the real count is zero.

    Until Stage 6 this asserted the scanner still saw the reads we knew were
    there. It cannot any more — there are none — so a scanner that had silently
    stopped matching would look identical to success, and the field removal it
    gates would proceed on a false all-clear.

    So it is checked against a synthetic violation instead: the regex must still
    recognise the shapes it exists to find.
    """
    violations = [
        "repo = DetectionRepository(settings.db_path)",
        "    store = settings.image_store_dir",
        "name = get_settings().site_name",
        "        lat = s.weather_latitude",
        "mailbox.login(app_settings.imap_user, app_settings.imap_password)",
        "folder = _settings.imap_folder",
    ]
    for line in violations:
        assert _MOVED_RE.search(line), f"the scanner no longer matches: {line!r}"

    # ...and does not fire on things that merely look similar.
    for benign in [
        "storage.db_path.parent",                  # the duck-typed seen-store arg
        "ctx.site.latitude",                       # the project's own value
        "manifest.mailbox.imap_host",              # read off a manifest
        "self._settings_view()",
    ]:
        assert not _MOVED_RE.search(benign), f"false positive on: {benign!r}"


def test_the_scan_catches_a_moved_field_read_as_METADATA():
    """The shape that got past it, replayed exactly.

    `projects/service.py` bound `Settings.model_fields` to a local and then
    subscripted it by name. No line contained both halves, so the line regex saw
    nothing while the code raised `KeyError('imap_folder')` on every project
    creation. The field looked unreferenced and the scan stayed green.

    Asserted on the real deleted source rather than a paraphrase.
    """
    deleted = (
        "from ..config import Settings\n"
        "def _mailbox_defaults(m):\n"
        "    fields = Settings.model_fields\n"
        '    m.imap_folder = fields["imap_folder"].default\n'
        '    m.poll_seconds = fields["poll_seconds"].default\n')

    found = _field_metadata_reads(ast.parse(deleted))
    assert [what for _line, what in found] == [
        'Settings field metadata ["imap_folder"]'], found

    direct = 'x = Settings.model_fields["site_name"].default'
    assert _field_metadata_reads(ast.parse(direct)), "direct form missed"


def test_the_metadata_scan_does_not_flag_a_manifest_read():
    """`manifest.py` reads the PROJECT'S OWN toml, where the key genuinely lives.

    The receiver is the whole distinction, which is why this half of the scan is
    AST rather than a regex: `mailbox.get("imap_folder", ...)` is correct code
    and must never be reported.
    """
    legitimate = (
        'def _read(mailbox, defaults):\n'
        '    return mailbox.get("imap_folder", defaults.imap_folder)\n')

    assert _field_metadata_reads(ast.parse(legitimate)) == []


# =========================================================================== #
# Ambiguity and absence are errors, not defaults
# =========================================================================== #
def test_two_projects_and_no_selection_is_an_error_that_lists_them(
        two_projects, root):
    """Never silently pick one. Picking is how a bulk recompute rewrites 800 rows
    in the wrong deployment.

    The active-project pointer is cleared first: creating the first project makes
    it active (Stage 1), so rule 3 would otherwise answer before rule 5 is ever
    reached. This is the genuinely unselected case.
    """
    registry = Registry.load(root)
    registry.active_project = None
    registry.save()

    result = runner.invoke(cli_app, ["report", "--species", "wood pigeon",
                                     "--window", "30d"],
                           env={ppaths.ROOT_ENV_VAR: str(root)})

    assert result.exit_code != 0
    assert "Alpha Site" in result.output and "Beta Site" in result.output
    assert "--project" in result.output


def test_a_bulk_write_refuses_a_defaulted_project(two_projects, root):
    """Reads may default; bulk writes may not. With two projects and only the
    active pointer to go on, a recompute that would rewrite every row stops."""
    result = runner.invoke(cli_app, ["recompute-crosscheck", "--no-backfill-lineage"],
                           env={ppaths.ROOT_ENV_VAR: str(root)})

    assert result.exit_code == 2
    assert "will not use a defaulted project" in result.output
    assert "--project" in result.output


def test_a_bulk_write_accepts_an_explicit_project(two_projects, root):
    result = runner.invoke(
        cli_app, ["--project", "Alpha Site", "recompute-crosscheck",
                  "--no-backfill-lineage", "--dry-run"],
        env={ppaths.ROOT_ENV_VAR: str(root)})
    assert result.exit_code == 0, result.output


def test_exactly_one_project_may_be_defaulted_to(root):
    """The rule that keeps single-project users from ever typing --project."""
    manifest, _ = create_project("Only One", "only@gmail.com",
                                 "abcd efgh ijkl mnop", root=root,
                                 mailbox_factory=mailbox_factory())
    ctx = context_for(Registry.load(root).get(manifest.id), root)
    _seed(ctx, {"Columba palumbus": 2}, day_offset=0)

    result = runner.invoke(cli_app, ["report", "--species", "wood pigeon",
                                     "--window", "30d"],
                           env={ppaths.ROOT_ENV_VAR: str(root)})

    assert result.exit_code == 0, result.output
    assert "Only One" in result.output


def test_an_archived_project_does_not_make_the_remaining_one_ambiguous(
        two_projects, root):
    """Most of the point of archiving: it takes a project out of selection
    without deleting it."""
    from ttr.projects.service import set_archived

    set_archived("Beta Site", True, root=root)

    result = runner.invoke(cli_app, ["report", "--species", "wood pigeon",
                                     "--window", "30d"],
                           env={ppaths.ROOT_ENV_VAR: str(root)})

    assert result.exit_code == 0, result.output
    assert "Alpha Site" in result.output
