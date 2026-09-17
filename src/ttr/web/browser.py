"""Locate an installed Chrome or Edge executable for headless PDF rendering.

A small, self-contained, independently-tested utility — deliberately NOT mixed
into the report endpoint. Discovery order:

1. an explicit override (argument, then the ``TTR_BROWSER_EXECUTABLE`` /
   ``CHROME_PATH`` environment variables),
2. the usual per-platform install locations for Chrome, then Edge,
3. the Windows ``App Paths`` registry,
4. anything named like a browser on ``PATH``.

Raises :class:`BrowserNotFound` with an actionable message when nothing is found.
Only the plain executable path is returned; the PDF adapter owns how it is driven.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: Environment variables that override discovery (checked in this order).
ENV_OVERRIDES = ("TTR_BROWSER_EXECUTABLE", "CHROME_PATH")


class BrowserNotFound(RuntimeError):
    """No Chrome/Edge executable could be located for PDF rendering."""


def _install_paths() -> list[Path]:
    """Common install locations for Chrome (preferred) then Edge, per platform."""
    out: list[Path] = []
    if sys.platform == "win32":
        roots = [os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                 os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        rels = (r"Google\Chrome\Application\chrome.exe",
                r"Microsoft\Edge\Application\msedge.exe")
        for root in roots:
            if root:
                out.extend(Path(root) / rel for rel in rels)
    elif sys.platform == "darwin":
        out += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    else:  # linux / other posix
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "microsoft-edge", "microsoft-edge-stable"):
            found = shutil.which(name)
            if found:
                out.append(Path(found))
    return out


def _registry_paths() -> list[Path]:
    """Windows ``App Paths`` registry entries for chrome.exe / msedge.exe."""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return []
    out: list[Path] = []
    for exe in ("chrome.exe", "msedge.exe"):
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    hive,
                    rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}",
                ) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                if value:
                    out.append(Path(value))
            except OSError:
                continue
    return out


def _path_lookups() -> list[Path]:
    out: list[Path] = []
    for name in ("chrome", "google-chrome", "chromium", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            out.append(Path(found))
    return out


def candidate_browsers(override: str | None = None) -> list[Path]:
    """The ordered candidate list discovery will consider (before existence checks).

    Exposed separately so it is unit-testable without a browser installed.
    """
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    for var in ENV_OVERRIDES:
        value = os.environ.get(var)
        if value:
            candidates.append(Path(value))
    candidates += _install_paths()
    candidates += _registry_paths()
    candidates += _path_lookups()
    return candidates


def find_browser(override: str | None = None) -> Path:
    """Return the path to a usable Chrome/Edge executable, or raise BrowserNotFound."""
    seen: set[str] = set()
    for cand in candidate_browsers(override):
        key = str(cand).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    raise BrowserNotFound(
        "No Chrome or Edge executable was found for PDF rendering. Install Google "
        "Chrome or Microsoft Edge, or set the TTR_BROWSER_EXECUTABLE environment "
        "variable to the full path of a Chrome/Edge executable."
    )
