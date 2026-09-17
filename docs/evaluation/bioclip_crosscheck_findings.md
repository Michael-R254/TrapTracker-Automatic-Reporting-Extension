# BioCLIP cross-check on real trail-camera imagery — findings

_Empirical evaluation of the BioCLIP taxonomic cross-check (Decision 1/2) on a
genuine image from the supervisor's TrapTracker RT deployment. Recorded as
evidence for the dissertation's evaluation and future-work chapters._

> **REVISED 2026-09-03 (commit `422b5ac`).** This evaluation was written against the
> three-state cross-check (`agree` / `disagree` / `indeterminate`). That model has been
> retired in favour of a two-state, species-level verdict, with each disagreement carrying a
> measured taxonomic distance — see `docs/DECISIONS.md` Decision 2 for the superseded text
> and the evidence. **The measurements below are unchanged**: the same BioCLIP predictions on
> the same frames, re-expressed under the current model. Every outcome that previously read
> `indeterminate` here reads `disagree` now, because BioCLIP named a taxon and it was not the
> upstream species. The paragraphs that *argued from* the three-state model rather than
> reporting a measurement have been rewritten, and are marked where that happened.

## Provenance of the evidence

> **The two frames are NOT redistributed with this repository.** They are
> photographs of a private residential garden taken on a third-party deployment,
> and permission to republish them has not been established. The paths below name
> them for the record and are the paths used when the evaluation was run; the
> files themselves are held privately. Every number reported in this document is
> reproducible from the image-free extract in
> [`data/detections_20260628-0716.csv`](data/detections_20260628-0716.csv), which
> is published in full.

- **Original frame:** `docs/evidence/garden_woodpigeon_20260628_original.jpg`
  (Reolink garden camera, 28/06/2026 18:38, colour daylight; 896×512).
- **Boxed frame:** `docs/evidence/garden_woodpigeon_20260628_boxed.jpg` — the
  upstream detector's own annotated output.

The camera is a garden bird-feeder cam ("=GardenCatBird="). The subject is a
**common wood pigeon** at a feeder. The `_boxed` frame shows the upstream
TrapTracker RT verdict drawn on the image: a green box labelled
**`ColumbaPalumbus 0.97`**. This is real ground truth from the actual upstream
model, not a fixture.

> **This is a favourable case:** daylight, colour, a clear single subject, and a
> 0.97 upstream confidence — about the easiest input the system will ever see.
> The cross-check still failed to corroborate it (below): BioCLIP's top-1 was the
> wrong *Columba* species. Treat every figure here as a **lower bound** on the
> problem; night/IR frames with grain, motion blur, partial occlusion, or eyeshine
> will be worse, not better.

## Incidental confirmations of the reconstructed email format

Two claims the parser was built against (from a **reconstructed** spec, Decision
7) are corroborated by this real artifact:

1. **`_boxed` attachment naming.** The analysis gave `..._boxed.jpg` only as an
   illustrative "e.g." (`DETECTION_SYSTEM_ANALYSIS.md §4a:98`); the real boxed
   file is named exactly `..._boxed.jpg`. Our parser keys image role on that
   marker and flags the inference (`sources/email_parser.py`).
2. **`Rule` / `Best confidence` pairing.** The drawn label `ColumbaPalumbus 0.97`
   matches the reconstructed body fields `Rule: ColumbaPalumbus` /
   `Best confidence: 0.97` (`§4a:105-111`), including 2-dp rounding.

These are not a substitute for the Decision-7 golden-email validation gate (a
captured `.eml`), but they are positive signal that the reconstruction is close.

## Result 1 — BioCLIP on the full frame (what the pipeline actually does)

BioCLIP (`hf-hub:imageomics/bioclip`) runs on the **whole original image**,
because the email discards bounding-box coordinates *as data* (`§4c`) — only the
drawn box survives as pixels, so the pipeline cannot crop to the subject.

Top-5 (score-descending):

| Rank | BioCLIP taxon | Score | Note |
|---:|---|---:|---|
| 1 | *Columba fasciata* | 0.3893 | band-tailed pigeon — **North American** |
| 2 | *Ectopistes migratorius* | 0.0870 | passenger pigeon — **extinct**, N. American |
| 3 | *Poliocitellus franklinii* | 0.0534 | Franklin's ground squirrel — N. American |
| 4 | ***Columba palumbus*** | **0.0194** | **the CORRECT species — buried at rank 4** |
| 5 | *Apus apus* | 0.0175 | common swift |

Embedding: 512-d (returned OK).

`compute_crosscheck` (default **top-1** join) against three upstream tokens:

| Upstream token | Flag | Distance | Rationale |
|---|---|---|---|
| `ColumbaPalumbus` (the true label) | **disagree** | `same_genus` | BioCLIP top-1 *Columba fasciata* is a real pigeon in the right genus, but the wrong species |
| `VulpesVulpes` | **disagree** | `other_animal` | top-1 is a bird; the upstream label is a mammal |
| `MelesMeles` | **disagree** | `other_animal` | same |

