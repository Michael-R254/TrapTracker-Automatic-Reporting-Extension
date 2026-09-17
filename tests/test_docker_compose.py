"""What the container setup promises, asserted rather than trusted.

These are cheap file-level checks, and each one stands for a decision that is
easy to undo by accident:

* the compose file must pass EVERY machine setting, so adding a `Settings` field
  cannot silently become unreachable in a container - the same reason
  `test_config_consistency` holds `.env.example` to the model;
* it must not give those settings a second set of defaults to drift from;
* the UI must stay on loopback, and on one port number (the PDF route dials the
  port the request arrived on);
* the ingest loop must stay opt-in and bounded;
* the build context must stay an allowlist, and the image must not run as root.

Nothing here runs Docker. The behaviour these files produce was measured
separately, by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from ttr.config import Settings

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker" / "compose.yaml"
DOCKERFILE = REPO / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"

#: The port spec is interpolated by compose, so both halves are matched as
#: either a literal or `${TTR_PORT:-8000}`.
PORT = r"(?:\$\{TTR_PORT:-\d+\}|\d+)"
LOOPBACK_PUBLISH = re.compile(rf"^127\.0\.0\.1:({PORT}):({PORT})$")


@pytest.fixture(scope="module")
def compose() -> dict:
    # safe_load resolves the `<<:` merge keys, so each service dict already
    # carries the shared definition.
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _env(service: dict) -> dict:
    return service["environment"]


# --------------------------------------------------------------------------- #
# Settings reach the container, and keep their only home.
# --------------------------------------------------------------------------- #
def test_every_machine_setting_is_passed_through(compose):
    passed = set(_env(compose["services"]["ttr"]))
    missing = sorted(name.upper() for name in Settings.model_fields
                     if name.upper() not in passed)

    assert missing == [], (
        f"docker/compose.yaml passes no {missing} - a setting `Settings` "
        f"declares cannot be set in a container")


def test_the_settings_are_not_given_a_second_set_of_defaults(compose):
    """Unset must mean the app's own default, not a value copied in here.

    OLLAMA_ENDPOINT is the single exception, and it is one because the app's
    default (`localhost`) means the container itself.
    """
    valued = {key for key, value in _env(compose["services"]["ttr"]).items()
              if value is not None}

    assert valued == {"OLLAMA_ENDPOINT"}


def test_no_credential_is_written_into_the_file(compose):
    for key, value in _env(compose["services"]["ttr"]).items():
        if key.startswith("TTR_IMAP_PASSWORD") or key == "TTR_UI_TOKEN":
            assert value is None, f"{key} carries a value in the compose file"


def test_the_ui_token_can_be_pinned_from_the_shell(compose):
    assert "TTR_UI_TOKEN" in _env(compose["services"]["ttr"])


# --------------------------------------------------------------------------- #
# Exposure.
# --------------------------------------------------------------------------- #
def test_the_ui_is_published_on_loopback_and_on_one_port_number(compose):
    published = compose["services"]["ttr"]["ports"]

    assert published, "the web UI publishes no port"
    for spec in published:
        match = LOOPBACK_PUBLISH.match(str(spec))
        assert match, f"{spec!r} is not a 127.0.0.1 publish of one port"
        assert match.group(1) == match.group(2), (
            f"{spec!r} maps different host and container ports; the PDF route "
            f"dials the port the request arrived on")


def test_no_service_publishes_beyond_loopback(compose):
    for name, service in compose["services"].items():
        for spec in service.get("ports", []):
            assert str(spec).startswith("127.0.0.1:"), (
                f"service {name!r} publishes {spec!r} beyond loopback")


def test_there_is_no_compose_file_at_the_repository_root():
    """At the root, compose would read the app's own `.env` - whose values
    describe the host, and which is exactly where a password must not be read
    from."""
    for name in ("compose.yaml", "compose.yml",
                 "docker-compose.yaml", "docker-compose.yml"):
        assert not (REPO / name).exists(), f"{name} must not sit at the root"


# --------------------------------------------------------------------------- #
# The ingest service.
# --------------------------------------------------------------------------- #
def test_the_ingest_loop_is_opt_in(compose):
    ingest = compose["services"]["ingest"]

    assert ingest["profiles"] == ["ingest"], (
        "a plain `up` must not start an ingest loop: without a credential in the "
        "environment it would only fail")


def test_the_ingest_restart_policy_is_bounded(compose):
    """An unbounded loop re-offers a revoked password to Gmail indefinitely."""
    policy = str(compose["services"]["ingest"]["restart"])

    assert policy.startswith("on-failure")
    assert policy != "on-failure", f"{policy!r} sets no maximum"


def test_the_ingest_service_resolves_its_project_the_apps_own_way(compose):
    """No --project in the file: TTR_PROJECT, the active project, or the only
    one - the same rules every command follows."""
    ingest = compose["services"]["ingest"]

    assert ingest["command"] == ["run"]
    assert "TTR_PROJECT" in _env(ingest)


def test_the_ingest_service_cannot_drift_from_the_web_service(compose):
    web, ingest = compose["services"]["ttr"], compose["services"]["ingest"]

    assert ingest["image"] == web["image"]
    assert ingest["volumes"] == web["volumes"]
    assert _env(ingest) == _env(web)


# --------------------------------------------------------------------------- #
# The image and its build context.
# --------------------------------------------------------------------------- #
def test_the_image_does_not_run_as_root():
    users = [line.split()[1] for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
             if line.startswith("USER ")]

    assert users and users[-1] == "ttr"


def test_the_build_context_is_an_allowlist():
    """This checkout holds a .env, project databases, camera imagery and
    dissertation material. A denylist is one forgotten line from copying any of
    them into a layer, where a later delete does not remove it."""
    lines = [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.strip().startswith("#")]

    assert lines[0] == "*", "everything must be excluded before anything is named"
    assert {line[1:] for line in lines if line.startswith("!")} == {
        "pyproject.toml", "README.md", "LICENSE", "src/", "examples/",
        "docs/evaluation/data/",
    }
