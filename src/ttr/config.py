"""MACHINE-level configuration (pydantic-settings).

What this describes is the HOST: which model checkpoints to load, which device to
run them on, where Ollama listens, which browser prints a PDF. It no longer
describes a deployment.

Everything per-deployment — the mailbox and its credential, the database, the
image store, the site and its coordinates, the species tables — belongs to a
PROJECT and lives in that project's ``project.toml`` and the OS credential store.
``ttr project create`` sets one up; ``ProjectContext`` is what the pipeline, the
CLI and the web layer read it through.

The split matters beyond tidiness. A value that describes the host must be the
SAME for every project — two projects disagreeing about ``crop_iou_tau`` would
make their crop verdicts incomparable, and about ``ollama_model`` would make
their narratives incomparable. A value that describes a deployment must be
DIFFERENT per project, and holding one of those globally is how a command ends up
writing to a database the user did not choose.

Settings are loaded lazily via :func:`get_settings` so that commands which need
no configuration (``ttr --help``) never trigger validation. There are no required
fields left: every one has a working default, so an install with no ``.env`` at
all starts and runs.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application configuration.

    Field names map case-insensitively to the env keys documented in
    ``.env.example`` (e.g. field ``imap_host`` <- ``IMAP_HOST``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # The mailbox — host, user, folder, SSL, poll interval, lookback — is
    # per-deployment and now lives in a project's [mailbox] table. Its password
    # lives in the OS credential store, reachable only through
    # `ProjectContext.imap_password()`, which still returns a SecretStr: the
    # structural defence against a repr or traceback leak moved with the value
    # rather than being dropped with the field.

    # --- Crop path: independent detector + spatial match (Phase 2c) ---
    # RT-DETR is Apache-2.0 and runs through transformers (Apache-2.0). MegaDetector
    # is deliberately not wired: every shipped inference path for it installs an
    # AGPL-3.0 dependency and this project serves a web UI (LICENSING_REVIEW.md §4).
    detector_model: str = Field(
        "PekingU/rtdetr_r50vd_coco_o365",
        description="Independent detector checkpoint. Runs on the CLEAN frame and "
        "never sees a label; its own class names are recorded but never filtered on.",
    )
    detector_device: str = Field("cpu", description="Torch device for the detector.")
    detector_score_threshold: float = Field(
        0.30, ge=0.0, le=1.0,
        description="Phase 2a operating point: 97.0% recall at IoU 0.5 while cutting "
        "candidates per frame from 26.5 to 4.9, with no event left without one.",
    )
    crop_iou_tau: float = Field(
        0.30, ge=0.0, le=1.0,
        description="Separates cropped_verified from cropped_disputed. Derived in "
        "Phase 2a from the valley in the IoU distribution; any value in [0.20, 0.45] "
        "changes at most four of 819 verdicts.",
    )
    crop_pad_frac: float = Field(
        0.15, ge=0.0, le=1.0,
        description="Context margin added around the detector's box before cropping.",
    )

    # --- BioCLIP taxonomic cross-check (Stage 3) ---
    bioclip_model: str = Field(
        "hf-hub:imageomics/bioclip",
        description="BioCLIP checkpoint identifier (pybioclip 2.x also ships 'bioclip-2').",
    )
    bioclip_device: str = Field("cpu", description="Torch device for BioCLIP (cpu|cuda).")
    bioclip_topk: int = Field(5, ge=1, description="Number of top taxa BioCLIP returns.")
    agreement_topk_join: bool = Field(
        False,
        description="If True, agreement is 'agree' when the upstream binomial appears "
        "ANYWHERE in BioCLIP's top-k (laxer). Default False = the honest top-1 join; "
        "top-k is provided only to measure both strategies for evaluation.",
    )

    # --- Ollama VLM + report LLM (Stage 3/4) ---
    ollama_endpoint: str = Field(
        "http://localhost:11434", description="Ollama HTTP endpoint."
    )
    ollama_model: str = Field(
        # Matches .env.example AND the model every published evaluation figure was
        # produced with. A different code default meant a user relying on defaults
        # silently ran a different model from the one the findings describe.
        "llava:latest", description="Vision-capable Ollama model name."
    )
    ollama_timeout_seconds: float = Field(
        60.0, gt=0, description="Per-request timeout for Ollama calls."
    )
    vlm_fallback_to_original_on_missing_boxed: bool = Field(
        False,
        description="If True, the VLM describes the original when the boxed image is "
        "absent (with a provenance note); default False records the failure instead.",
    )

    # The database, the image store and the species tables are per-deployment and
    # live under a project's directory; the site name and the camera's
    # coordinates live in its [site] table. Coordinates still have no default
    # anywhere: `ttr backfill-weather` refuses to run without them, per project,
    # because weather matched to somewhere the camera is not would be fabricated
    # context.

    # --- Weather API access (the SERVICE, not the site) ---
    weather_endpoint: str = Field(
        "https://archive-api.open-meteo.com/v1/archive",
        description="Open-Meteo historical (archive) API endpoint.",
    )
    weather_timeout_seconds: float = Field(20.0, gt=0, description="Per-request weather API timeout.")
    weather_small_sample_threshold: int = Field(
        20, ge=1,
        description="A per-species weather sample below this N is flagged as too small "
        "to support a weather-preference claim.",
    )

    # --- PDF rendering ---
    # Machine-level, not deployment-level: which browser binary this host has.
    # Previously readable ONLY as a bare environment variable, which meant
    # .env.example was not the full key list the README claims. The bare
    # TTR_BROWSER_EXECUTABLE / CHROME_PATH variables still work as a fallback.
    browser_executable: Optional[Path] = Field(
        None,
        description="Full path to a Chrome/Edge executable for PDF rendering. "
                    "Auto-discovered when unset.",
    )

    # --- Report presentation ---
    report_include_crosscheck: bool = Field(
        True,
        description="If False, the rendered report omits the BioCLIP agreement/cross-check "
        "flags (PRESENTATION ONLY — the agreement data is still ingested, stored, and read; "
        "no enrichment, schema, or read-path change; both variants render from the same DB).",
    )

    # --- Report thresholds ---
    confidence_low_flag_threshold: float = Field(
        0.5,
        ge=0.0,
        le=1.0,
        description="Upstream confidence at/below which a report flags 'low confidence'.",
    )


#: Env keys this class used to own, with what replaced each. Kept so a user whose
#: `.env` predates the project layer is TOLD their mailbox settings stopped being
#: read, rather than discovering it when a run finds no mail.
REMOVED_ENV_KEYS = {
    "IMAP_HOST": "a project's [mailbox] imap_host",
    "IMAP_USER": "a project's [mailbox] imap_user",
    "IMAP_PASSWORD": "the OS credential store (ttr project set-password)",
    "IMAP_FOLDER": "a project's [mailbox] imap_folder",
    "IMAP_USE_SSL": "a project's [mailbox] imap_use_ssl",
    "POLL_SECONDS": "a project's [mailbox] poll_seconds",
    "IMAP_LOOKBACK_DAYS": "a project's [mailbox] imap_lookback_days",
    "DB_PATH": "each project's own detections.sqlite",
    "IMAGE_STORE_DIR": "each project's own images/",
    "SPECIES_ALIAS_PATH": "a project's [species] alias_path",
    "TARGET_TAXONOMY_PATH": "a project's [species] taxonomy_path",
    "SITE_NAME": "a project's [site] name (ttr project set-site)",
    "WEATHER_LATITUDE": "a project's [site] latitude (ttr project set-site)",
    "WEATHER_LONGITUDE": "a project's [site] longitude (ttr project set-site)",
}

_NOTICE_EMITTED = False


def _env_file_keys() -> set[str]:
    """Bare KEY= names in the configured ``.env``, if one is configured at all.

    Read directly rather than through pydantic, because ``extra="ignore"`` is
    exactly what makes these keys invisible. Tests disable ``env_file``, so this
    reads nothing there.
    """
    env_file = Settings.model_config.get("env_file")
    if not env_file:
        return set()
    path = Path(env_file)
    if not path.is_file():
        return set()
    keys = set()
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            keys.add(line.split("=", 1)[0].strip().upper())
    except OSError:
        return set()
    return keys


def warn_about_removed_keys(stream=None) -> list[str]:
    """Say once, loudly, that a removed key is being ignored.

    ``extra="ignore"`` means an old ``.env`` still loads cleanly — and its
    mailbox settings are silently disregarded. Silence is the wrong failure here:
    someone's IMAP configuration ceasing to be read without a word is precisely
    the class of thing this refactor exists to remove. Returns the keys found, so
    it is testable without capturing output.
    """
    global _NOTICE_EMITTED
    present = sorted(
        (set(os.environ) | _env_file_keys()) & set(REMOVED_ENV_KEYS)
    )
    if not present or _NOTICE_EMITTED:
        return present
    _NOTICE_EMITTED = True

    out = stream or sys.stderr
    print("", file=out)
    print("NOTE: these settings are no longer read, and are being ignored:", file=out)
    for key in present:
        print(f"  {key:<22} -> now {REMOVED_ENV_KEYS[key]}", file=out)
    print("", file=out)
    print("They describe a DEPLOYMENT, and a deployment is now a project: its own", file=out)
    print("mailbox, database, image store and site. Set one up with:", file=out)
    print("    ttr project create        # name, alert inbox, app password", file=out)
    print("    ttr project set-site <p> --latitude <lat> --longitude <lon>", file=out)
    print("", file=out)
    print("Nothing was lost — the old values are still in your .env, just unused.", file=out)
    print("Delete them once the project is created.", file=out)
    print("", file=out)
    return present


#: One instance per process, and now that is simply CORRECT rather than tolerated.
#: While `Settings` carried per-deployment values, a cache of one was a compromise
#: — it meant a process could hold exactly one deployment's database path, which is
#: what made multi-project support a refactor rather than a feature. This class now
#: holds only machine-level configuration, which genuinely is the same for every
#: project in the process, so caching one is the right shape and not a bug waiting
#: to be found. Project state is deliberately NOT cached here: it is resolved
#: per command and per request, from the URL or the `--project` flag.
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache the machine-level settings.

    Every field has a default, so this cannot fail for want of configuration.
    Call :func:`get_settings.cache_clear` in tests that manipulate the
    environment.
    """
    settings = Settings()
    warn_about_removed_keys()
    return settings