The first row is the informative one, and it is the case the retired model handled worst.
*Columba fasciata* against *Columba palumbus* is a **near miss in the correct genus** — the
classifier found a pigeon and picked the wrong species of it. Under the three-state model
that outcome was reported identically to a prediction of grass: both were `indeterminate`,
because both were absent from the UK alias table. It is now a `disagree` at `same_genus`,
which is a materially different — and much more encouraging — finding about the model's
behaviour on this frame.

## Result 2 — BioCLIP on a crop of the subject (what the pipeline CANNOT do)

To isolate the cause, the original was cropped by eye to the pigeon region — an
operation the email-only pipeline **cannot perform** (no bbox numbers) — and
BioCLIP re-run:

| Rank | BioCLIP taxon | Score |
|---:|---|---:|
| 1 | ***Columba palumbus*** | **0.9202** |
| 2 | *Columba linnaeus* | 0.0211 |
| 3 | *Columba trocaz* | 0.0153 |
| 4 | *Columba leucomela* | 0.0089 |
| 5 | *Patagioenas fasciata* | 0.0053 |

On the crop, agreement against `ColumbaPalumbus` would be **`agree`** (0.92,
top-1).

## The headline number

**Same model, same image, same species — 0.92 (crop) vs 0.019 (full frame).**
The only difference is whether BioCLIP sees the *animal* or the *scene*. This is
the cost of the upstream's dropped-bounding-box decision (`§4c`), **quantified on
real data**. The cross-check's failure here is not the model and not the imaging
conditions — it is the **email seam**. Everything argued theoretically at the
analysis stage about the email being lossy is now empirical.

## Dataset-scale result — the first volume ingestion (n=475, 2026-07-16)

The single-image finding above holds **at scale, and worse**. A full pass over the
dedicated mailbox ingested **475 real alerts** (480 fetched, 5 non-alerts; every
alert nominal — both attachments, well-formed; 0 edge-case variants). BioCLIP and
the VLM ran on all 475 with **zero failures**; 0 unmapped keys, 0 undated events.

**Cross-check distribution (n=475), full-frame, under the current two-state model:**

| Flag | Count | Share |
|---|---:|---:|
| `disagree` | 460 | **96.8%** |
| `agree` | 15 | 3.2% |
| `not_evaluable` | 0 | 0.0% |

*(As originally recorded under the retired three-state model, the same 475 events read
`indeterminate` 411 / `disagree` 49 / `agree` 15. Nothing was re-measured: all 411
`indeterminate` events are the `disagree` rows below, and the not-corroborated total is
460 either way.)*

**The cross-check failed to corroborate the upstream label on 96.8% of detections.**

**How far off it was — the decomposition the three-state model could not express:**

| Distance | Count | Share of the 460 disagreements | Share of n=475 |
|---|---:|---:|---:|
| **not an animal** | **169** | **36.7%** | **35.6%** |
| same class, different order | 157 | 34.1% | 33.1% |
| same genus, different species | 49 | 10.7% | 10.3% |
| different class of animal | 45 | 9.8% | 9.5% |
| same family, different genus | 38 | 8.3% | 8.0% |
| same order, different family | 2 | 0.4% | 0.4% |

This is the point of the revision. The headline "97% not corroborated" is unchanged and was
never in dispute; what was previously unavailable is that **more than a third of the corpus
is BioCLIP naming a plant or a fungus**, while **18.9%** (87 events) is a near miss inside
the correct genus or family. Those two failure modes have entirely different implications —
one is an input problem the alert email causes, the other is ordinary classifier error — and
the retired model reported them as the same thing.

**Why — BioCLIP classifies the scene, not the animal.** On real full frames the
animal is small, so BioCLIP's top-1 is dominated by background. The real top-1 taxa:

| BioCLIP top-1 | n | What it is |
|---|---:|---|
| *Passer domesticus* | 38 | house sparrow |
| *Poa pratensis* | 31 | **Kentucky bluegrass (a lawn grass)** |
| *Columba fasciata* | 30 | band-tailed pigeon (N. American) |
| *Phyllostachys dulcis* | 15 | **bamboo** |
| *Columba palumbus* | 15 | the correct wood pigeon |
| *Columba oenas* | 14 | stock dove |
| *Apus apus* | 14 | common swift |

BioCLIP returns **grass and bamboo** as the top-1 for a pigeon-at-a-feeder frame,
and in the tail even a **mushroom** (*Amanita populiphila*) and an **Australian
bowerbird** (*Ptilonorhynchus violaceus*). The correct *Columba palumbus* is top-1
only **15 of 458** times — exactly the 15 `agree` cases.

**The `disagree` cases are the crux of the false-doubt argument.** In 49 frames
BioCLIP did not merely fail to identify the bird — it **confidently identified a
correct wood-pigeon detection as a different, real UK species**: as a **house
sparrow** (`upstream ColumbaPalumbus → Columba palumbus, but BioCLIP →
Passer domesticus`), and in other frames as a **rabbit** (`… BioCLIP →
Oryctolagus cuniculus`). Had BioCLIP been trusted as the classifier, or had the
report weighted its confident answer, it would have manufactured **doubt about a
correct label** — 49 times. This is precisely what Decision 1 (cross-check, never
replacement) and Decision 2 (three-state, `disagree` visible not silently
reconciled) exist to contain, now shown at scale.

