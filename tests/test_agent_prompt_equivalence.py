"""Prompt-equivalence regression: the effective model input must not drift.

``tests/fixtures/agent_prompt_baseline.json`` is a golden capture of every string
this system actually hands to a language model, taken from the code as it stood
BEFORE the Markdown agent-definition refactor. These tests replay the same real
code paths and assert byte equality against that capture.

The point is narrow and deliberate: moving an instruction from a Python constant
into a version-controlled ``.md`` file is an ARCHITECTURE change, so the bytes the
model receives must be identical either side of it. A failure here means model
input changed — which may be intended, but is never incidental, and must be
accompanied by a regenerated baseline in the same commit.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ttr.agents.report import ReportGeneratorAgent
from ttr.agents.window import TimeWindow

_BASELINE_PATH = Path(__file__).parent / "fixtures" / "agent_prompt_baseline.json"


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


class _Recorder:
    """Captures every (prompt, system) pair handed to the model."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def generate(self, prompt, *, system=None, images=None):
        self.calls.append({"prompt": prompt, "system": system})
        return self.replies.pop(0)


# The same fixed scenario the baseline was captured from. Held here rather than in
# a shared fixture so the replay cannot drift with unrelated test refactoring.
_WINDOW = TimeWindow(
    start_utc=datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc),
    end_utc=datetime(2026, 7, 3, 23, 59, 59, tzinfo=timezone.utc),
)
_BY_DAY = Counter({"2026-06-27": 5, "2026-06-28": 0, "2026-06-29": 12,
                   "2026-06-30": 3, "2026-07-01": 0, "2026-07-02": 8,
                   "2026-07-03": 1})
_GAP = (datetime(2026, 6, 30, 2, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 30, 20, 0, tzinfo=timezone.utc), 64800.0)


def _run_narrative(gaps):
    rec = _Recorder(["Twelve alert events occurred, peaking on 29 June."])
    agent = ReportGeneratorAgent(retrieval=None, llm=rec)
    agent._narrative("common wood pigeon", "Columba palumbus", _WINDOW,
                     total=29, dated=29, undated=0, by_day=_BY_DAY, gaps=gaps)
    return rec.calls[0]


def _assert_matches(baseline: dict, key: str, actual: str) -> None:
    expected = baseline["values"][key]
    assert actual == expected, f"effective model input changed for {key!r}"
    assert hashlib.sha256(actual.encode("utf-8")).hexdigest() == baseline["sha256"][key]


# ------------------------------------------------------------ narrative agent
def test_narrative_system_prompt_unchanged(baseline):
    call = _run_narrative([])
    _assert_matches(baseline, "narrative.system.base", call["system"])
    _assert_matches(baseline, "narrative.system.no_outage", call["system"])


def test_narrative_facts_message_unchanged(baseline):
    """The user message and the ORDER of the facts inside it."""
    _assert_matches(baseline, "narrative.facts.no_outage", _run_narrative([])["prompt"])


def test_narrative_outage_variant_unchanged(baseline):
    call = _run_narrative([_GAP])
    _assert_matches(baseline, "narrative.system.outage", call["system"])
    _assert_matches(baseline, "narrative.facts.outage", call["prompt"])
    # the static outage instruction still sits inside the deterministic facts
    assert baseline["values"]["narrative.facts.outage_line"] in call["prompt"]


def test_narrative_rejection_retry_prompt_unchanged(baseline):
    """The vocabulary gate's retry instruction, as assembled for attempt 2."""
    rec = _Recorder(["There were 12 sightings.", "There were 12 alert events."])
    agent = ReportGeneratorAgent(retrieval=None, llm=rec)
    agent._llm_narrative_body("FACTS", baseline["values"]["narrative.system.base"])
    assert len(rec.calls) == 2, "the rejected attempt must be retried exactly once here"
    _assert_matches(baseline, "narrative.system.rejection_attempt2", rec.calls[1]["system"])


# ----------------------------------------------------------- VLM describer agent
class _FakeResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"response": "a description"}


class _FakeClient:
    def __init__(self):
        self.url = None
        self.payload = None

    def post(self, url, json=None):
        self.url = url
        self.payload = dict(json)
        return _FakeResp()


def _run_describe():
    from ttr.enrichment.ollama_enricher import OllamaEnricher

    fc = _FakeClient()
    OllamaEnricher(model="llama3.2-vision", client=fc).describe(b"\xff\xd8imagebytes")
    return fc


def test_vlm_prompts_unchanged(baseline):
    fc = _run_describe()
    _assert_matches(baseline, "vlm.system", fc.payload["system"])
    _assert_matches(baseline, "vlm.user", fc.payload["prompt"])


def test_vlm_request_shape_unchanged(baseline):
    """Model config and request composition — no temperature, no tools, no schema."""
    fc = _run_describe()
    expected = baseline["vlm_request"]
    assert fc.url.endswith(expected["url_suffix"][expected["url_suffix"].rfind("/api"):])
    assert list(fc.payload.keys()) == expected["payload_key_order"]
    assert fc.payload["model"] == expected["payload"]["model"]
    assert fc.payload["stream"] is expected["payload"]["stream"]
    # nothing new may appear in the request
    assert set(fc.payload) == {"model", "system", "prompt", "images", "stream"}


def test_baseline_fixture_is_self_consistent(baseline):
    """Guards the guard: the committed hashes must match the committed strings."""
    for key, text in baseline["values"].items():
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == baseline["sha256"][key], key
