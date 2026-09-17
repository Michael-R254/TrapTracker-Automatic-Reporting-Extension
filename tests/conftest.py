"""Shared pytest fixtures.

Grows across stages (FakeLLM, FakeEnricher, temp-db, .eml fixtures). At Stage 0
it provides a clean-environment helper so config tests are not polluted by any
IMAP_* variables that happen to be set in the developer's shell.
"""

from __future__ import annotations

import atexit
import email
import os
from contextlib import ExitStack
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path

import pytest

from ttr.species.aliases import bundled_example_alias_path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
EMAILS_DIR = FIXTURES_DIR / "emails"

# The bundled example alias table, resolved as PACKAGE DATA rather than by a
# hard-coded "config/species_aliases.example.yaml" repo path. Tests that hard-code
# the repo path pass in a checkout and silently stop covering the installed
# package, which is exactly where the resolution bug lived. The ExitStack keeps
# the resource extracted for the whole session on layouts where package data is
# not a real file.
_RESOURCES = ExitStack()
atexit.register(_RESOURCES.close)
EXAMPLE_ALIAS_PATH = str(_RESOURCES.enter_context(bundled_example_alias_path()))


def load_eml(name: str) -> EmailMessage:
    """Load a fixture ``.eml`` as an EmailMessage under policy.default.

    ``name`` is relative to ``tests/fixtures/emails`` (e.g. ``both_attachments.eml``
    or ``golden/real_alert.eml``).
    """
    data = (EMAILS_DIR / name).read_bytes()
    return email.message_from_bytes(data, policy=default_policy)


@pytest.fixture
def emails_dir() -> Path:
    return EMAILS_DIR


# --------------------------------------------------------------------------- #
# Stage 3 fixtures/fakes: alias map + enrichers that need no models.
# --------------------------------------------------------------------------- #
@pytest.fixture
def alias_map():
    """A small alias map exercising every shape: single-token species, a
    many-to-one (life-stage) key, an ambiguous common name ("deer"), a
    non-comparable species, and a reserved non-binomial class."""
    from ttr.species.aliases import SpeciesAliasMap

    return SpeciesAliasMap({
        "Vulpes vulpes": {"raw_tokens": ["VulpesVulpes"], "common_names": ["red fox", "fox"]},
        "Meles meles": {"raw_tokens": ["MelesMeles"], "common_names": ["european badger", "badger"]},
        "Ursus arctos": {"raw_tokens": ["UrsusArctos"], "common_names": ["brown bear"]},
        "Numenius arquata": {
            "raw_tokens": ["NumeniusArquata", "NumeniusArquataChick"],
            "common_names": ["eurasian curlew", "curlew"],
        },
        "Capreolus capreolus": {"raw_tokens": ["CapreolusCapreolus"], "common_names": ["roe deer", "deer"]},
        "Dama dama": {"raw_tokens": ["DamaDama"], "common_names": ["fallow deer", "deer"]},
        "Bos taurus": {"raw_tokens": ["BosTaurus"], "common_names": ["cattle", "cow"]},
        "nonbio:Person": {"raw_tokens": ["Person"], "common_names": ["person"], "bioclip_comparable": False},
    })


@pytest.fixture
def target_taxonomy():
    """Higher ranks for the small ``alias_map`` fixture's species.

    Deliberately a fixture rather than the shipped table: these tests exercise the
    DISTANCE logic, and pinning the lineages here keeps them independent of edits
    to the real class list. Shapes covered: same genus (Vulpes), same family
    (Canidae vs Vulpes), same order (Carnivora), same class (Mammalia vs Aves) and
    a non-animal.
    """
    from ttr.species.taxonomy import TargetTaxonomy

    return TargetTaxonomy({
        "Vulpes vulpes": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                          "order": "Carnivora", "family": "Canidae", "genus": "Vulpes"},
        "Meles meles": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                        "order": "Carnivora", "family": "Mustelidae", "genus": "Meles"},
        "Ursus arctos": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                         "order": "Carnivora", "family": "Ursidae", "genus": "Ursus"},
        "Numenius arquata": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Aves",
                             "order": "Charadriiformes", "family": "Scolopacidae",
                             "genus": "Numenius"},
        "Capreolus capreolus": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                                "order": "Artiodactyla", "family": "Cervidae",
                                "genus": "Capreolus"},
        "Dama dama": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                      "order": "Artiodactyla", "family": "Cervidae", "genus": "Dama"},
        "Bos taurus": {"kingdom": "Animalia", "phylum": "Chordata", "class": "Mammalia",
                       "order": "Artiodactyla", "family": "Bovidae", "genus": "Bos"},
    })


