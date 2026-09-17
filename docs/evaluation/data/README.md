# Evaluation dataset extract

`detections_20260628-0716.csv` — an **image-free** per-detection extract of the
first volume ingestion, sufficient to reproduce every claim in
`../bioclip_crosscheck_findings.md` and `../vlm_confabulation_findings.md` without
shipping the deployment's camera images or any email metadata.

It carries no identifiers — no addresses, Message-IDs, paths or image bytes (see
"Deliberately excluded" below). It is **not** free of description of a private
place: read the next section before publishing or citing it.

## Provenance

- **Source:** one full pass over the dedicated recipient mailbox (Decision 6),
  ingested 2026-07-16 through the production pipeline (parse → BioCLIP + Ollama VLM
  → store). 475 alert detections; 0 unmapped, 0 undated, 0 enrichment failures.
- **Re-exported 2026-09-03** after the cross-check moved to a two-state,
  species-level verdict (commit `422b5ac`; see `../../DECISIONS.md` Decision 2).
  **Nothing was re-measured.** The same 475 events, the same BioCLIP predictions and
  the same VLM text — re-exported so the `agreement_flag` column reports the current
  model and the audit columns are present. The re-export asserts, event for event,
  that `event_time_utc`, `upstream_label`, `bioclip_top1` and `vlm_description` are
  unchanged from the previous file before writing.
- **Event-time span:** 2026-06-28 → 2026-07-16 (UTC). Times are alert-**send** time,
  not capture time (Decision 3).
- **Deliberately excluded** (never in this file): image bytes/paths, email bodies,
  Message-IDs, From/To addresses. Verified: a grep for `@gmail`, `mx.google`,
  `message-id`, `image_store`, `.eml`, and the mailbox handles returns nothing.

## What `vlm_description` is, and why it is kept

That column holds **vision-model descriptions of a private domestic garden** —
the camera's field of view. Across the 475 rows it refers to a fence (212), a
garden (117), a house (16), and occasionally a patio, shed, door, window or
street. No address, no name and no image: prose about a place.

It is retained deliberately, and editing it is not on the table. This file is the
**evidence base for the confabulation finding** in
`../vlm_confabulation_findings.md`, which measures how often the model invents
on-image text — timestamps, URLs, temperatures, camera labels. A dataset trimmed
to look tidier would be evidence for a claim about a dataset that no longer
exists, which is the failure mode this project's honesty rules exist to prevent.
The available response to uncomfortable evidence here is disclosure, not editing.

Two consequences follow, and they point in opposite directions:

- **For a reader:** the specifics are unreliable *by the very finding this data
  supports*. A description mentioning a house number or a street is more likely a
  confabulation than an observation — that is the measured result, not a hedge.
  Do not treat any detail in this column as a fact about a real location.
- **For publication:** it is prose describing someone's garden. That is a lower
  order of exposure than a photograph, and unlike the images it is not excluded
  from a public release — but it is a deliberate decision rather than an
  oversight, and it is recorded here so it reads as one.

## Dataset composition caveat (read before drawing conclusions)

**96.4% single-species:** `ColumbaPalumbus` 458, `CorvusCorone` 8,
`SciurusCarolinensis` 7, `FelisCatus` 2. This is effectively a **wood-pigeon-at-a-
garden-feeder** dataset, not a wildlife survey — every figure is a claim about that,
not about trap-camera performance in general. `Person`/`Car`/`CalibrationPole` never
appeared, so the `nonbio:` path is untested against real data.

## Columns

| Column | Meaning |
|---|---|
| `idx` | Sequential row id (1..475). NOT a Message-ID. |
| `upstream_label` | Raw upstream class token (authoritative), e.g. `ColumbaPalumbus`. |
| `upstream_confidence` | Upstream best confidence, as emailed (2 dp, best-per-label). |
| `canonical_binomial` | Species binomial resolved via the alias table (Decision 5). |
| `event_time_utc` | Alert-send time, ISO-8601 UTC. |
| `bioclip_top1` | BioCLIP's top-1 taxon on the full original frame. |
| `bioclip_top1_score` | Its score. |
| `bioclip_topk_json` | Full top-k `[[taxon, score], ...]`, score-descending. |
| `agreement_flag` | `agree` / `disagree` (Decision 2 as revised, species-level top-1 join). A third value `not_evaluable` exists for comparisons that are impossible rather than negative; **it does not occur in this extract**. |
| `cross_check_status` | `evaluable` / `not_evaluable`. The binary is reported over evaluable rows; all 475 here are evaluable. |
| `resolution_basis` | How the verdict was reached: `exact_species_match`, `subspecies_collapsed_match`, `no_taxonomic_match`, `unresolved_prediction`. |
| `matched_rank` | `species` on an agreement, `none` otherwise. The verdict is species-level only. |
| `taxonomic_distance` | How far BioCLIP's top-1 sat from the upstream label: `same_species`, `same_genus`, `same_family`, `same_order`, `same_class`, `other_animal`, `non_animal`, `undetermined`. This is the column that replaced the retired `indeterminate` bucket. |
| `bioclip_top1_kingdom` … `_genus` | The top-1's own ranks **as BioCLIP reported them**, not looked up or inferred. Empty where the model did not supply a rank. |
| `agreement_rationale` | Human-readable reason for the flag. |
| `vlm_description` | Ollama `llava` description of the boxed image. **Unverified model output** — it fabricates on-image text and can invent animals/species (see `../vlm_confabulation_findings.md`). |

## Reproducing the headline numbers

```python
import csv, collections
rows = list(csv.DictReader(open("detections_20260628-0716.csv", encoding="utf-8")))
collections.Counter(r["agreement_flag"] for r in rows)
#   -> {'disagree': 460 (96.8%), 'agree': 15 (3.2%)}
collections.Counter(r["taxonomic_distance"] for r in rows if r["agreement_flag"] == "disagree")
#   -> {'non_animal': 169, 'same_class': 157, 'same_genus': 49,
#       'other_animal': 45, 'same_family': 38, 'same_order': 2}
collections.Counter(r["bioclip_top1"] for r in rows).most_common(6)
#   -> Passer domesticus 38, Poa pratensis 31 (grass), Columba fasciata 30,
#      Columba palumbus 15 (correct), Phyllostachys dulcis 15 (bamboo), Apus apus 14
```

Under the retired three-state model these same rows read `indeterminate` 411 /
`disagree` 49 / `agree` 15. The not-corroborated total is 460 either way; what
changed is that the 411 are no longer an undifferentiated bucket.

**The `taxonomic_distance` column is where the finding lives.** Filter
`taxonomic_distance == "non_animal"` for the 169 events (35.6% of the extract) where
BioCLIP's best guess was a plant or a fungus — the whole-frame-read artefact of
classifying un-cropped alert images. Filter `same_genus` or `same_family` for the 87
(18.9% of disagreements) where it found the right kind of bird but the wrong species.
Filter `agreement_flag == "disagree" and taxonomic_distance == "other_animal"` for the
false-doubt crux the previous version of this note pointed at: BioCLIP confidently
calling a correct wood-pigeon detection a rabbit (`Oryctolagus cuniculus`). Note that
`Passer domesticus` — 38 events, the single most common top-1 — is `same_class`, not
`other_animal`: it is a bird, just the wrong order.
