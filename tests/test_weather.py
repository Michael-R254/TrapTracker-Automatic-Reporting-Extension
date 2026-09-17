"""Weather-correlation enrichment + report view (new stage).

Covers the mapping table, nearest-hour matching, the degrade-not-drop paths
(send-time fallback, API gap), the storage round-trip + cache, and the report
section's mandatory caveats. Network is never hit: a fake cache supplies records.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from ttr.agents.report import ReportGeneratorAgent
from ttr.agents.retrieval import RetrievalAgent
from ttr.agents.window import TimeWindow
from ttr.enrichment import weather_codes
from ttr.enrichment.weather import (
    CATEGORY_UNAVAILABLE,
    WeatherClient,
    _round_to_hour,
    enrich_weather,
    weather_time_basis,
)

from conftest import make_persisted_event

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Mapping table — the reviewable, locked-candidate decision.
# --------------------------------------------------------------------------- #
def test_wmo_mapping_covers_the_three_agreed_buckets():
    assert weather_codes.categorize(0) == ("sunny", "Clear sky")
    assert weather_codes.categorize(1)[0] == "sunny"
    assert weather_codes.categorize(2)[0] == "cloudy"
    assert weather_codes.categorize(3)[0] == "cloudy"
    assert weather_codes.categorize(45)[0] == "cloudy"       # fog
    assert weather_codes.categorize(51)[0] == "rainy"        # drizzle
    assert weather_codes.categorize(65)[0] == "rainy"        # heavy rain
    assert weather_codes.categorize(82)[0] == "rainy"        # violent showers
    assert weather_codes.categorize(95)[0] == "rainy"        # thunderstorm


def test_unrecognised_and_missing_codes_never_guess():
    assert weather_codes.categorize(999)[0] == "unknown"
    assert weather_codes.categorize(None)[0] == "unknown"


def test_snow_is_folded_into_rainy_and_flagged_in_the_table():
    assert weather_codes.categorize(71)[0] == "rainy"
    snow_rows = [r for r in weather_codes.WMO_TABLE if "snow" in r.description.lower()]
    assert snow_rows and all(r.note for r in snow_rows)       # every snow row carries the note


# --------------------------------------------------------------------------- #
# Nearest-hour matching + degradation.
# --------------------------------------------------------------------------- #
class FakeCache:
    """In-memory weather cache — the storage protocol without a DB or network."""

    def __init__(self, days: dict[date, list[dict]]):
        self._days = days

    def get_day(self, lat, lon, day):
        return self._days.get(day)

    def put_day(self, lat, lon, day, records):
        self._days[day] = records


def _hour(h, code, temp=15.0, precip=0.0):
    return {"hour_utc": datetime(2026, 7, 5, h, tzinfo=UTC).isoformat(),
            "weather_code": code, "temperature_c": temp, "precipitation_mm": precip}


def test_rounds_to_nearest_hour():
    assert _round_to_hour(datetime(2026, 7, 5, 6, 19, tzinfo=UTC)).hour == 6
    assert _round_to_hour(datetime(2026, 7, 5, 6, 45, tzinfo=UTC)).hour == 7
    assert _round_to_hour(datetime(2026, 7, 5, 6, 30, tzinfo=UTC)).hour == 7   # half rounds up


def test_match_picks_the_nearest_hour_and_categorises():
    cache = FakeCache({date(2026, 7, 5): [_hour(6, 3), _hour(7, 51)]})
    client = WeatherClient(latitude=53.44, longitude=-2.93, cache=cache)
    # 06:19 -> 06:00 (overcast, cloudy)
    r = client.match(datetime(2026, 7, 5, 6, 19, tzinfo=UTC),
                     basis_label="capture_time", used_send_fallback=False)
    assert r.available and r.category == "cloudy" and r.code_raw == 3
    assert "nearest hour 2026-07-05T06:00" in r.match_source
    # 06:45 -> 07:00 (drizzle, rainy)
    r2 = client.match(datetime(2026, 7, 5, 6, 45, tzinfo=UTC),
                      basis_label="capture_time", used_send_fallback=False)
    assert r2.category == "rainy" and r2.code_raw == 51


def test_api_gap_is_unavailable_not_interpolated():
    client = WeatherClient(latitude=53.44, longitude=-2.93, cache=FakeCache({}))
    r = client.match(datetime(2026, 7, 5, 6, 19, tzinfo=UTC),
                     basis_label="capture_time", used_send_fallback=False)
    assert not r.available and r.category == CATEGORY_UNAVAILABLE
    assert "no record cached" in r.match_source and r.code_raw is None


def test_time_basis_prefers_capture_falls_back_to_send_flagged():
    cap = make_persisted_event("<a@x>", canonical_binomial="Vulpes vulpes",
                               event_time=datetime(2026, 7, 5, 7, 0, tzinfo=UTC),
                               capture_time_utc=datetime(2026, 7, 5, 6, 19, tzinfo=UTC),
                               capture_time_source="filename_block_d")
    instant, label, fb = weather_time_basis(cap)
    assert instant.hour == 6 and label == "capture_time" and fb is False

    send_only = make_persisted_event("<b@x>", canonical_binomial="Vulpes vulpes",
                                     event_time=datetime(2026, 7, 5, 7, 0, tzinfo=UTC),
                                     capture_time_utc=None, capture_time_source="none")
    instant2, label2, fb2 = weather_time_basis(send_only)
    assert instant2.hour == 7 and "send-time" in label2 and fb2 is True


def test_send_fallback_flows_through_enrich():
    cache = FakeCache({date(2026, 7, 5): [_hour(7, 0)]})       # clear at 07:00
    client = WeatherClient(latitude=53.44, longitude=-2.93, cache=cache)
    ev = make_persisted_event("<b@x>", canonical_binomial="Vulpes vulpes",
                              event_time=datetime(2026, 7, 5, 7, 0, tzinfo=UTC),
                              capture_time_utc=None, capture_time_source="none")
    r = enrich_weather(ev, client)
    assert r.category == "sunny" and r.used_send_fallback is True
    assert "send-time" in r.match_source


# --------------------------------------------------------------------------- #
# Storage round-trip + cache.
# --------------------------------------------------------------------------- #
def test_repository_weather_roundtrip_and_cache(repo):
    from ttr.enrichment.weather import WeatherRead

    ev_id = repo.upsert_event(make_persisted_event(
        "<w@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 5, 7, 0, tzinfo=UTC)))
    repo.set_weather(ev_id, WeatherRead("rainy", 51, 16.1, 0.1,
                                        "open-meteo hourly, matched to capture_time",
                                        used_send_fallback=False, available=True))
    back = repo.query(binomial="Vulpes vulpes",
                      start_utc=datetime(2026, 7, 5, tzinfo=UTC),
                      end_utc=datetime(2026, 7, 6, tzinfo=UTC))[0]
    assert back.weather_category == "rainy" and back.weather_code_raw == 51
    assert back.temperature_c == 16.1 and back.precipitation_mm == 0.1
    assert back.weather_used_send_fallback is False

    # Cache round-trips and is keyed by rounded coords + date.
    repo.put_day(51.5072, -0.1276, date(2026, 7, 5), [_hour(6, 3)])
    assert repo.get_day(51.5072, -0.1276, date(2026, 7, 5)) is not None
    assert repo.get_day(51.5072, -0.1276, date(2026, 7, 6)) is None


# --------------------------------------------------------------------------- #
# Report section — the caveats that must appear in rendered text.
# --------------------------------------------------------------------------- #
def _weathered(mid, day, category, *, binomial="Vulpes vulpes", fallback=False,
               common="red fox"):
    return make_persisted_event(
        mid, canonical_binomial=binomial,
        event_time=datetime(2026, 7, day, 7, 0, tzinfo=UTC),
        capture_time_utc=datetime(2026, 7, day, 7, 0, tzinfo=UTC),
        capture_time_source="filename_block_d", display_common_name=common,
        weather_category=category, weather_code_raw=1, temperature_c=15.0,
        precipitation_mm=0.0, weather_used_send_fallback=fallback)


def _agent(repo, alias_map, fake_llm, threshold=20):
    return ReportGeneratorAgent(RetrievalAgent(repo, alias_map), fake_llm,
                                weather_small_sample_threshold=threshold)


def _window():
    return TimeWindow.from_dates(date(2026, 7, 8), date(2026, 7, 12))


def test_weather_report_states_N_and_the_mandatory_caveats(repo, alias_map, fake_llm):
    for i in range(15):
        repo.upsert_event(_weathered(f"<s{i}@x>", 9, "sunny"))
    for i in range(7):
        repo.upsert_event(_weathered(f"<c{i}@x>", 10, "cloudy"))
    for i in range(3):
        repo.upsert_event(_weathered(f"<r{i}@x>", 11, "rainy"))
    md = _agent(repo, alias_map, fake_llm).weather_report("fox", _window())

    assert "N = 25" in md
    assert "<svg" in md and "<path" in md                      # multi-slice pie present
    assert "| sunny | 15 | 60% |" in md
    assert "| cloudy | 7 | 28% |" in md
    assert "| rainy | 3 | 12% |" in md
    assert "| **Total categorised (N)** | **25** | 100% |" in md
    # Every mandatory caveat, verbatim enough to be unmistakable.
    assert "camera's location, not by the animal" in md
    assert "no on-site sensor" in md
    assert "does not control for time-of-day" in md
    assert "season or feeder-refill schedule" in md
    assert "Correlation is not a weather preference" in md
    assert "alert-event counts" in md.lower() and "not animal abundance" in md.lower()
    assert "The pie is a share of detections, not a preference" in md


def test_small_species_sample_is_flagged_not_presented_as_confident(repo, alias_map, fake_llm):
    for i in range(3):
        repo.upsert_event(_weathered(f"<c{i}@x>", 9, "cloudy",
                                     binomial="Meles meles", common="european badger"))
    md = _agent(repo, alias_map, fake_llm).weather_report("badger", _window())
    assert "N = 3" in md
    assert "Sample too small for a weather-preference claim" in md
    assert "below the 20-event threshold" in md


def test_large_combined_sample_is_not_flagged(repo, alias_map, fake_llm):
    for i in range(25):
        repo.upsert_event(_weathered(f"<s{i}@x>", 9, "sunny"))
    md = _agent(repo, alias_map, fake_llm).weather_report(None, _window())
    assert "all monitored species" in md
    assert "Sample too small" not in md


def test_unavailable_weather_is_disclosed_not_dropped(repo, alias_map, fake_llm):
    repo.upsert_event(_weathered("<s1@x>", 9, "sunny"))
    repo.upsert_event(_weathered("<u1@x>", 10, "unavailable"))   # API gap
    md = _agent(repo, alias_map, fake_llm).weather_report("fox", _window())
    assert "N = 1" in md                                         # unavailable excluded from N
    assert "no weather data for the exact date/hour" in md
    assert "not interpolated" in md


def test_send_fallback_events_are_disclosed_in_the_report(repo, alias_map, fake_llm):
    repo.upsert_event(_weathered("<s1@x>", 9, "sunny"))
    repo.upsert_event(_weathered("<f1@x>", 10, "cloudy", fallback=True))
    md = _agent(repo, alias_map, fake_llm).weather_report("fox", _window())
    assert "matched to **send time**, not" in md


def _seed_base_rate_cache(repo, day, *, sunny_hours, rainy_hours):
    """Populate the weather cache so the base-rate column has an exposure denominator."""
    recs = ([_hour(h, 0) for h in range(sunny_hours)]                 # code 0 = sunny
            + [_hour(12 + h, 61) for h in range(rainy_hours)])        # code 61 = rainy
    repo.put_day(51.5072, -0.1276, day, recs)


def test_base_rate_column_shows_exposure_and_the_divergence_note(repo, alias_map, fake_llm):
    from datetime import date as _date

    # Cache: 20 sunny hours, 4 rainy hours -> sunny is 83% of hours, rainy 17%.
    _seed_base_rate_cache(repo, _date(2026, 7, 9), sunny_hours=20, rainy_hours=4)
    # Detections: rainy OVER-represented vs its 17% availability.
    for i in range(5):
        repo.upsert_event(_weathered(f"<s{i}@x>", 9, "sunny"))
    for i in range(5):
        repo.upsert_event(_weathered(f"<r{i}@x>", 9, "rainy"))
    md = _agent(repo, alias_map, fake_llm).weather_report("fox", _window())

    assert "Share of window's hours" in md                # exposure column present
    assert "| rainy | 5 | 50% | 17% |" in md              # 50% of events, 17% of hours
    assert "| sunny | 5 | 50% | 83% |" in md
    # The deterministic divergence note names the gap without a cause.
    assert "than its availability" in md
    assert "not a cause" in md


def test_weather_section_appears_in_the_main_species_report(repo, alias_map, fake_llm):
    from datetime import date as _date

    _seed_base_rate_cache(repo, _date(2026, 7, 9), sunny_hours=20, rainy_hours=4)
    for i in range(3):
        repo.upsert_event(_weathered(f"<s{i}@x>", 9, "sunny"))
    agent = _agent(repo, alias_map, fake_llm)
    md = agent.generate("fox", _window())                 # the MAIN report, not weather_report
    assert "## Weather correlation" in md
    assert "<svg" in md and "N = 3" in md
    # ...and the honesty block still comes after it (weather is not the last word).
    assert md.index("## Weather correlation") < md.index("## What these figures mean")


def test_main_report_has_no_weather_section_before_backfill(repo, alias_map, fake_llm):
    # A report over records with no weather columns set must not sprout an empty
    # weather section — it appears only once the data is enriched.
    repo.upsert_event(make_persisted_event(
        "<raw@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 9, 7, 0, tzinfo=UTC),
        capture_time_utc=datetime(2026, 7, 9, 7, 0, tzinfo=UTC),
        capture_time_source="filename_block_d", display_common_name="red fox"))
    md = _agent(repo, alias_map, fake_llm).generate("fox", _window())
    assert "## Weather correlation" not in md


def test_unenriched_events_are_disclosed_not_counted(repo, alias_map, fake_llm):
    repo.upsert_event(_weathered("<s1@x>", 9, "sunny"))
    repo.upsert_event(make_persisted_event(                      # no weather columns set
        "<raw@x>", canonical_binomial="Vulpes vulpes",
        event_time=datetime(2026, 7, 10, 7, 0, tzinfo=UTC),
        capture_time_utc=datetime(2026, 7, 10, 7, 0, tzinfo=UTC),
        capture_time_source="filename_block_d", display_common_name="red fox"))
    md = _agent(repo, alias_map, fake_llm).weather_report("fox", _window())
    assert "N = 1" in md
    assert "not been weather-enriched" in md
