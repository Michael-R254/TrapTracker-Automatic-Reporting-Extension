"""Minimal TOML emitter, plus the stdlib reader.

Reading is ``tomllib`` (stdlib since 3.11, and this project's floor is 3.11).
Writing has no stdlib equivalent, so it is done here by hand rather than by
adding a dependency for the handful of shapes these files use: a schema version,
a few string/bool/int/float keys, and four fixed tables. Anything richer is
deliberately not supported - if a future field needs an array of tables, that is
the moment to reconsider the dependency, not a reason to grow this.

Every string is emitted as a TOML basic string with full escaping, so a display
name containing a quote, a backslash or a newline round-trips exactly.
"""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import RegistryError

_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t",
            "\b": "\\b", "\f": "\\f"}


def dump_string(value: str) -> str:
    """A TOML basic string, escaped."""
    out = []
    for ch in value:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def dump_value(value: Any) -> str:
    """One scalar. Bools before ints - ``bool`` is a subclass of ``int``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise RegistryError(f"cannot write non-finite float: {value!r}")
        return repr(value)
    if isinstance(value, str):
        return dump_string(value)
    raise RegistryError(f"unsupported TOML value type: {type(value).__name__}")


def render(sections: Iterable[tuple],
           *, header: Iterable[str] = ()) -> str:
    """Render ``[(table_name_or_None, {key: value}[, comments]), ...]`` as TOML.

    A ``None`` table name emits bare top-level keys, which TOML requires to come
    before any table header. Keys whose value is ``None`` are OMITTED, not
    written as an empty string: an absent key and a key set to "" mean different
    things here (unset coordinates versus a blank one), and the readers
    distinguish them.

    The optional third element is a list of comment lines appended after the
    table's keys. It exists because omitting a ``None`` key makes that setting
    correct but INVISIBLE - a reader of the file cannot discover a knob that is
    not written down. This is the same failure `test_config_consistency` guards
    for `.env.example`: documentation that silently stops being complete. So an
    unset-but-supported key is emitted as a commented example instead of
    vanishing.
    """
    lines: list[str] = []
    for comment in header:
        lines.append(f"# {comment}" if comment else "#")
    if lines:
        lines.append("")
    for section in sections:
        table, body = section[0], section[1]
        comments = section[2] if len(section) > 2 else ()
        pairs = [(k, v) for k, v in body.items() if v is not None]
        if table is not None:
            lines.append(f"[{table}]")
        for key, value in pairs:
            lines.append(f"{key} = {dump_value(value)}")
        for comment in comments:
            lines.append(f"# {comment}" if comment else "#")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def load(path: Path) -> dict:
    """Parse a TOML file, turning both failure modes into one readable error."""
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise RegistryError(f"missing file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{path} is not valid TOML: {exc}") from exc


def write_atomic(path: Path, text: str) -> None:
    """Write via a same-directory temp file and ``os.replace``.

    ``os.replace`` is atomic on POSIX and on Windows, and the temp file is a
    sibling so the replace never crosses a filesystem boundary (where atomicity
    would be lost). The failure this prevents is a TORN file: a half-written
    registry makes every project unloadable, whereas a lost update to
    ``active_project`` costs one re-selection. Last-writer-wins is the accepted
    trade, not an oversight.

    The temp file is removed if anything fails before the replace, so a crashed
    write leaves the original in place and no debris beside it.
    """
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
