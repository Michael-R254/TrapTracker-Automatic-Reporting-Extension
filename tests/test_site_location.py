"""F5 verification: the camera location is a project-configuration value, never
inferred from an alert or an image, and the report discloses that origin.

The requirement has two halves and both are checked here:

(a) the location value ORIGINATES IN CONFIGURATION — it reaches the weather client
    from ``Settings``, and no alert-derived field can supply one, because neither
    event model carries a coordinate at all;
(b) the report PROSE DISCLOSES that origin — §1 states the site is described from
    the known deployment rather than asserted from the data.

(c) the deployment PLACE NAME follows configuration too. This was F37 — the place
    name was a literal at ``report.py:294`` while only the coordinates were
    configurable, so the two could disagree and a report for one site would name
    another. Held as a strict xfail while deferred; FIXED for public release,
    where shipping one operator's site name to every user is a privacy fault and
    not merely a generality one. The test below is now a live assertion.
"""

from __future__ import annotations

import inspect
import re
from datetime import date, datetime, timezone

import pytest

from ttr.agents.report import ReportGeneratorAgent
from ttr.agents.retrieval import RetrievalAgent
from ttr.agents.window import TimeWindow
from ttr.sources.base import DetectionEvent
from ttr.sources.email_parser import parse_alert_email
from ttr.storage.repository import PersistedEvent

from conftest import FakeLLM, load_eml, make_persisted_event

UTC = timezone.utc

# Field-name fragments that would indicate a location riding in on the data path.
_LOCATION_FIELD_HINTS = ("latitude", "longitude", "coord", "location", "grid",
                         "easting", "northing", "gps", "geo")


def _agent(repo, alias_map, *, site_name=None):
    return ReportGeneratorAgent(
        RetrievalAgent(repo, alias_map),
        FakeLLM("Detections held steady across these alert events."),
        weather_small_sample_threshold=20,
        site_name=site_name)


def _seed(repo):
    """A small dated multi-species window so every report section has content."""
    for mid, day in [("<f1@x>", 8), ("<f2@x>", 9), ("<f3@x>", 10)]:
        repo.upsert_event(make_persisted_event(
            mid, canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, day, 8, 30, tzinfo=UTC),
            display_common_name="red fox", upstream_confidence=0.92,
            bioclip_ok=True, vlm_ok=True, agreement_flag="agree"))
    repo.upsert_event(make_persisted_event(
        "<b1@x>", canonical_binomial="Meles meles",
        event_time=datetime(2026, 7, 9, 22, 0, tzinfo=UTC),
        display_common_name="european badger", bioclip_ok=True, vlm_ok=True,
        agreement_flag="agree"))


_WINDOW = TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 14))


# --------------------------------------------------------------------------- #
# (a) The value originates in configuration.
# --------------------------------------------------------------------------- #
def test_location_is_project_configuration_with_no_shipped_default(tmp_path, monkeypatch):
    """Site identity belongs to a PROJECT, so a different deployment is a
    different project — not a config edit and certainly not a code edit.

    Nothing is defaulted anywhere: a shipped coordinate would publish one
    operator's site to every user AND silently match detections to weather
    somewhere the camera is not. The property is unchanged by the move; only its
    home is. It is asserted here at the project level, and `Settings` is asserted
    not to have taken a copy."""
    from ttr.config import Settings
    from ttr.projects import paths as ppaths
    from ttr.projects.manifest import ProjectManifest
    from ttr.projects.service import create_project, set_site

    # `Settings` holds no location at all any more.
    for field in ("site_name", "weather_latitude", "weather_longitude"):
        assert field not in Settings.model_fields

    root = tmp_path / "ttr-root"
    monkeypatch.setenv(ppaths.ROOT_ENV_VAR, str(root))
    manifest, project_dir = create_project("Sited", "s@gmail.com", root=root,
                                           skip_connection_test=True)

    # A new project has NO coordinates. backfill-weather refuses without them.
    assert manifest.site.latitude is None
    assert manifest.site.longitude is None

    # And they are genuinely configurable, per project.
    set_site("Sited", name="Example Garden, Exampleton",
             latitude=51.5072, longitude=-0.1276, root=root)
    moved = ProjectManifest.read(project_dir)
    assert moved.site.name == "Example Garden, Exampleton"
    assert moved.site.latitude == pytest.approx(51.5072)
    assert moved.site.longitude == pytest.approx(-0.1276)


