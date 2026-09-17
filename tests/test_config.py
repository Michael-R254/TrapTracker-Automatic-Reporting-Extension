"""Machine-level configuration: what Settings still owns, and what it refuses
to own any more.

The per-deployment fields moved to projects. The password's SecretStr treatment
moved WITH the value, to `ProjectContext.imap_password()` — it was not dropped;
see test_credentials.py, which asserts the sentinel reaches no log record, no
repr and no file.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ttr.config import REMOVED_ENV_KEYS, Settings, get_settings


def test_loads_with_no_configuration_at_all(clean_env):
    """Every field has a default, so an install with no .env starts and runs.

    This is new: `Settings` used to raise for a missing IMAP password. A missing
    MAILBOX is still a hard failure — it is just a project's failure now, raised
    where the mailbox is actually used."""
    settings = get_settings()

    assert settings.ollama_endpoint == "http://localhost:11434"
    assert settings.bioclip_model == "hf-hub:imageomics/bioclip"
    assert settings.crop_iou_tau == 0.30
    assert settings.confidence_low_flag_threshold == 0.5


def test_env_overrides_defaults(clean_env):
    clean_env.setenv("POLL_SECONDS", "10")          # removed: must be ignored
    clean_env.setenv("OLLAMA_MODEL", "qwen2-vl")

    settings = Settings()
    assert settings.ollama_model == "qwen2-vl"
    assert not hasattr(settings, "poll_seconds")


def test_invalid_threshold_rejected(clean_env):
    clean_env.setenv("CONFIDENCE_LOW_FLAG_THRESHOLD", "1.5")  # out of [0, 1]

    with pytest.raises(ValidationError):
        Settings()


# --------------------------------------------------------------------------- #
# The per-deployment fields are GONE
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", sorted({
    "imap_host", "imap_user", "imap_password", "imap_folder", "imap_use_ssl",
    "poll_seconds", "imap_lookback_days",
    "db_path", "image_store_dir", "species_alias_path", "target_taxonomy_path",
    "site_name", "weather_latitude", "weather_longitude",
}))
def test_a_moved_field_is_not_on_settings(field):
    """Each of these describes a DEPLOYMENT. Holding one globally is how a
    command ends up writing to a database the user did not choose."""
    assert field not in Settings.model_fields


def test_settings_keeps_only_machine_level_fields():
    """Stated as a whole list, so ADDING a per-deployment field back is as
    visible as removing one."""
    assert set(Settings.model_fields) == {
        "detector_model", "detector_device", "detector_score_threshold",
        "crop_iou_tau", "crop_pad_frac",
        "bioclip_model", "bioclip_device", "bioclip_topk", "agreement_topk_join",
        "ollama_endpoint", "ollama_model", "ollama_timeout_seconds",
        "vlm_fallback_to_original_on_missing_boxed",
        "weather_endpoint", "weather_timeout_seconds",
        "weather_small_sample_threshold",
        "browser_executable", "report_include_crosscheck",
        "confidence_low_flag_threshold",
    }


def test_the_weather_service_stays_but_the_site_does_not():
    """A distinction worth pinning: HOW to reach Open-Meteo is the same wherever
    the camera is; WHERE the camera is, is not."""
    assert "weather_endpoint" in Settings.model_fields
    assert "weather_timeout_seconds" in Settings.model_fields
    assert "weather_latitude" not in Settings.model_fields
    assert "weather_longitude" not in Settings.model_fields


# --------------------------------------------------------------------------- #
# A removed key is reported, not silently ignored
# --------------------------------------------------------------------------- #
def test_a_stale_env_key_is_named_rather_than_silently_dropped(clean_env, capsys):
    """`extra="ignore"` means an old .env still loads cleanly and its mailbox
    settings are quietly disregarded. Someone's IMAP configuration ceasing to be
    read without a word is exactly what this refactor exists to remove."""
    import ttr.config as config

    clean_env.setenv("IMAP_HOST", "imap.gmail.com")
    clean_env.setenv("IMAP_PASSWORD", "abcdefghijklmnop")
    clean_env.setenv("DB_PATH", "data/old.db")
    clean_env.setenv("SITE_NAME", "Somewhere")
    config._NOTICE_EMITTED = False

    found = config.warn_about_removed_keys()
    err = capsys.readouterr().err

    assert found == ["DB_PATH", "IMAP_HOST", "IMAP_PASSWORD", "SITE_NAME"]
    for key in found:
        assert key in err
    # Each names its replacement, and the message names the fix.
    assert "ttr project create" in err
    assert "ttr project set-site" in err
    assert "credential store" in err        # where IMAP_PASSWORD went
    # The notice names the KEY, never the value it happens to hold.
    assert "abcdefghijklmnop" not in err
    # And says nothing was destroyed.
    assert "Nothing was lost" in err


def test_the_notice_is_emitted_once_per_process(clean_env, capsys):
    """Once, not per read: get_settings is called from every command handler."""
    import ttr.config as config

    clean_env.setenv("IMAP_HOST", "imap.gmail.com")
    config._NOTICE_EMITTED = False

    config.warn_about_removed_keys()
    first = capsys.readouterr().err
    config.warn_about_removed_keys()
    second = capsys.readouterr().err

    assert "IMAP_HOST" in first
    assert second == ""


def test_no_notice_when_nothing_stale_is_set(clean_env, capsys):
    import ttr.config as config

    config._NOTICE_EMITTED = False
    assert config.warn_about_removed_keys() == []
    assert capsys.readouterr().err == ""


def test_every_removed_field_has_a_documented_replacement():
    """The notice is only useful if it says where the value went."""
    assert len(REMOVED_ENV_KEYS) == 14
    for key, replacement in REMOVED_ENV_KEYS.items():
        assert key.isupper()
        assert replacement and not replacement.endswith(".")
