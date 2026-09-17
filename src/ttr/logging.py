"""Structured logging setup.

A single :func:`configure_logging` call installs a console handler that emits
one line per record with stable, machine-greppable ``key=value`` fields plus
any structured ``extra`` passed at the call site. Every stage logs through the
standard library logger obtained via :func:`get_logger` (guardrail §8: log at
every stage — source poll, parse warnings, each enricher outcome, each persist,
each report request).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_CONFIGURED = False

# Attributes present on every LogRecord; anything else in __dict__ is treated as
# a structured extra and appended as key=value.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime"}


class _KeyValueFormatter(logging.Formatter):
    """Render ``ts=... level=... logger=... msg="..." k=v ...``."""

    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f'ts={self.formatTime(record)} '
            f'level={record.levelname} '
            f'logger={record.name} '
            f'msg={self._quote(record.getMessage())}'
        )
        extras = [
            f"{k}={self._quote(v)}"
            for k, v in record.__dict__.items()
            if k not in _RESERVED
        ]
        line = " ".join([base, *extras])
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line

    @staticmethod
    def _quote(value: Any) -> str:
        text = str(value)
        if any(c in text for c in ' ="\n'):
            escaped = text.replace('\\', '\\\\').replace('"', '\\"')
            return f'"{escaped}"'
        return text


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install the structured handler on the root logger (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_KeyValueFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


def key_value_formatter() -> logging.Formatter:
    """A fresh instance of the structured ``ts=... level=... msg=... k=v`` formatter.

    Exposed so other surfaces — the web ingest console — render log lines in
    EXACTLY the format the CLI prints, rather than restating the format and
    letting the two drift apart.
    """
    return _KeyValueFormatter()


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger (call :func:`configure_logging` first)."""
    return logging.getLogger(name)