def test_no_coordinate_literal_is_hardcoded_anywhere_in_the_package():
    """The privacy half of F37: no module may carry a deployment's coordinates as
    a literal, however well-intentioned the comment beside it."""
    import pathlib

    import ttr

    root = pathlib.Path(ttr.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # A decimal-degree literal on a line that is ABOUT a coordinate.
            # Scoped this way so ordinary floats (timeouts, thresholds, the
            # 23:59:59.999999 in window.py) cannot false-positive, while a real
            # inlined site still has nowhere to hide.
            if (re.search(r"latitude|longitude|\blat\b|\blon\b|coord", line, re.I)
                    and re.search(r"-?\d{1,3}\.\d{3,}", line)):
                offenders.append(f"{path.name}:{n} coordinate literal")
    assert offenders == []


def test_weather_caption_interpolates_the_configured_site_not_a_literal():
    """The place-name half, asserted STRUCTURALLY rather than by naming a place.

    Checking for a specific forbidden place name would mean writing that place
    name into a public test file — reintroducing, as a string literal in the
    guard, exactly the disclosure the guard exists to prevent. So this reads the
    source of the caption instead: it must interpolate the configured value, and
    must not carry a quoted "Somewhere, Someplace"-shaped literal of its own.
    """
    source = inspect.getsource(ReportGeneratorAgent._weather_section)
    caption = source[source.index("camera's location"):][:200]

    assert "self._site_name" in caption, "caption must come from configuration"
    # A quoted two-word-capitalised place, optionally with a comma — the shape a
    # hardcoded deployment name takes.
    assert re.search(r"\(\s*[A-Z][a-z]+(?: [A-Z][a-z]+)*,\s*[A-Z][a-z]+\s*\)", caption) is None


def test_weather_client_receives_the_configured_coordinates_not_literals():
    """The production construction site must pass the configured values through.

    Asserted structurally, in the same spirit as the enricher-signature check:
    inlining a coordinate at the call site is exactly the violation F5 names, and
    a signature test cannot see it.
    """
    import ttr.cli

    source = inspect.getsource(ttr.cli)
    construction = source[source.index("WeatherClient("):]
    construction = construction[:construction.index(")")]

    # The coordinates now come from the PROJECT's site, not from Settings —
    # each deployment carries its own camera location, and one project having
    # coordinates says nothing about another. The property under test is
    # unchanged: they are read from configuration, never inlined.
    assert "ctx.site.latitude" in construction
    assert "ctx.site.longitude" in construction
    # No decimal-degree literal is inlined at the call site.
    assert re.search(r"-?\d+\.\d{3,}", construction) is None


def test_no_event_model_carries_a_location_field():
    """Neither the ingestion nor the storage model has anywhere to put a location,
    so a coordinate cannot arrive from an alert even in principle."""
    for model in (DetectionEvent, PersistedEvent):
        assert len(model.__dataclass_fields__) > 5      # the scan is not vacuous
        for field_name in model.__dataclass_fields__:
            assert not any(hint in field_name.lower() for hint in _LOCATION_FIELD_HINTS), (
                f"{model.__name__}.{field_name} looks like a location field — F5 requires "
                "the location to come from configuration, never from the data path")


def test_real_captured_alert_yields_no_location_of_any_kind():
    """The genuine captured alert is the evidence that the channel carries none:
    no location field, no coordinate-shaped value, no OS grid, anywhere in it."""
    ev = parse_alert_email(load_eml("golden/real_alert.eml"))

    assert not any(hint in key.lower()
                   for key in ev.field_provenance
                   for hint in _LOCATION_FIELD_HINTS)

    # Nothing coordinate-shaped survives in the carried text either. The subject
    # and body are searched, not just the parsed fields, so a location smuggled in
    # as free text would still be caught.
    carried = " ".join(str(v) for v in ev.raw_source_metadata.values())
    assert re.search(r"-?\d{1,3}\.\d{4,},\s*-?\d{1,3}\.\d{4,}", carried) is None  # lat,lon pair
    assert re.search(r"\b[A-Z]{2}\s?\d{4,10}\b", carried) is None                 # OS grid


# --------------------------------------------------------------------------- #
# (b) The report discloses that origin in prose.
# --------------------------------------------------------------------------- #
def test_report_states_the_site_is_described_from_the_known_deployment(repo, alias_map):
    """The disclosure sentence F5 requires, asserted verbatim: a reader must be
    told the site description is background knowledge, not a data finding."""
    _seed(repo)
    md = _agent(repo, alias_map).generate_bng_aligned(_WINDOW)

    assert "the site is described from the " "known deployment, not asserted from the data" in md
    # The reason is given, not just the disclaimer: the alerts carry no identity.
    assert "carries no camera identity or coordinates" in md
    assert "none is transmitted with the alerts" in md


def test_configured_coordinates_are_never_presented_as_surveyed_site_data(
        repo, alias_map, monkeypatch):
    """Configuration supplies a weather lookup point, NOT a surveyed location. The
    grid reference must stay the required-but-absent placeholder, and the configured
    decimal coordinates must not leak into the site section as though surveyed.

    Coordinates are set here rather than relied on as defaults — there are no
    coordinate defaults any more, so asserting against unset values would pass
    vacuously.
    """
    _seed(repo)
    md = _agent(repo, alias_map,
                site_name="Example Garden, Exampleton").generate_bng_aligned(_WINDOW)

    assert "**OS grid reference (camera location):** **NOT RECORDED — required.**" in md
    # The project's lookup coordinates are not printed as site identity.
    assert "51.5072" not in md and "-0.1276" not in md
    assert re.search(r"\b[A-Z]{2}\s?\d{4,10}\b", md) is None  # no invented grid


def test_weather_section_attributes_the_location_to_the_camera_not_the_animal(repo, alias_map):
    """The configured point is the camera's, and the report says so — the reading
    is not presented as conditions the animal experienced."""
    for i in range(3):
        repo.upsert_event(make_persisted_event(
            f"<w{i}@x>", canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, 9, 7, 0, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, 9, 7, 0, tzinfo=UTC),
            capture_time_source="filename_block_d", display_common_name="red fox",
            weather_category="sunny", weather_code_raw=1, temperature_c=15.0,
            precipitation_mm=0.0))
    md = _agent(repo, alias_map).weather_report("fox", _WINDOW)

    assert "camera's location, not by the animal" in md
    assert "no on-site sensor" in md


