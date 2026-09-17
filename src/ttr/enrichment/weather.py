"""Historical weather lookup + matching (Open-Meteo Archive API).

Fetches hourly historical weather for a fixed location (the camera's, not the
animal's — a limitation the report states loudly) and matches each detection's
UTC capture time to the nearest hourly record. Responses are cached per-date in
our own store so repeated backfills never re-hit the API — the data is historical
and will not change.

Layering (CLAUDE.md constraint 5): this is enrichment. It performs HTTP and
matching only; it does not know about agents, and its cache is injected (a thin
duck-typed store owned by the storage layer), so it never imports the repository.
Degradation (constraint 4): a missing capture time falls back to send time with
that fact recorded; an API gap for a date yields an ``unavailable`` category with
the reason recorded — never a dropped detection and never an interpolated guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Protocol

import httpx

from ..logging import get_logger
from . import weather_codes

logger = get_logger(__name__)

ARCHIVE_ENDPOINT = "https://archive-api.open-meteo.com/v1/archive"
CATEGORY_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class HourRecord:
    """One hourly observation from Open-Meteo."""
    hour_utc: datetime          # tz-aware, on the hour
    weather_code: Optional[int]
    temperature_c: Optional[float]
    precipitation_mm: Optional[float]


@dataclass(frozen=True)
class WeatherRead:
    """Outcome of weather enrichment for ONE detection — the storage shape.

    ``available`` is False for an API gap or a detection with no usable time;
    ``used_send_fallback`` is True when the match was made against send time
    because no filename-derived capture time existed. Both are persisted so the
    report can scope its claims per row instead of asserting blanket coverage.
    """
    category: str                       # sunny | cloudy | rainy | unknown | unavailable
    code_raw: Optional[int]
    temperature_c: Optional[float]
    precipitation_mm: Optional[float]
    match_source: str
    used_send_fallback: bool
    available: bool


class WeatherCache(Protocol):
    """Duck-typed per-date cache. The storage layer supplies the implementation."""
    def get_day(self, latitude: float, longitude: float, day: date) -> Optional[list[dict]]: ...
    def put_day(self, latitude: float, longitude: float, day: date,
                records: list[dict]) -> None: ...


def _round_to_hour(moment: datetime) -> datetime:
    """Round a UTC instant to the nearest whole hour (half rounds up)."""
    m = moment.astimezone(timezone.utc)
    floor = m.replace(minute=0, second=0, microsecond=0)
    return floor + timedelta(hours=1) if m.minute >= 30 else floor


class WeatherClient:
    """Fetch + cache + nearest-hour match against Open-Meteo's archive."""

    def __init__(
        self,
        *,
        latitude: float,
        longitude: float,
        endpoint: str = ARCHIVE_ENDPOINT,
        cache: Optional[WeatherCache] = None,
        client: Optional[httpx.Client] = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._lat = latitude
        self._lon = longitude
        self._endpoint = endpoint
        self._cache = cache
        self._client = client
        self._timeout = timeout_seconds

    # ---------------------------------------------------------------- fetching
    def ensure_range(self, start: date, end: date) -> None:
        """Populate the cache for every date in ``[start, end]`` not already held,
        in ONE API call over the missing span. Idempotent: fully-cached ranges
        make no request."""
        if self._cache is None:
            return
        missing = [d for d in _dates(start, end)
                   if self._cache.get_day(self._lat, self._lon, d) is None]
        if not missing:
            return
        lo, hi = min(missing), max(missing)
        by_day = self._fetch_range(lo, hi)          # may raise on network failure
        for d in _dates(lo, hi):
            self._cache.put_day(self._lat, self._lon, d,
                                [r.__dict__ for r in by_day.get(d, [])])

    def _fetch_range(self, start: date, end: date) -> dict[date, list["_RawHour"]]:
        params = {
            "latitude": self._lat, "longitude": self._lon,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "hourly": "weathercode,temperature_2m,precipitation", "timezone": "UTC",
        }
        resp = (self._client.get(self._endpoint, params=params) if self._client
                else httpx.get(self._endpoint, params=params, timeout=self._timeout))
        resp.raise_for_status()
        hourly = resp.json().get("hourly", {})
        times = hourly.get("time", [])
        codes = hourly.get("weathercode", [])
        temps = hourly.get("temperature_2m", [])
        precs = hourly.get("precipitation", [])
        out: dict[date, list[_RawHour]] = {}
        for i, t in enumerate(times):
            # Open-Meteo omits the zone with timezone=UTC; the value IS UTC.
            hour = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            rec = _RawHour(hour_utc=hour.isoformat(),
                           weather_code=_at(codes, i), temperature_c=_at(temps, i),
                           precipitation_mm=_at(precs, i))
            out.setdefault(hour.date(), []).append(rec)
        logger.info("weather_fetched", extra={"start": start.isoformat(),
                                              "end": end.isoformat(), "hours": len(times)})
        return out

    # ---------------------------------------------------------------- matching
    def match(self, moment: datetime, *, basis_label: str,
              used_send_fallback: bool) -> WeatherRead:
        """Match a UTC instant to the nearest cached hourly record."""
        target = _round_to_hour(moment)
        day_records = self._cached_hours(target.date())
        if day_records is None:
            return WeatherRead(
                CATEGORY_UNAVAILABLE, None, None, None,
                f"open-meteo: no record cached for {target.date().isoformat()} (API gap)",
                used_send_fallback, available=False)
        rec = day_records.get(target.isoformat())
        if rec is None or rec.weather_code is None:
            return WeatherRead(
                CATEGORY_UNAVAILABLE, None, None, None,
                f"open-meteo: no hourly value at {target.isoformat()} (gap)",
                used_send_fallback, available=False)
        category, _desc = weather_codes.categorize(rec.weather_code)
        return WeatherRead(
            category, rec.weather_code, rec.temperature_c, rec.precipitation_mm,
            f"open-meteo hourly, matched to {basis_label} (nearest hour {target.isoformat()})",
            used_send_fallback, available=True)

    def _cached_hours(self, day: date) -> Optional[dict[str, HourRecord]]:
        if self._cache is None:
            return None
        raw = self._cache.get_day(self._lat, self._lon, day)
        if raw is None:
            return None
        out: dict[str, HourRecord] = {}
        for r in raw:
            out[r["hour_utc"]] = HourRecord(
                hour_utc=datetime.fromisoformat(r["hour_utc"]),
                weather_code=r["weather_code"], temperature_c=r["temperature_c"],
                precipitation_mm=r["precipitation_mm"])
        return out


@dataclass
class _RawHour:
    hour_utc: str
    weather_code: Optional[int]
    temperature_c: Optional[float]
    precipitation_mm: Optional[float]


def _at(seq, i):
    return seq[i] if i < len(seq) else None


def _dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def weather_time_basis(event) -> tuple[Optional[datetime], str, bool]:
    """Choose the instant to match weather against for a detection, and disclose it.

    Prefers the filename-derived CAPTURE time (Decision 3, amended). Falls back to
    send time — flagged — when no capture time was recovered, so the detection is
    never dropped from the weather analysis. Returns ``(instant, label, fallback)``;
    ``instant`` is None only when no time of any kind exists."""
    if event.capture_time_source == "filename_block_d" and event.capture_time_utc:
        return event.capture_time_utc, "capture_time", False
    if event.event_time_utc:
        return event.event_time_utc, "send-time (capture_time unrecovered)", True
    return None, "no usable timestamp", False


def enrich_weather(event, client: WeatherClient) -> WeatherRead:
    """Weather enrichment for one detection — degrades, never drops."""
    instant, label, fallback = weather_time_basis(event)
    if instant is None:
        return WeatherRead(CATEGORY_UNAVAILABLE, None, None, None,
                           "no usable timestamp on this detection", False, available=False)
    return client.match(instant, basis_label=label, used_send_fallback=fallback)
