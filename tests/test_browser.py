"""Browser-discovery utility (headless-PDF support).

Pure-logic tests — no browser required in CI. They exercise the ordering
(override → env → install paths → registry → PATH), the existence filter, and the
clear BrowserNotFound error, by controlling the candidate list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ttr.web import browser
from ttr.web.browser import BrowserNotFound, candidate_browsers, find_browser


def test_explicit_override_is_first_candidate():
    cands = candidate_browsers(override=r"C:\custom\chrome.exe")
    assert cands[0] == Path(r"C:\custom\chrome.exe")


def test_env_override_is_considered(monkeypatch):
    monkeypatch.setenv("TTR_BROWSER_EXECUTABLE", r"C:\env\edge.exe")
    assert Path(r"C:\env\edge.exe") in candidate_browsers()


def test_find_browser_returns_an_existing_override(tmp_path):
    fake = tmp_path / "chrome.exe"
    fake.write_bytes(b"")                       # a real file on disk
    assert find_browser(override=str(fake)) == fake


def test_find_browser_skips_nonexistent_and_raises_when_none(monkeypatch):
    # No candidate exists on disk -> a clear, actionable error.
    monkeypatch.setattr(browser, "candidate_browsers",
                        lambda override=None: [Path(r"C:\nope\chrome.exe")])
    with pytest.raises(BrowserNotFound) as exc:
        find_browser()
    assert "TTR_BROWSER_EXECUTABLE" in str(exc.value)


def test_find_browser_returns_first_existing(monkeypatch, tmp_path):
    missing = tmp_path / "missing.exe"
    present = tmp_path / "present.exe"
    present.write_bytes(b"")
    monkeypatch.setattr(browser, "candidate_browsers",
                        lambda override=None: [missing, present])
    assert find_browser() == present
