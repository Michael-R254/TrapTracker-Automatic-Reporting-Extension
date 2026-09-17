"""Configuration must not drift between the model, the documentation and the code.

Every check here guards a defect that was actually found, not a hypothetical one:

* `.env.example` claimed to be "the full key list" while `Settings` had grown
  past it;
* the Ollama model default in code disagreed with both `.env.example` and the
  model every published evaluation figure was produced with;
* the browser executable was readable ONLY as a bare environment variable, so it
  was invisible to anyone reading the documented configuration.

These are cheap to assert and expensive to notice by hand, so they are pinned.
"""

from __future__ import annotations

import re
from pathlib import Path

from ttr.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: `KEY=` at the start of a line, optionally commented out (an optional setting is
#: still documented, just not enabled by default).
_ENV_KEY = re.compile(r"^#?\s*([A-Z][A-Z0-9_]+)=", re.M)


def _documented_keys() -> set[str]:
    return set(_ENV_KEY.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def _settings_keys() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_every_setting_is_documented_in_env_example():
    """The README points at `.env.example` for the full key list. It must be full."""
    missing = sorted(_settings_keys() - _documented_keys())
    assert missing == [], (
        f"settings absent from .env.example: {missing} — a user reading the "
        "documented configuration cannot discover these")


def test_env_example_documents_nothing_that_is_not_a_setting():
    """The reverse drift: a key that no longer exists silently does nothing."""
    stale = sorted(_documented_keys() - _settings_keys())
    assert stale == [], f".env.example documents non-existent settings: {stale}"


def test_env_example_values_parse_into_settings(monkeypatch):
    """Copying .env.example verbatim must produce a valid configuration.

    The documented file is the first thing a new user copies, so a value that
    fails validation is a broken first run.
    """
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        monkeypatch.setenv(key.strip(), value.strip())

    settings = Settings(_env_file=None)          # env only, not the developer's .env
    assert settings.ollama_model
    assert settings.bioclip_model
    assert settings.weather_endpoint


def test_ollama_model_default_matches_the_documented_default():
    """A code default that disagrees with `.env.example` means two users running
    'the defaults' run different models — and the published VLM findings are
    specific to one of them."""
    documented = re.search(r"^OLLAMA_MODEL=(.+)$",
                           ENV_EXAMPLE.read_text(encoding="utf-8"), re.M)
    assert documented, "OLLAMA_MODEL is not documented in .env.example"
    assert Settings.model_fields["ollama_model"].default == documented.group(1).strip()


def test_browser_executable_is_a_setting_not_only_an_env_var():
    """It was readable only via os.environ, so it never appeared in the documented
    configuration surface. The bare env vars remain as a fallback."""
    assert "browser_executable" in Settings.model_fields
    assert Settings.model_fields["browser_executable"].default is None

    from ttr.web.browser import ENV_OVERRIDES

    assert "TTR_BROWSER_EXECUTABLE" in ENV_OVERRIDES   # fallback still supported


def test_settings_env_keys_helper_tracks_the_model():
    """conftest derives the key list from the model rather than restating it; a
    restated list had already drifted to 13 of 23 keys.

    It now also carries the REMOVED keys. Those are no longer read, but they DO
    trigger the deprecation notice, so a developer with a real IMAP_HOST exported
    would otherwise see it surface inside unrelated tests."""
    from ttr.config import REMOVED_ENV_KEYS

    from conftest import _settings_env_keys

    assert set(_settings_env_keys()) == _settings_keys() | set(REMOVED_ENV_KEYS)


def test_env_example_documents_no_removed_key():
    """The file a new user copies must not still offer settings that are ignored."""
    from ttr.config import REMOVED_ENV_KEYS

    stale = sorted(_documented_keys() & set(REMOVED_ENV_KEYS))
    assert stale == [], (
        f".env.example still documents keys that are no longer read: {stale}")


# --------------------------------------------------------------------------- #
# Command output stays ASCII
# --------------------------------------------------------------------------- #
#: Modules whose string literals reach a terminal. Report prose is deliberately
#: NOT here: a report is a document — written to a file, rendered in the web UI
#: and set in a PDF — and flattening its typography to suit a console would
#: damage the deliverable to fix the wrong surface.
_CLI_MODULES = ("cli.py", "cli_project.py", "storage/migrations.py",
                "projects/*.py")


def test_command_output_is_ascii():
    """A console that cannot encode an em-dash prints a replacement box instead,
    and CLI output is a surface people screenshot and paste into a write-up.

    Comments are exempt: they are read in an editor, where typography is free.
    The check therefore looks at string literals only — and at f-strings too,
    which Python 3.12 tokenises as FSTRING_* rather than STRING, which is exactly
    how the first pass at this missed half of them.
    """
    import io
    import tokenize

    src_root = Path(__file__).resolve().parents[1] / "src" / "ttr"
    files: list[Path] = []
    for pattern in _CLI_MODULES:
        files.extend(sorted(src_root.glob(pattern)))
    assert files, "no CLI modules matched"

    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        comment_spans: dict[int, list[tuple[int, int]]] = {}
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comment_spans.setdefault(tok.start[0], []).append(
                    (tok.start[1], tok.end[1]))
        for row, line in enumerate(text.splitlines(), start=1):
            spans = comment_spans.get(row, [])
            for col, ch in enumerate(line):
                if ord(ch) > 127 and not any(a <= col < b for a, b in spans):
                    offenders.append(
                        f"{path.name}:{row}: U+{ord(ch):04X} {ch!r} in {line.strip()[:70]}")

    assert not offenders, (
        "non-ASCII in command output:\n  " + "\n  ".join(offenders[:15]))