**Dataset composition — read before drawing any conclusion.** This is **not a
wildlife survey**: **458 of 475 (96.4%) are `ColumbaPalumbus`**, the rest
`CorvusCorone` (8), `SciurusCarolinensis` (7), `FelisCatus` (2). It is effectively
a **single-species wood-pigeon-at-a-garden-feeder dataset**, so every number here is
a claim about wood pigeons at a feeder, not about trap-camera performance in
general. And **`Person`/`Car`/`CalibrationPole` never appeared**, so the `nonbio:`
reserved-key path is correct-by-construction but **untested against real data**.

**Operational note (not a data problem).** The ingestion held the IMAP connection
open across the ~40-min enrichment loop (~5 s/alert), and Gmail dropped the idle
session at teardown — the process exited non-zero on a `logout()` EOF **after all
475 rows had persisted**. Benign for the data; a real hardening item for `ttr run`.

## Three findings

1. **Geographic bias of a global foundation model used as a regional cross-check.**
   3 of BioCLIP's top-5 full-frame taxa are North American, and it ranks an
   **extinct** North American species (*Ectopistes migratorius*) above the correct
   UK one. BioCLIP has no UK prior. This is a substantive limitation of using a
   global Tree-of-Life model as a regional cross-check and was **not anticipated**
   by the background research.

2. **Decision 1 (cross-check, never replacement) is empirically vindicated.** Had
   BioCLIP been the replacement classifier, a correct `ColumbaPalumbus 0.97` would
   have been overwritten with a wrong `Columba fasciata 0.39`. The call was made on
   reasoning; this is the counterfactual, measured.

3. **Decision 2 avoided a false `agree` — but its third state was the wrong instrument.**
   *(Rewritten 2026-09-03; the original claim was that the three-state flag "caught what it
   was designed to catch".)* The outcome was correctly not an `agree`, and that much held up.
   But calling it `indeterminate` asserted that no verdict was possible, when in fact BioCLIP
   had returned a confident, checkable answer — the wrong *Columba* species. Across the
   corpus that reading was applied to 78.4% of events, which made an ordinary classifier
   result look like a broken comparison. The honest verdict is `disagree`; the useful
   information is the distance attached to it. The protection the third state was reaching
   for survives as `not_evaluable`, now confined to comparisons that genuinely cannot be
   made — and **0 events on the real corpus fall into it**.

## Expected operating characteristic

*(Rewritten 2026-09-03. The original text predicted "a high `indeterminate` rate" and
treated it as the expected operating characteristic. The prediction was right about the
magnitude and wrong about what to call it.)*

A **low corroboration rate** should be expected in practice, from two compounding causes:
(a) full-frame classification (the email drops bounding boxes), and (b) UK-only alias
coverage against BioCLIP's global taxonomy. These are not the same failure and the
taxonomic distance now separates them. Cause (a) shows up as `non_animal` and
`same_class` — the classifier reading vegetation or naming an unrelated bird, together
**70.9%** of the disagreements here. Cause (b) shows up as `same_genus` and `same_family` —
the classifier finding the right kind of animal but a species the UK table does not
contain, **18.9%**.

Only the second is a coverage limitation the alias table could in principle reduce, and an
audit of all 216 distinct unresolved labels across the full 787-event corpus found **zero**
name-matching gaps to close: no synonym, no common-name mismatch, no subspecies of a target
species. The residual is genuine taxonomic distance, not naming. The §8 watch-item (logging
every unmappable BioCLIP label with its event id) is what made that audit possible.

## Two design levers (measured, not adopted)

- **Top-k-aware agreement** (`AGREEMENT_TOPK_JOIN`, default **off**). With it on,
  the rank-4 *Columba palumbus* at **0.019** flips to `agree`. **This is a
  threshold change, not a better cross-check** — it is the join criterion moving
  until the answer looks nicer. With 31 UK classes against a global taxonomy,
  "is the upstream label anywhere in the top-5" will manufacture agreements that
  mean very little. The flag exists to measure both strategies and report both;
  the honest top-1 join remains the default and the reported number. The
  `compute_agreement` rationale always names the matching rank and score under
  top-k, so a deep match is never dressed up as a confident one.

- **The `DatabaseSource` seam.** With database/volume access, the crop is
  recoverable and BioCLIP reaches 0.92 — a genuinely meaningful cross-check. This
  is empirical support for the seam the upstream analysis recommended in `§5` and
  which this project deliberately overrode in favour of the email-first path. It
  is **not** a reason to switch now (weeks from deadline; the modular
  `DetectionSource` seam exists precisely so this is a localised future swap) —
  but it is a strong evaluation- and future-work paragraph.

## Reproduction

```bash
# Full frame (as the pipeline runs it):
ttr enrich --image docs/evidence/garden_woodpigeon_20260628_original.jpg --label ColumbaPalumbus
```
(The crop result was produced by cropping the original to the subject region and
re-running `BioClipEnricher.classify`; the pipeline itself never crops.)