# --------------------------------------------------------------------------- #
# The divergence: the place name is a literal, not a configuration value.
# --------------------------------------------------------------------------- #
def test_place_name_in_the_weather_caption_follows_the_configured_location(
        repo, alias_map, monkeypatch):
    """Configure a deployment and the caption names THAT one — F37, now fixed."""
    from ttr.config import get_settings

    monkeypatch.setenv("IMAP_HOST", "h")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    monkeypatch.setenv("SITE_NAME", "Example Garden, Exampleton")
    monkeypatch.setenv("WEATHER_LATITUDE", "51.5072")
    monkeypatch.setenv("WEATHER_LONGITUDE", "-0.1276")
    get_settings.cache_clear()
    try:
        for i in range(3):
            repo.upsert_event(make_persisted_event(
                f"<w{i}@x>", canonical_binomial="Vulpes vulpes",
                event_time=datetime(2026, 7, 9, 7, 0, tzinfo=UTC),
                capture_time_utc=datetime(2026, 7, 9, 7, 0, tzinfo=UTC),
                capture_time_source="filename_block_d", display_common_name="red fox",
                weather_category="sunny", weather_code_raw=1, temperature_c=15.0,
                precipitation_mm=0.0))

        first = _agent(repo, alias_map,
                       site_name="Example Garden, Exampleton").weather_report("fox", _WINDOW)
        assert "Example Garden, Exampleton" in first

        # Reconfigure and regenerate. The caption must follow, and must no longer
        # carry the PREVIOUS site — which is the failure F37 described, expressed
        # without writing any real deployment's name into a public test file.
        second = _agent(repo, alias_map,
                        site_name="Second Field, Othertown").weather_report("fox", _WINDOW)
        assert "Second Field, Othertown" in second
        assert "Example Garden, Exampleton" not in second
    finally:
        get_settings.cache_clear()


def test_unconfigured_site_is_stated_as_unset_not_guessed(repo, alias_map):
    """No site configured: the caption says so. An absent fact is reported absent,
    never filled in with a plausible-looking place name."""
    for i in range(3):
        repo.upsert_event(make_persisted_event(
            f"<u{i}@x>", canonical_binomial="Vulpes vulpes",
            event_time=datetime(2026, 7, 9, 7, 0, tzinfo=UTC),
            capture_time_utc=datetime(2026, 7, 9, 7, 0, tzinfo=UTC),
            capture_time_source="filename_block_d", display_common_name="red fox",
            weather_category="sunny", weather_code_raw=1, temperature_c=15.0,
            precipitation_mm=0.0))
    md = _agent(repo, alias_map).weather_report("fox", _WINDOW)
    assert "site name not configured" in md
