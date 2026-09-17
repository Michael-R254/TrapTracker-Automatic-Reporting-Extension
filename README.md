# TrapTracker Automatic Reporting Extension (`ttr`)

**Turn a wildlife camera's alert emails into day-by-day reports — without letting
the report claim more than the emails can support.**

`ttr` is an independent MSc research extension that reads the detection-alert
emails sent by [Trap Tracker](https://traptracker.co.uk/), stores them with their
provenance and uncertainty as first-class columns, enriches them with an
independent BioCLIP cross-check, an RT-DETR crop, a local vision-model description
and historical weather, and generates Markdown, HTML and PDF reports in which
**every number is computed in Python and the language model only writes prose
around it**. Evaluated on one garden camera's 787 alert events, cropping to the
animal before cross-checking raised BioCLIP's agreement with the upstream label
from **110 to 307** and cut its plant-or-fungus reads from **205 to 43**; recovering
capture time from attachment filenames moved **24 of the first 475 events** to
their true day and removed a phantom zero-activity day (2026-07-06: **0 → 20**)
caused by a 53-hour email outage. The suite runs **1035 tests** with no mailbox,
no upstream system and no model weights.

![The monitoring page of the local web UI, showing the worked example: 475 alert events across 18 of 19 days, four classes, and the three statements every report carries](docs/screenshots/4-MonitoringPage.png)

![Diagram: a delivery outage flushes several days of captures under one send timestamp, producing a phantom zero-activity day; decoding capture time restores each event to its capture day](diagrams/temporal-backlog-misdating.svg)

---

## Contents

- [Results](#results)
- [Dataset](#dataset)
- [Pipeline](#pipeline)
- [Web interface](#web-interface)
- [Repository structure](#repository-structure)
- [Setup and requirements](#setup-and-requirements)
- [How to run](#how-to-run)
- [Known issues and limitations](#known-issues-and-limitations)
- [References](#references)
- [Relationship to Trap Tracker](#relationship-to-trap-tracker) · [Licence](#licence) · [Acknowledgements](#acknowledgements)

---

## Results

Every figure below is taken from a machine output, and each table names which one.
Two corpora appear, and they are not interchangeable:

- **the full corpus** — 787 alert events, 2026-06-28 → 2026-08-05, held privately.
  Its cross-check figures are published as a snapshot in
  [`docs/evaluation/baselines/baseline_phase2c_test.json`](docs/evaluation/baselines/baseline_phase2c_test.json);
- **the published extract** — the first 475 of those events, 2026-06-28 →
  2026-07-16, shipped in full as
  [`docs/evaluation/data/detections_20260628-0716.csv`](docs/evaluation/data/detections_20260628-0716.csv).

### 1. Cropping to the animal before cross-checking (full corpus, n = 787)

BioCLIP is run twice per event: once on the whole alert frame, once on a crop
proposed by an independent RT-DETR detector. Both verdicts are stored side by side.
Source: `baseline_phase2c_test.json`.

| Verdict | Full frame | Crop |
|---|---:|---:|
| `agree` | 110 (14.0%) | **307 (39.0%)** |
| `disagree` | 677 (86.0%) | 480 (61.0%) |

How far BioCLIP's top-1 sat from the upstream label:

| Taxonomic distance | Full frame | Crop |
|---|---:|---:|
| same species | 110 | 307 |
| same genus, different species | 65 | 100 |
| same family, different genus | 106 | 153 |
| same order, different family | 3 | 6 |
| same class, different order | 225 | 144 |
| different class of animal | 72 | 34 |
| **not an animal** (plant or fungus) | **205** | **43** |
| undetermined | 1 | 0 |

The full-frame failure was mostly framing: in a garden-feeder frame the animal is
small and the lawn is large. How the crop was chosen (same source):

| Crop status | Events |
|---|---:|
| `cropped_verified` — RT-DETR box confirmed by Trap Tracker's recovered box | 674 (85.6%) |
| `uncropped_ambiguous` — no crop could be chosen safely | 103 (13.1%) |
| `cropped_disputed` — the two boxes disagreed | 8 (1.0%) |
| `cropped_unverified` — single candidate, no box to check against | 2 (0.3%) |

Localisation IoU between the RT-DETR box and the recovered Trap Tracker box, over
the 682 events where both exist: median **0.896**, interquartile range 0.847–0.930.

### 2. The full-frame cross-check on the published extract (n = 475)

Recomputed directly from the CSV; you can reproduce this table with the snippet in
[`docs/evaluation/data/README.md`](docs/evaluation/data/README.md).

| Verdict | Events |
|---|---:|
| `agree` | 15 (3.2%) |
| `disagree` | 460 (96.8%) |

| Distance, among the 460 disagreements | Events | Share |
|---|---:|---:|
| not an animal | 169 | 36.7% |
| same class, different order | 157 | 34.1% |
| same genus, different species | 49 | 10.7% |
| different class of animal | 45 | 9.8% |
| same family, different genus | 38 | 8.3% |
| same order, different family | 2 | 0.4% |

Most common BioCLIP top-1 names on the full frame: *Passer domesticus* 38,
*Poa pratensis* (a lawn grass) 31, *Columba fasciata* 30, *Columba palumbus* (the
correct species) 15, *Phyllostachys dulcis* (bamboo) 15, *Apus apus* 14,
*Columba oenas* 14.

### 3. Capture time versus send time (first 475 events)

The alert email carries only its send time. Capture time was decoded from the
attachment filename. Source: a read-only query of the full-corpus database, which
is not published — the extract carries send time only.

| Measure | Value |
|---|---:|
| Filenames decoded | 475 / 475 |
| Median capture → send delay | 33 s |
| Sent within 60 s of capture | 419 (88.2%) |
| Longest delay | 53.06 h |
| Events sent *before* capture (a timezone check) | 0 |
| Events on a different local day by capture time | **24 (5.1%)** |

| Local day | By send time | By capture time |
|---|---:|---:|
| 2026-07-05 | 3 | 7 |
| **2026-07-06** | **0** | **20** |
| 2026-07-07 | 49 | 25 |

Over the full 787 events: 710 (90.2%) sent within 60 s, 27 (3.4%) on a different
local day.

### 4. Enrichment and tests

| Measure | Value | Source |
|---|---:|---|
| BioCLIP full-frame, BioCLIP crop and VLM description all succeeded | 787 / 787 | corpus database |
| Events matched to historical weather | 475 / 787 | corpus database |
| Species in the full corpus | *Columba palumbus* 759 (96.4%), *Corvus corone* 11, *Sciurus carolinensis* 11, *Felis catus* 6 | `baseline_phase2c_test.json` |
| Test suite | **1035 passed, 8 skipped, 1 deselected** in 75 s | `pytest`, 2026-09-17, Windows 11, Python 3.12.10 |

### Generated reports

[`docs/example-reports/`](docs/example-reports/) holds six PDFs generated from the
published extract: an all-species summary, four single-species reports and the
BNG-aligned format. They contain no raster images. Read them with the second known
issue below in mind: the extract carries no capture times, so these reports date
events by send time and still show the 2026-07-06 zero.

Longer write-ups of each finding:

- [BioCLIP cross-check findings](docs/evaluation/bioclip_crosscheck_findings.md)
- [VLM confabulation — a measured null result](docs/evaluation/vlm_confabulation_findings.md)
- [Capture-time recovery](docs/evaluation/capture_time_findings.md)

---

## Dataset

**Source.** Alert emails from one Trap Tracker deployment: a single camera
watching a bird feeder in a private domestic garden in the UK, forwarded to a
dedicated Gmail mailbox. Each email carries the upstream class token, its best
confidence to two decimal places, the send time, and two JPEG attachments (the
original frame and a copy with Trap Tracker's box drawn on it).

**Composition.** 787 alert events from 2026-06-28 to 2026-08-05, 96.4% wood pigeon.
It is a wood-pigeon-at-a-feeder dataset, not a wildlife survey. The classes
`Person`, `Car` and `CalibrationPole` never occur.

**What is published.** The camera images, the emails and the database are **not**
published: the images show a private garden, and the emails carry addresses and
message IDs. What is published is an image-free extract of the first 475 events,
one row per alert:

- **Download:** [`detections_20260628-0716.csv`](https://raw.githubusercontent.com/Michael-R254/TrapTracker-Automatic-Reporting-Extension/main/docs/evaluation/data/detections_20260628-0716.csv)
  (607 KB, UTF-8)
- **Columns:** upstream label and confidence, resolved binomial, send time,
  BioCLIP top-k and verdict, taxonomic distance with BioCLIP's own ranks, and the
  VLM description. Documented in full in
  [`docs/evaluation/data/README.md`](docs/evaluation/data/README.md).
- **Species:** *Columba palumbus* 458, *Corvus corone* 8, *Sciurus carolinensis* 7,
  *Felis catus* 2.
- **Not included:** image bytes or paths, email bodies, Message-IDs, addresses,
  capture times, and the full corpus's crop-path columns.

The `vlm_description` column is kept deliberately: it is the evidence for the
confabulation finding. It is model-written prose about a private garden, and by
that very finding its specific details are unreliable.

**Models** (downloaded on first use, not redistributed):
[BioCLIP](https://huggingface.co/imageomics/bioclip) via `pybioclip`,
[RT-DETR `PekingU/rtdetr_r50vd_coco_o365`](https://huggingface.co/PekingU/rtdetr_r50vd_coco_o365)
via `transformers`, and [LLaVA](https://ollama.com/library/llava) via Ollama.

---

## Pipeline

```
alert mailbox (Gmail, IMAP)
   │
   ▼  1. Ingest ─────── sources/      headers first; download only unseen messages
   ▼  2. Parse ──────── sources/      alert → DetectionEvent, recording what is MISSING
   ▼  3. Date ───────── sources/      decode capture time from the filename; keep send time
   ▼  4. Crop ───────── crop/         RT-DETR proposes, Trap Tracker's burned-in box corroborates
   ▼  5. Enrich ─────── enrichment/   BioCLIP ×2 · Ollama VLM · Open-Meteo weather
   ▼  6. Store ──────── storage/      SQLite, one row per alert, provenance as columns
   ▼  7. Report ─────── agents/       figures in Python, then prose from the LLM, then a gate
   ▼  8. Present ────── web/, cli.py  Markdown · HTML · PDF · local web UI
```

1. **Ingest** (`sources/email_fetcher.py`, `sources/seen_store.py`). Each poll scans
   message headers, works out which messages are new, and downloads only those.
   The IMAP connection is closed before enrichment, so a long BioCLIP pass cannot
   outlive the server's idle timeout. A message is marked seen only after every
   durable write succeeds; re-delivery is a no-op on a unique key.
2. **Parse** (`sources/email_parser.py`). A pure function from an email to a
   `DetectionEvent`. Absent fields — no bounding box, no capture time, no camera
   identity — are recorded as absent rather than defaulted.
3. **Date** (`sources/capture_time.py`). Decodes the Reolink filename's capture
   block as Europe/London local time via `zoneinfo`, flags ambiguous DST times, and
   falls back to send time with a flag when a filename does not decode. Send time
   is never overwritten.
4. **Crop** (`crop/`). RT-DETR proposes candidate boxes on the clean frame without
   seeing the label. Trap Tracker's box is recovered from the pixels of the boxed
   attachment (`banner_ocr.py`, `overlay.py`) and used only to *verify* the choice,
   by IoU, never as crop geometry — so the cross-check stays independent.
5. **Enrich** (`enrichment/`). BioCLIP classifies the full frame and the crop;
   `agreement.py` turns each top-1 into a two-state verdict plus a taxonomic
   distance. `ollama_enricher.py` describes the boxed frame as untrusted text.
   `weather.py` matches each event to Open-Meteo archive data at the project's
   coordinates. Any enricher failure is stored on the row; it never halts the run
   or drops the event.
6. **Store** (`storage/`). One SQLite database per project, created from
   `schema.sql` and upgraded by idempotent migrations.
7. **Report** (`agents/`). `RetrievalAgent` selects rows by species and window.
   `ReportGeneratorAgent` computes every count, trend, comparison and chart first,
   then asks the LLM for a summary around those figures. A pure gate
   (`narrative_violations`) rejects prose that describes alert events as animals;
   after three non-compliant attempts the summary is withheld with the reason. If
   the LLM is unreachable, the report renders in full without the paragraph.
8. **Present** (`cli.py`, `cli_project.py`, `web/`). The CLI, and a FastAPI web UI
   with a project picker, report pages, a live ingest console and PDF export
   through an installed Chrome or Edge. Model-derived text is sanitised with `nh3`;
   the report's own charts arrive through a separate nonce-keyed channel, so model
   output cannot become SVG on the page.

Every report states three things unconditionally: counts are alert events, not
animals; how each event was dated; and which parts of the email format were
validated against a real captured alert.

---

## Web interface

`ttr serve` starts a local web UI (see [How to run](#how-to-run)). Every screenshot
below shows the worked example project, built from the published extract; the
reports page is the one at the top of this README.

### Projects

Each project has its own database, image store, mailbox and species table. Its card
shows the alert-event count and a daily chart, the dated range, whether a mailbox
credential is stored, and the site on a map.

![The projects page: one card for Example Site with 475 alert events over 19 days, a daily bar chart, the mailbox credential status and a site map, beside a panel for creating a project](docs/screenshots/1-ProjectPage1.png)

Below the cards, the page sets out which project actions the browser can do and
which stay in the terminal. Creating a project works in both, but the terminal is
safer for the app password: `getpass` is out of reach of browser autofill and
history.

![The "Browser or terminal?" panel: a table of project actions marking which can be done in the browser and which only in the terminal, beside the matching ttr project commands](docs/screenshots/2-ProjectPage2.png)

### Creating a project

The form takes the site (name, site name, optional coordinates for weather), the
Gmail alert inbox and its app password. The password is verified against the
mailbox before anything is written, then stored in the operating system's
credential store.

<img src="docs/screenshots/3-CreateProject.png" width="320" alt="The Create a project form, with Site, Mailbox and Species steps and a warning that a password typed into a browser is within reach of autofill and history">

### Choosing a report

A report covers all species, one species, or the BNG-aligned habitat-condition
format. Species are listed with their alert-event counts in the chosen window.

<img src="docs/screenshots/5-ReportTypes.png" width="370" alt="The report menu: All species, BNG-aligned monitoring report, and the four species in the dataset with their counts">

### Ingesting alerts

The ingest page polls the project's mailbox, runs BioCLIP and the vision model on
each image, and stores one row per image. It names the files it writes to, and
stays disabled until a mailbox credential exists.

![The Ingest new alerts page: Fetch now and Poll continuously buttons, where the run reads and writes, 475 rows stored, and a notice that fetching is disabled until a mailbox credential is set](docs/screenshots/6-IngestAlerts.png)

---

## Repository structure

```
TrapTracker-Automatic-Reporting-Extension/
├── src/ttr/
│   ├── cli.py, cli_project.py   the `ttr` command and `ttr project` subcommands
│   ├── config.py                machine-level settings (.env)
│   ├── pipeline.py              poll → parse → crop → enrich → store
│   ├── sources/                 IMAP fetcher, email parser, capture-time decoder, seen-store
│   ├── crop/                    RT-DETR detector, box recovery from pixels, crop decision
│   ├── enrichment/              BioCLIP, agreement verdicts, Ollama VLM, weather
│   ├── storage/                 SQLite schema, migrations, repository
│   ├── agents/                  retrieval and report generation
│   ├── agentdefs/               prompt definitions for the LLM components
│   ├── projects/                multi-project registry, credentials, locking, extract import
│   ├── species/                 alias table (bundled illustrative example) and taxonomy
│   └── web/                     FastAPI UI, ingest page, PDF export, auth, theme
├── tests/                       78 modules; fixtures include a redacted real alert email
├── docs/
│   ├── evaluation/
│   │   ├── data/                the published 475-row extract and its README
│   │   ├── baselines/           full-corpus snapshots (distributions and digests)
│   │   ├── reports/             Markdown reports before and after capture-time correction
│   │   ├── *_findings.md        the three evaluation write-ups
│   │   └── *.py                 provenance audit, backfill and image-scan scripts
│   ├── example-reports/         six generated PDF reports
│   └── screenshots/             six screenshots of the web UI (example project only)
├── diagrams/                    two SVG diagrams of the dating problem
├── examples/back-garden/        worked example: alias table and instructions
├── config/                      local configuration (your alias table goes here)
├── docker/compose.yaml          web UI, optional ingest loop, optional Ollama
├── Dockerfile
├── .github/workflows/ci.yml     tests on 3.11 and 3.12, privacy guards, lint
├── .env.example                 every machine-level setting, with defaults
├── pyproject.toml
└── LICENSE                      MIT
```

---

## Setup and requirements

**Python 3.11 or newer.** CI runs 3.11 and 3.12; the figures above were produced
on 3.12. Nothing else is required for the core install and the test suite.

Optional, depending on what you want to run:

| For | You need |
|---|---|
| The web UI and PDF export | the `[web]` extra, and an installed Chrome or Edge for PDFs |
| The BioCLIP cross-check | the `[enrich]` extra (PyTorch; weights download on first use) |
| The RT-DETR crop path | the `[detect]` extra (PyTorch and `transformers`) |
| Report prose and image descriptions | [Ollama](https://ollama.com/) running locally, with `ollama pull llava` |
| Live ingestion | a dedicated Gmail mailbox with 2-Step Verification and an [app password](https://myaccount.google.com/apppasswords) |

Everything runs on CPU by default (`DETECTOR_DEVICE=cpu`, `BIOCLIP_DEVICE=cpu`).

```bash
git clone https://github.com/Michael-R254/TrapTracker-Automatic-Reporting-Extension.git
cd TrapTracker-Automatic-Reporting-Extension

python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -e ".[dev]"            # core + web UI + test tooling
pip install -e ".[enrich,detect]"  # optional: BioCLIP and RT-DETR (large download)
```

Machine-level settings (model names, devices, Ollama endpoint, browser path) all
have working defaults. To change one, copy [`.env.example`](.env.example) to `.env`
and edit it. Mailbox credentials never go in `.env`: they are stored in your
operating system's credential store when you create a project.

**Docker** (bundles Chromium for PDF export; projects live on a named volume):

```bash
docker compose -f docker/compose.yaml build
docker compose -f docker/compose.yaml up -d
```

Build with `TTR_EXTRAS=web,enrich,detect` for the ML stack. A container has no
credential store, so a mailbox password is passed in as `TTR_IMAP_PASSWORD` from
the invoking shell; see the comments in
[`docker/compose.yaml`](docker/compose.yaml).

Each `up` restarts the server and mints a new session token, printed by
`docker compose -f docker/compose.yaml logs ttr`. To keep one URL instead, set
`TTR_UI_TOKEN` in the shell before `up`, to at least 32 URL-safe characters
(`python -c "import secrets; print(secrets.token_urlsafe(32))"`), and open
`http://127.0.0.1:8000/?token=<that value>`. The trade-off: the token then sits in
an environment variable, readable by `docker inspect`, and lasts until you change it.

---

## How to run

### Tests

```bash
pytest
```

Tests needing model weights are deselected by default (`pytest -m models` to run
them); browser and live-IMAP tests skip themselves when those are absent.

### The worked example — no mailbox, no models

This loads the published extract as a project you can browse and report on. Full
notes: [`examples/back-garden/README.md`](examples/back-garden/README.md).

```bash
# 1. Where projects live — an ABSOLUTE path.
export TTR_PROJECTS_ROOT="$PWD/.ttr-example"          # macOS/Linux
# $env:TTR_PROJECTS_ROOT = "$PWD\.ttr-example"        # Windows PowerShell

# 2. Build the project from the extract (takes about a second).
ttr project import-extract \
    --csv docs/evaluation/data/detections_20260628-0716.csv \
    --name "Example Site" \
    --alias-table examples/back-garden/species_aliases.yaml \
    --site-name "Example Village, UK" \
    --latitude 51.5 --longitude -0.1

# 3a. Browse it. The printed URL carries a session token.
ttr serve

# 3b. Or print a report. --window counts back from TODAY, so it must reach
#     2026-06-28 to include the data: 90d does as of September 2026.
ttr report --all-species --window 90d --out .ttr-example/all-species.md
```

In the web UI, the **All records** window preset avoids the date arithmetic.
Without Ollama the report still renders, with the summary replaced by a statement
that it could not be generated.

### A live deployment

```bash
ttr project create                                   # name, Gmail inbox, app password
ttr project set-site "North Field Camera" --latitude 54.97 --longitude -1.61
ttr project test-connection "North Field Camera"     # reports the message count found
ttr --project "North Field Camera" run               # poll → enrich → store, until stopped
```

`project create` connects to the mailbox before writing anything; on failure
nothing is created. Only Gmail is accepted. If `test-connection` reports zero
messages while alerts are arriving, a Gmail filter is probably archiving them out
of the inbox.

### Common commands

| Command | What it does |
|---|---|
| `ttr run --once` | Poll the mailbox once, enrich and store, then exit |
| `ttr report --species fox --window 7d` | Day-by-day report for one species (common name or binomial) |
| `ttr report --all-species --window 30d` | Cross-species summary |
| `ttr report --bng-aligned --window 90d --out r.md` | Habitat-condition evidence format |
| `ttr enrich --image frame.jpg --label VulpesVulpes` | One-off BioCLIP cross-check on an image |
| `ttr backfill-capture-time --dry-run` | Show which stored events would be re-dated |
| `ttr backfill-weather --dry-run` | Show which events would gain weather |
| `ttr recompute-crops` / `ttr recompute-crop-crosscheck` | Rebuild the crop path over stored rows |
| `ttr serve` | Local web UI for every project |
| `ttr project list` | Projects, the active one, and which alias table each uses |

Every command names the project it chose and the rule that chose it
(`--project`, then `TTR_PROJECT`, then the active project). Commands that rewrite
stored rows require an explicit `--project` once more than one project exists.

---

## Known issues and limitations

**Evidence**

- **One camera, one site, one dominant species.** 96.4% of events are wood pigeon.
  Every result is a claim about that camera and those model versions, not about
  camera traps, BioCLIP or LLaVA in general.
- **The example reports show a bug the pipeline has fixed.** The published extract
  has no capture times, so a project built from it dates everything by send time,
  and `2026-07-06` shows 0 events instead of 20. The capture-time results cannot be
  reproduced from the published data.
- **The full-corpus figures are a snapshot.** The 787-event database is private; its
  distributions are published in `docs/evaluation/baselines/`, but the rows are
  not.
- **The non-species path is untested on real data.** `Person`, `Car` and
  `CalibrationPole` never occurred.
- **Capture-time decoding is validated only in British Summer Time**, for one
  camera model. Winter (GMT) times use correct `zoneinfo` rules but are flagged
  per row as unvalidated.
- **VLM descriptions are unreliable by measurement.** LLaVA invents on-image text
  (dates, URLs, temperatures) and occasionally animals. Descriptions are shown as
  unverified and nothing parses or counts them.

**Software**

- **Gmail only.** Microsoft personal accounts are refused because Microsoft
  withdrew app-password IMAP access; other providers are refused rather than
  guessed.
- **The web UI is plain HTTP.** It is gated by a session token (per launch, unless
  pinned with `TTR_UI_TOKEN`) and binds `127.0.0.1`; binding another interface
  requires `--i-understand-no-auth`. Use an SSH tunnel for remote access.
- **Docker's Chromium runs with `--no-sandbox`**, because Chromium's sandbox does
  not start under Docker's default security profile. It runs as an unprivileged
  user and prints only the server's own sanitised page.
- **`--window` counts back from now**, with no fixed-date option on the CLI.
- **Lint checks errors only** (`ruff --select E9,F63,F7,F82`), not style.
- **Deprecation warnings** from the current test run: Starlette's `httpx` test
  client, and `Image.getdata` in `docs/evaluation/embedded_image_scan.py`, which
  Pillow 14 removes.

---

## References

- Stevens, S., Wu, J., Thompson, M. J., et al. (2024). *BioCLIP: A Vision
  Foundation Model for the Tree of Life.* CVPR 2024.
  [arXiv:2311.18803](https://arxiv.org/abs/2311.18803) ·
  [model](https://huggingface.co/imageomics/bioclip) ·
  [pybioclip](https://github.com/Imageomics/pybioclip)
- Zhao, Y., Lv, W., Xu, S., et al. (2024). *DETRs Beat YOLOs on Real-time Object
  Detection.* CVPR 2024. [arXiv:2304.08069](https://arxiv.org/abs/2304.08069) ·
  [model](https://huggingface.co/PekingU/rtdetr_r50vd_coco_o365)
- Liu, H., Li, C., Wu, Q., & Lee, Y. J. (2023). *Visual Instruction Tuning.*
  NeurIPS 2023. [arXiv:2304.08485](https://arxiv.org/abs/2304.08485) ·
  [Ollama `llava`](https://ollama.com/library/llava)
- Zippenfenig, P. (2023). *Open-Meteo.com Weather API.* Zenodo.
  [doi:10.5281/zenodo.7970649](https://doi.org/10.5281/zenodo.7970649) ·
  [historical weather API](https://open-meteo.com/en/docs/historical-weather-api)
- Trap Tracker. [traptracker.co.uk](https://traptracker.co.uk/) ·
  [licence and permitted use](https://traptracker.co.uk/licience-permitted-use/)

---

## Relationship to Trap Tracker

**This is an independent MSc research extension. It is not an official Trap
Tracker component, and it is not endorsed by or affiliated with the Trap Tracker
project.**

[Trap Tracker](https://traptracker.co.uk/) is a wildlife-detection system from the
**Conservation AI Research Group** at the **School of Computer Science and
Mathematics, Liverpool John Moores University**, led by **Dr Paul Fergus**. It runs
the detection models and emails an alert when it detects something.

This project consumes one thing from it: those alert emails. It does not import,
link against, vendor or execute any Trap Tracker code, and it does not read its
database or image directories. Trap Tracker states on its
[licence and permitted-use page](https://traptracker.co.uk/licience-permitted-use/)
that it is released under **CC BY-NC 4.0** unless otherwise stated, and that its AI
models and trained weights remain the intellectual property of the Conservation AI
Research Group.

## Licence

This repository's own source code and documentation are **MIT** — see
[`LICENSE`](LICENSE). That covers this project's code only, not third-party
material.

## Acknowledgements

- **Trap Tracker**, the **Conservation AI Research Group**, and the **School of
  Computer Science and Mathematics, Liverpool John Moores University** — for the
  detection system this work extends.
- **Dr Paul Fergus**, who leads the Trap Tracker initiative.
- The deployment supervisor, for the camera deployment and the alert stream this
  project was built and evaluated against.