class FakeTaxonomicEnricher:
    """A TaxonomicEnricher stand-in — returns a preset read, no model needed."""

    def __init__(self, read):
        self._read = read
        self.calls = 0

    def classify(self, original_image: bytes):
        self.calls += 1
        return self._read


class FakeDescriptionEnricher:
    def __init__(self, read):
        self._read = read
        self.calls = 0

    def describe(self, boxed_image: bytes):
        self.calls += 1
        return self._read


# --------------------------------------------------------------------------- #
# Stage 4 fixtures/fakes: repo, a PersistedEvent builder, and a FakeLLM.
# --------------------------------------------------------------------------- #
@pytest.fixture
def repo(tmp_path):
    from ttr.storage.repository import DetectionRepository

    r = DetectionRepository(tmp_path / "test.db")
    yield r
    r.close()


def make_persisted_event(message_id: str, *, canonical_binomial, event_time, **overrides):
    """Build a PersistedEvent carrying the Decision-7 provenance note by default."""
    import datetime as _dt

    from ttr.storage.repository import PersistedEvent

    base = dict(
        source_type="email",
        source_message_id=message_id,
        ingested_at_utc=_dt.datetime(2026, 7, 15, 12, 0, tzinfo=_dt.timezone.utc),
        canonical_binomial=canonical_binomial,
        canonical_is_binomial=not str(canonical_binomial).startswith("nonbio:"),
        event_time_utc=event_time,
        images_present="both",
        field_provenance={"ingestion_validation": {"present": True, "source": "derived"}},
    )
    base.update(overrides)
    return PersistedEvent(**base)


class FakeLLM:
    """Deterministic LLMClient stand-in — records calls, returns fixed text."""

    def __init__(self, text: str = "A deterministic narrative summary of the period's "
                                "alert events."):
        self._text = text
        self.calls: list[dict] = []

    def generate(self, prompt: str, *, system=None, images=None) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        return self._text


class ScriptedLLM:
    """LLMClient returning a preset sequence of replies, then repeating the last.

    Lets a test drive the narrative vocabulary gate: hand it a non-compliant reply
    followed by a compliant one and assert the report regenerates rather than
    publishing the first.
    """

    def __init__(self, *replies: str):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def generate(self, prompt: str, *, system=None, images=None) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        i = min(len(self.calls) - 1, len(self._replies) - 1)
        return self._replies[i]


class FailingLLM:
    """LLMClient that always raises — to prove the report degrades gracefully."""

    def generate(self, prompt: str, *, system=None, images=None) -> str:
        raise RuntimeError("llm down")


@pytest.fixture
def fake_llm():
    return FakeLLM()


def _settings_env_keys():
    """Every env key configuration can be affected by, derived from the model.

    Derived rather than restated: the hand-maintained list this replaced had
    drifted to 13 of 23 keys, so a developer with (say) WEATHER_LATITUDE exported
    got different results from CI — the exact contamination the fixture existed
    to prevent.

    REMOVED_ENV_KEYS is included even though `Settings` no longer has those
    fields. They are not read any more, but they DO trigger the deprecation
    notice, so a developer with a real IMAP_HOST exported would otherwise get
    that notice mixed into unrelated tests' output.
    """
    from ttr.config import REMOVED_ENV_KEYS, Settings

    return tuple(name.upper() for name in Settings.model_fields) + tuple(REMOVED_ENV_KEYS)


