# Worked example — the published evaluation extract

This directory turns the study's own data into a project you can open, browse and
generate reports from. It ships **no images, no mailbox and no credential**: the
data is `../../docs/evaluation/data/detections_20260628-0716.csv`, the image-free
extract described in [its own README](../../docs/evaluation/data/README.md).

## Run it

**Usually you do not need to.** With the package installed, the first `ttr serve`
against an empty projects folder builds this project by itself, from copies of the
extract and alias table bundled with the package (`src/ttr/projects/example_data/`).
It happens once: delete the project and it is not rebuilt. `ttr project load-example`
builds it again on request, and `TTR_NO_EXAMPLE=1` skips the automatic step.

To build it by hand instead, from the repository root with the package installed
(`pip install -e ".[web]"`):

```bash
# 1. Choose where projects live. Must be an ABSOLUTE path.
#    Linux/macOS:
export TTR_PROJECTS_ROOT="$PWD/.ttr-example"
#    Windows PowerShell:
#    $env:TTR_PROJECTS_ROOT = "$PWD\.ttr-example"

# 2. Build the project from the extract.
ttr project import-extract \
    --csv docs/evaluation/data/detections_20260628-0716.csv \
    --name "Example Site" \
    --alias-table examples/back-garden/species_aliases.yaml \
    --site-name "Example Village, UK" \
    --latitude 51.5 --longitude -0.1

# 3. Open it. The URL printed carries a session token; every route refuses
#    without it.
ttr serve
```

You should see **475 alert events** across **2026-06-28 → 2026-07-16**, four
classes, and an alias table reported as *this project's own*.

Generating a report needs a local [Ollama](https://ollama.com) for the narrative
prose. Without one the figures still compute and the report renders with the
prose section replaced by a plain statement that it could not be generated —
that is a normal state, not a failure, and worth seeing at least once.

Nothing here reaches the network otherwise. There is no mailbox to poll, so the
ingest page's fetch controls are disabled and say why.

## What this example is, and is not

**It is** the evidence base for the two findings in
`../../docs/evaluation/`: the BioCLIP cross-check result and the VLM
confabulation measurement. Every figure in those documents is reproducible from
these 475 rows.

**It is not a wildlife survey.** 96.4% of the rows are one species — wood pigeon,
at a garden feeder. `Person`, `Car` and `CalibrationPole` never appeared, so the
reserved `nonbio:` path is untested against this data. Read the extract's README
before drawing any conclusion from a number here.

**It is not the deployment.** The site name and coordinates above are invented,
and the extract carries no Message-IDs, no addresses and no image paths. The real
deployment's project, images and mailbox are not published.

## What the loader does to the rows

Nothing, beyond writing them. `ttr project import-extract` copies each stored
value across as-is — `agreement_flag`, `taxonomic_distance`, the BioCLIP top-k and
the VLM text. It never calls a model and never recomputes a verdict, because a
loader that re-decided one would build a project that disagreed with the evidence
file it came from.

Three fields are supplied rather than copied, and each says so in the row:

| Field | Value | Why |
|---|---|---|
| `source_message_id` | `<extract-N@invalid>` | The extract has no Message-IDs. `.invalid` is reserved by RFC 2606 and can never resolve. |
| `images_present` | `none` | No image bytes are published. The image store is created and stays empty. |
| `capture_time_utc` | NULL | The extract carries alert-**send** time only (Decision 3). The row is flagged, exactly as a real row is when a filename cannot be decoded. |

## The alias table

`species_aliases.yaml` here covers **exactly the four classes in the extract**,
authored from a public taxonomy. It is not a camera's class list and is not
derived from the TrapTracker RT class list, whose redistribution terms are
unconfirmed.

Without `--alias-table` the project falls back to the bundled illustrative table,
which resolves species this data does not contain and misses none that it does —
the projects page will report the difference as *bundled example* rather than
*this project's own*.

## Cleaning up

The project lives entirely under `TTR_PROJECTS_ROOT`. Delete that directory, or
run `ttr project delete "Example Site"`, and nothing remains.
