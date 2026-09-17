"""WMO weather-code → category mapping (PURE, inspectable, reviewable).

Open-Meteo reports each hour's conditions as a single WMO code (0–99). This module
folds those into the three review-agreed buckets — ``sunny`` / ``cloudy`` /
``rainy`` — via one explicit table, so the categorization is auditable rather than
buried in inline magic numbers. It is a **candidate locked decision**: once the
table below is agreed it should be recorded as Decision 8, and any later change
recorded as an amendment rather than edited in place.

Design commitments (consistent with the rest of ``ttr``):
- **Never fabricate.** A code not in the WMO table maps to ``"unknown"`` with the
  raw code preserved, never guessed into a bucket.
- **Nothing is lost.** ``weather_code_raw`` is always stored alongside the category,
  so folding (e.g. snow → precipitation) is reversible and reviewable.
- **Snow is a documented simplification.** The three agreed buckets have no snow
  bucket; snow/wintry codes fold into ``rainy`` (the precipitation bucket) and are
  flagged as such in the table. The evaluated dataset is summer (June–July,
  Liverpool), so no snow occurs in it — but the mapping is complete for honesty,
  not just for the data in hand.
"""

from __future__ import annotations

from typing import NamedTuple

SUNNY = "sunny"
CLOUDY = "cloudy"
RAINY = "rainy"
UNKNOWN = "unknown"


class WmoRow(NamedTuple):
    code: int
    category: str
    description: str
    note: str = ""


# The mapping table — one row per WMO code Open-Meteo can emit. Reviewable at a
# glance; the `note` column flags the deliberate simplifications.
WMO_TABLE: tuple[WmoRow, ...] = (
    WmoRow(0,  SUNNY,  "Clear sky"),
    WmoRow(1,  SUNNY,  "Mainly clear"),
    WmoRow(2,  CLOUDY, "Partly cloudy"),
    WmoRow(3,  CLOUDY, "Overcast"),
    WmoRow(45, CLOUDY, "Fog"),
    WmoRow(48, CLOUDY, "Depositing rime fog"),
    WmoRow(51, RAINY,  "Drizzle: light"),
    WmoRow(53, RAINY,  "Drizzle: moderate"),
    WmoRow(55, RAINY,  "Drizzle: dense"),
    WmoRow(56, RAINY,  "Freezing drizzle: light"),
    WmoRow(57, RAINY,  "Freezing drizzle: dense"),
    WmoRow(61, RAINY,  "Rain: slight"),
    WmoRow(63, RAINY,  "Rain: moderate"),
    WmoRow(65, RAINY,  "Rain: heavy"),
    WmoRow(66, RAINY,  "Freezing rain: light"),
    WmoRow(67, RAINY,  "Freezing rain: heavy"),
    WmoRow(71, RAINY,  "Snow fall: slight",   "snow folded into rainy (precipitation)"),
    WmoRow(73, RAINY,  "Snow fall: moderate", "snow folded into rainy (precipitation)"),
    WmoRow(75, RAINY,  "Snow fall: heavy",    "snow folded into rainy (precipitation)"),
    WmoRow(77, RAINY,  "Snow grains",         "snow folded into rainy (precipitation)"),
    WmoRow(80, RAINY,  "Rain showers: slight"),
    WmoRow(81, RAINY,  "Rain showers: moderate"),
    WmoRow(82, RAINY,  "Rain showers: violent"),
    WmoRow(85, RAINY,  "Snow showers: slight", "snow folded into rainy (precipitation)"),
    WmoRow(86, RAINY,  "Snow showers: heavy",  "snow folded into rainy (precipitation)"),
    WmoRow(95, RAINY,  "Thunderstorm: slight or moderate"),
    WmoRow(96, RAINY,  "Thunderstorm with slight hail"),
    WmoRow(99, RAINY,  "Thunderstorm with heavy hail"),
)

_BY_CODE: dict[int, WmoRow] = {row.code: row for row in WMO_TABLE}

#: Fixed category order for stable rendering / colour assignment.
CATEGORY_ORDER: tuple[str, ...] = (SUNNY, CLOUDY, RAINY, UNKNOWN)


def categorize(code: int | None) -> tuple[str, str]:
    """``(category, description)`` for a WMO code.

    An unrecognised or missing code yields ``("unknown", …)`` — never a guessed
    bucket. The raw code is stored separately by the caller, so this is lossless.
    """
    if code is None:
        return UNKNOWN, "no weather code"
    row = _BY_CODE.get(int(code))
    if row is None:
        return UNKNOWN, f"unrecognised WMO code {code}"
    return row.category, row.description