@pytest.fixture(autouse=True)
def _hermetic_settings(monkeypatch):
    """Configuration must not depend on the machine the suite runs on.

    Two faults this closes, both found by checking out the release branch clean
    and running its own tests:

    * 16 tests in ``test_pdf.py`` passed only because a gitignored ``.env``
      happened to sit in the working directory. On a fresh clone — a new
      contributor, or CI — they failed with a Settings ``ValidationError``.
    * Where a ``.env`` did exist, the developer's REAL values (mailbox host, site
      coordinates) became test inputs silently.

    So the ``.env`` file is disconnected and every affected key is cleared. No
    fake IMAP values are set any more: `Settings` has no required fields left,
    because the mailbox belongs to a project. This is autouse and therefore runs
    FIRST, so any test that sets its own values still wins.
    """
    import ttr.config as config
    from ttr.config import Settings, get_settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key in _settings_env_keys():
        monkeypatch.delenv(key, raising=False)

    # The deprecation notice fires once per PROCESS, so a test that triggers it
    # would otherwise silence it for every test after.
    monkeypatch.setattr(config, "_NOTICE_EMITTED", False, raising=False)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Stage 2: the real OS credential store is off limits to the test suite.
# --------------------------------------------------------------------------- #
class InMemoryKeyring:
    """A keyring backend that lives and dies with the test session.

    Installed for EVERY test, not just credential ones, so no test can reach the
    developer's Windows Credential Manager / Keychain / Secret Service even by
    accident. `test_credentials.py` additionally asserts that this is the backend
    actually in force.
    """

    priority = 1000                      # far above any real backend

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.set_calls = 0

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.set_calls += 1
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        import keyring.errors

        try:
            del self.store[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError("not found")


@pytest.fixture(autouse=True)
def _isolated_keyring(monkeypatch):
    """Point `keyring` at an in-memory backend for the whole test session."""
    import keyring

    backend = InMemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    yield backend


@pytest.fixture
def keyring_backend(_isolated_keyring):
    """The in-memory backend, for tests that inspect what was stored."""
    return _isolated_keyring


@pytest.fixture(autouse=True)
def _no_credential_env(monkeypatch):
    """Strip credential env vars so a developer's exported password cannot leak
    into a test — the same hermetic principle as `_hermetic_settings`."""
    for key in list(os.environ):
        if key.startswith("TTR_IMAP_PASSWORD"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_example_project(monkeypatch):
    """`ttr serve` builds the example project into the projects root on first
    start. The serve tests run with no TTR_PROJECTS_ROOT, which is the user's real
    data directory, so the step is off unless a test turns it back on
    (`test_example_project.py`)."""
    from ttr.projects.example import SKIP_ENV_VAR

    monkeypatch.setenv(SKIP_ENV_VAR, "1")


#: The repository's own data directory. Nothing in the suite may open a database
#: in here.
_REPO_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(autouse=True)
def _never_touch_the_repo_data_dir(monkeypatch):
    """Fail loudly if a test opens a database under the repo's own ``data/``.

    This is not hypothetical. During Stage 5 a partially-rewired command read
    `Settings.db_path`, whose default is the CWD-relative
    ``data/traptracker_report.db``; run from the repository root, that opened the
    real artefact and migrated its schema. Nothing was lost — it holds zero rows —
    but the corpora sit in the same directory, and a test suite that can reach
    them at all is one mistake away from rewriting 787 events of dissertation
    evidence.

    So the reach is removed rather than the mistake being remembered.
    """
    import sqlite3

    real_connect = sqlite3.connect

    def guarded(database, *args, **kwargs):
        spec = str(database)
        # READ-ONLY opens are allowed: `test_crosscheck_corpus` verifies the real
        # stored corpus, which is a legitimate and valuable thing for it to do.
        # What must never happen is a WRITABLE open — that is what migrates a
        # schema, or worse.
        if "mode=ro" in spec:
            return real_connect(database, *args, **kwargs)
        try:
            target = Path(spec.split("?", 1)[0].replace("file:", ""))
            resolved = (Path.cwd() / target).resolve()
        except Exception:
            return real_connect(database, *args, **kwargs)   # ":memory:" etc.
        if _REPO_DATA_DIR in resolved.parents:
            raise AssertionError(
                f"a test tried to open {resolved} for WRITING, inside the "
                f"repository's own data/ directory. Point it at tmp_path, or "
                f"open it with '?mode=ro' if you only need to read — the real "
                f"corpora live there.")
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded)


@pytest.fixture(autouse=True)
def _reset_log_handler():
    """Let each CliRunner invocation install its own log handler.

    `configure_logging` is idempotent by design (`logging.py:60-62`), so the
    FIRST CliRunner to run binds the root handler to ITS captured stderr. Every
    later invocation then writes to a stream that runner has since closed, which
    surfaces as "ValueError: I/O operation on closed file" noise inside unrelated
    assertions. Clearing the flag per test keeps the production behaviour intact
    and the test output readable.
    """
    import ttr.logging as ttr_logging

    ttr_logging._CONFIGURED = False
    yield
    ttr_logging._CONFIGURED = False


def make_web_project(tmp_path, monkeypatch, name="Web Test Site"):
    """A real project for the web suites to be scoped to.

    The web routes now live under ``/p/{project_id}/`` and depend on
    ``get_project``, so a test client needs a project that actually resolves.
    Returns the ProjectContext.
    """
    from ttr.projects import paths as ppaths
    from ttr.projects.registry import Registry
    from ttr.projects.service import context_for, create_project

    root = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    manifest, _ = create_project(name, "web@gmail.com", root=root,
                                 skip_connection_test=True)
    return context_for(Registry.load(root).get(manifest.id), root)


#: Fixed token for the suite. Never minted, never printed, never a real session.
_TEST_UI_TOKEN = "test-ui-token-not-a-real-session"


def project_client(app, ctx):
    """A TestClient bound to one project's URL prefix.

    Every project-scoped route lives under ``/p/{id}/``, and these suites are
    about those routes, so the prefix is applied by default and the existing call
    sites read unchanged. A path beginning ``//`` escapes it, for the handful of
    app-level routes (the project index at ``/``).
    """
    from fastapi.testclient import TestClient

    from ttr.web import auth

    # The UI is behind a session token (`web/auth.py`). These suites are about the
    # routes, not the gate, so the client carries a valid cookie and the gate has
    # its own tests. Configured here rather than per-test so a NEW route cannot be
    # added with its auth accidentally untested - it simply works, and the
    # dedicated tests are what prove the gate is still shut.
    auth.set_ui_token(_TEST_UI_TOKEN)

    prefix = f"/p/{ctx.id}"

    class _ProjectClient(TestClient):
        project_prefix = prefix

        def _u(self, url):
            if isinstance(url, str) and url.startswith("//"):
                return url[1:]                      # escape hatch: app-level route
            if isinstance(url, str) and url.startswith("/"):
                return prefix + url
            return url

        def get(self, url, *a, **kw):
            return super().get(self._u(url), *a, **kw)

        def post(self, url, *a, **kw):
            return super().post(self._u(url), *a, **kw)

    client = _ProjectClient(app)
    client.cookies.set(auth.SESSION_COOKIE, _TEST_UI_TOKEN)
    return client


def mailbox_factory(*, messages: int = 7, login_error: BaseException = None,
                    status_error: BaseException = None, record: list = None):
    """A stand-in for the imap_tools mailbox class.

    The suite must never make a network call, so every connection test is driven
    through this. `record` collects the arguments login() was called with, which
    is how the whitespace-stripping and credential-source tests check what
    actually reached the server without ever printing it.
    """
    class _Folder:
        def status(self, folder=None, options=None):
            if status_error is not None:
                raise status_error
            return {"MESSAGES": messages, "RECENT": 0, "UNSEEN": 0}

    class _FakeMailBox:
        def __init__(self, host):
            self.host = host
            self.folder = _Folder()

        def login(self, user, password, initial_folder=None):
            if record is not None:
                record.append({"host": self.host, "user": user,
                               "password": password, "folder": initial_folder})
            if login_error is not None:
                raise login_error
            return self

        def fetch(self, *args, **kw):
            """An empty mailbox by default.

            EmailFetcher's two-pass logic is exercised by its own tests against
            real .eml fixtures; here the point is only which credentials and
            mailbox reached the server, so returning nothing keeps these tests
            about that.
            """
            return iter(())

        def logout(self):
            return None

    def _factory(use_ssl):
        return _FakeMailBox

    return _factory


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Remove all Settings env keys and chdir to an empty tmp dir.

    Runs after :func:`_hermetic_settings`, so it strips that fixture's dummy IMAP
    values too — leaving the test's own environment as the only source of
    configuration, including for tests that assert a MISSING secret fails loudly.
    """
    for key in _settings_env_keys():
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    from ttr.config import get_settings

    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
