# Capture-time recovery: a phantom zero-activity day, root-caused and corrected

**Status:** identified, quantified, root-caused, corrected, and regression-tested.
**Dataset:** 475 detection events / 950 images, 2026-06-28 → 2026-07-16, one camera
(`GardenCatBird`, channel `01`), one garden feeder.
**Date of analysis:** 2026-07-22.

---

## 1. Summary

The system dated every detection by **alert-email send time**, because the alert
email carries no capture time and the camera strips EXIF. Send time is usually a
good proxy — 88.2% of alerts were sent within 60 seconds of capture — but it is
only as reliable as email delivery. A **53-hour delivery outage** caused a backlog
of older captures to be flushed under a single later send timestamp.

The visible consequence: **2026-07-06 reported zero detections when 20 had
actually been captured.** The system presented a day of missing *delivery* as a
day of absent *wildlife* — the precise failure this project's honesty layer exists
to prevent, and one the LLM narrative faithfully repeated back ("one zero-activity
day … July 6th").

Capture time was recovered from the camera's filename, which the upstream system
preserves. 475/475 filenames decoded; 24 events (5.1%) moved to their true day.

---

## 2. Where capture time was hiding

EXIF is stripped, but the Reolink-native portion of the attachment filename
survives end-to-end. Filenames carry **three** timestamps:

```
20260705_070618887_20260705_070605490_01_20260705061950000.jpg
└── A: send ────┘└── B: upstream ──┘ C  └──── D: capture ────┘
```

| Block | Value | Timezone | Identification | Evidence |
|---|---|---|---|---|
| **A** | 2026-07-05 07:06:18.887 | UTC | Alert-email **send** time | Reproduces stored `event_time_utc`; `send − A` median **−0.3s**, range −0.9…+0.3s (n=475) |
| **B** | 2026-07-05 07:06:05.490 | UTC | Upstream processing stamp | Consistently precedes A: median **2.4s**, p90 14.9s, max 96.5s |
| **C** | `01` | — | Camera channel | Constant across all 475 |
| **D** | 2026-07-05 06:19:50 | **Europe/London local** | **Capture time** | Two independent proofs below |

### D is capture time — two independent proofs

**(a) Causality.** Interpreting D as **BST→UTC** yields **0/475** negative lags and a
median capture→send delay of **33s**. Interpreting D as **UTC** yields **420/475
(88.4%)** *negative* lags — emails sent before the image existed. Only one reading
is physically possible.

**(b) Pixel overlay.** The timestamp burned into the image matches D exactly,
including in the hard case. A file sent 2026-07-07 12:21:46 whose D claims
2026-07-05 09:58:42 carries the overlay `05/07/2026 09:58:42 am SUN` — D is right
and the send time is 51.4 hours late.

> ⚠️ **The filename mixes timezones.** A and B are UTC; D is local. Parsing D as UTC
> shifts every capture by an hour — silently, because the result still looks
> plausible. The implementation converts via `zoneinfo`, never a fixed offset.

---

## 3. Format stability

- **475/475 originals parse — 0% failure.** Uniform 62-character length, channel
  always `01`, image size always 896×512. Boxed variants add `_boxed`.
- **The 100% is scoped, not a guarantee:** one camera, 19 days, entirely within
  BST. It is not evidence for another Reolink model, a multi-camera deployment, or
  captures after the October DST change. The parser therefore keeps an explicit
  fallback path even though nothing exercised it here.

---

## 4. Divergence: bimodal, not a smooth spread

| Percentile | Capture → send delay |
|---|---|
| p50 | **33 s** |
| p90 | 3.51 h |
| p95 | 16.82 h |
| p99 | 30.37 h |
| max | **53.06 h** |

| Band | Events | Share |
|---|---:|---:|
| ≤ 60 s | 419 | **88.2%** |
| 60 s – 5 min | 1 | 0.2% |
| 5 min – 1 h | 0 | 0.0% |
| 1 h – 6 h | 20 | 4.2% |
| 6 h – 24 h | 29 | 6.1% |
| > 24 h | 6 | 1.3% |

Normal operation is tight (~33s). The tail is a **separate failure mode**, not the
same distribution stretched: delivery stalls, then flushes.

---

## 5. Root cause

The send-time stream shows the outage directly:

| Gap | From | To |
|---:|---|---|
| **53.2 h** | 2026-07-05 07:06 | 2026-07-07 12:20 |
| 26.6 h | 2026-07-14 19:52 | 2026-07-15 22:30 |
| 13.7 h | 2026-07-15 22:32 | 2026-07-16 12:14 |

Captures continued throughout; only *delivery* stopped. On resumption the queue
flushed, and **one send-day (2026-07-07) absorbed three capture-days** (07-05,
07-06, 07-07 — 49 events).

### Reconciling this with the report's coverage-gap table

The generated report shows **34 events "recovered afterwards"** for this same 53.2h
silence, while this document reports **24 mis-dated events**. Both are correct; they
count different things, and an examiner comparing them should see the bridge:

| Quantity | Count |
|---|---:|
| Captures falling inside the 53.2h silence | 35 |
| − the alert that reopened the channel (its send *equals* the gap end, so it did not arrive *after* the silence) | 1 |
| **= "recovered afterwards" in the report table** | **34** |
| of those 34 — changed local calendar day | 23 |
| of those 34 — delivered on the same local day they were captured (all captured 7 Jul, before delivery resumed at 12:20) | 11 |
| 23 + the gap-closing alert (captured 5 Jul, delivered 7 Jul — mis-dated, but not "recovered") | **= 24 mis-dated** |

The two figures overlap in 23 events. **Recovery is about delivery; mis-dating is
about the calendar** — an event can be held back by a silence and still be delivered
on the day it was captured, which is what the 11 same-day events are. The report
carries a one-line note making the same point at the point of use.

**All 24 mis-datings trace to this mechanism:**

| Mechanism | Events | Share |
|---|---:|---:|
| Delivery backlog / outage flush | **24** | **100%** |
| Near-midnight boundary rounding | **0** | **0%** |

This directly contradicted the standing caveat, which blamed midnight boundaries —
a mechanism responsible for nothing here — while omitting the one responsible for
everything. **The caveat wording has been corrected** to name delivery latency and
outage flush, while keeping the true part (88.2% within 60s; day granularity
usually unaffected).

---

## 6. The correction

| Day | Stored (send-time) | Corrected (capture-time) | Δ |
|---|---:|---:|---:|
| 2026-06-28 … 2026-07-04 | *unchanged* | *unchanged* | 0 |
| **2026-07-05** | 3 | **7** | **+4** |
| **2026-07-06** | **0** ⚠️ | **20** | **+20** |
| **2026-07-07** | 49 | **25** | **−24** |
| 2026-07-08 … 2026-07-16 | *unchanged* | *unchanged* | 0 |

- 16 of 19 days unaffected; **3 days corrected**; 48 event-days reassigned.
- **The phantom zero is gone: 2026-07-06 reports 20 events, not 0.**
- 2026-07-07 is no longer inflated by two days of another period's backlog.

The table is dataset-wide (475 events). All 24 mis-dated events happen to be
*Columba palumbus*, so the per-species correction for wood pigeon (458 events) is
identical on these three days — the figures in
`docs/evaluation/reports/wood_pigeon_20260628-0716_corrected.md` match row for row.

**Verification:** every figure in this document is checked against the backfilled
database, not transcribed from the pre-build analysis — 21/21 claims match exactly
(counts, percentiles, all six lag buckets, mis-dating split, day totals, and the
correction table).

---

## 7. What was built

- **`sources/capture_time.py`** — pure decoder for block D; `zoneinfo`
  Europe/London → UTC; flags ambiguous/non-existent DST local times, non-BST
  offsets, and the impossible capture-after-send ordering.
- **Schema** — `capture_time_utc`, `capture_time_source`,
  `capture_time_tz_validated`, `capture_time_note`, added by idempotent migration.
- **`event_time_utc` is never overwritten.** Send time is retained for every row;
  the divergence between the two clocks is the evidence and is reported, not
  quietly corrected away.
- **Effective time** — `COALESCE(capture_time_utc, event_time_utc)`, defined once
  and shared by the SQL window filter and the report's day bucketing, so a row can
  never be selected by one clock and counted under another.
- **`ttr backfill-capture-time`** (`--dry-run` supported) — re-derives capture time
  for existing rows from stored filenames. No re-fetch, no network, no image files
  needed. Idempotent.
- **Report** — a *"How these events were dated"* section stating how many events
  were placed by capture time, how many fell back, how many would have landed on a
  different day under the old basis, and the largest gap.

### Decision 3, amended (not reversed)

Decision 3 recorded that send-time was the temporal key **because no capture time
was available**. Capture time is now available from a source not previously
examined, so capture time is the preferred key **where it decodes**, send time
remains stored and is the fallback. The original decision stands as the record of
what was decided and why; this is an amendment on new evidence.

---

## 8. Known limitations

1. **Timezone validated only within BST.** The dataset (2026-06-28…2026-07-16)
   predates the October 2026 DST change. GMT captures are decoded with correct
   `zoneinfo` rules but are **unvalidated against ground truth**, and flagged per
   row via `capture_time_tz_validated = 0`.
2. **Ambiguous / non-existent local times.** During the clocks-back hour a local
   time occurs twice (first occurrence assumed); during clocks-forward it does not
   exist. Both are flagged, never silently resolved.
3. **Format stability is scoped** to one camera model over 19 days (§3).
4. **Capture time is the camera's own clock**, with no independent verification of
   its accuracy or drift. The pixel overlay confirms the *filename agrees with the
   camera*, not that the camera agrees with real time.
5. **Counts remain alert-event counts, not abundance** (Decision 4). Correcting
   *when* an event is filed changes nothing about *what* it counts.

---

## 9. Reproducing

```bash
ttr backfill-capture-time --dry-run     # report what would change
ttr backfill-capture-time               # write capture_time_* columns only
pytest tests/test_capture_time.py       # 13 tests: BST->UTC, fallback, phantom zero
```

Regression tests pin the three cases that matter: the **BST→UTC conversion**
(including a GMT case a fixed +1h offset would break), the **unparseable-filename
fallback**, and the **phantom zero** — `2026-07-06` must render `20`, never `0`.
