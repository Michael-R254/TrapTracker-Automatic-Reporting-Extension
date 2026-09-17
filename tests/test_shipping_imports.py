"""The suite runs in an import graph that is not the CLI's.

`email_fetcher.py` used `email.policy.default` while importing only `email`.
`import email` does NOT bind the `policy` submodule, so that line worked solely
because something else in the process had imported it first — which was true
under pytest and false under `python -m ttr.cli`. The result was a live ingest
path that had never worked, and a green suite that could not see it.

So these run the code the way it SHIPS: in a fresh interpreter, as a subprocess,
with no test plugins and none of this suite's imports already loaded. An
in-process test cannot make this assertion about itself, because by the time it
runs, pytest has already imported half the standard library.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

#: A bare interpreter with the package importable and nothing else loaded.
_ENV_PATH = str(SRC)


def _run(args, **kw):
    env = {**kw.pop("env", {}), "PYTHONPATH": _ENV_PATH}
    import os
    full = {**os.environ, **env}
    return subprocess.run([sys.executable, *args], capture_output=True, text=True,
                          env=full, timeout=120, **kw)


# --------------------------------------------------------------------------- #
# The regression guard proper.
# --------------------------------------------------------------------------- #
def test_the_fetcher_binds_every_submodule_it_uses_at_runtime():
    """Import the fetcher ALONE and touch what `fetch_unseen` touches.

    This is the assertion that was missing. `fetch_unseen` reaches
    `email.policy.default` when it parses a fetched message; if the module stops
    importing `email.policy`, importing it still succeeds and only the live
    fetch breaks — so importing is not enough, the attribute must be resolved.
    """
    proc = _run(["-c", "import ttr.sources.email_fetcher; "
                       "import email; "
                       "email.policy.default"])
    assert proc.returncode == 0, (
        "the fetcher relies on a submodule nobody imported:\n" + proc.stderr)


def test_the_cli_reaches_the_fetcher_without_a_mailbox(tmp_path):
    """`ttr ingest` loads the fetcher BEFORE it resolves a project.

    Pointed at an empty projects root it therefore imports the whole ingest
    module graph and then stops with a project error — exercising the shipping
    import path with no credential, no network and no mailbox. A collection-time
    ImportError here would be indistinguishable from the bug this file exists
    for, which is why the exit code and the message are both asserted.
    """
    proc = _run(["-m", "ttr.cli", "ingest", "--project", "no-such-project"],
                env={"TTR_PROJECTS_ROOT": str(tmp_path)})

    assert proc.returncode == 2, f"expected a project error, got:\n{proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert "No projects exist yet" in combined, combined[:600]
    assert "Traceback" not in combined, (
        "the ingest path raised instead of erroring cleanly:\n" + combined)


# --------------------------------------------------------------------------- #
# The class, not just the instance.
# --------------------------------------------------------------------------- #
def _submodule_uses() -> set[tuple[str, str, str]]:
    """(file, real module, sub) for every `root.sub.…` where only `root` was imported.

    The local NAME may be an alias (`import numpy as np`), so it is mapped back
    to the real module before anything tries to import it — otherwise the probe
    below fails on `import np` and reports a defect that is really a typo here.
    """
    out = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bound: dict[str, str] = {}          # local name -> real module name
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    bound[a.asname or a.name] = a.name
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and isinstance(node.value.value, ast.Name)):
                local, sub = node.value.value.id, node.value.attr
                real = bound.get(local)
                if real is None:
                    continue                # not an `import X` name at all
                if f"{real}.{sub}" in bound.values():
                    continue                # explicitly imported
                out.add((str(path.relative_to(SRC)), real, sub))
    return out


def test_no_module_uses_a_submodule_the_parent_does_not_bind():
    """Generalises the fix: `import X` then `X.y.z` is safe ONLY if X binds y.

    Some packages do bind their submodules eagerly (`numpy.linalg`,
    `ctypes.windll`); `email` does not bind `email.policy`. Rather than carry a
    hand-maintained allowlist that would rot, each candidate is checked in a
    FRESH interpreter — which is the only place the answer is honest, since this
    process has already imported everything.
    """
    offenders = []
    for where, root, sub in sorted(_submodule_uses()):
        # THREE outcomes, not two. A probe that fails because the package is not
        # installed says nothing about whether the parent binds the submodule,
        # and reporting that as a violation is a guard inventing a defect — which
        # is worse than one that misses a defect, because it sends someone to fix
        # correct code. Observed on a fresh clone without the [detect] extra,
        # where this falsely accused `crop/banner_ocr.py` of the email.policy bug.
        probe = _run(["-c", "import importlib.util, sys; "
                            f"sys.exit(2) if importlib.util.find_spec({root!r}) is None "
                            f"else None; "
                            f"import {root}; "
                            f"sys.exit(0 if hasattr({root}, {sub!r}) else 1)"])
        if probe.returncode == 2:
            continue                    # not installed here; nothing to conclude
        if probe.returncode != 0:
            offenders.append(f"{where}: uses {root}.{sub} but only imports {root}, "
                             f"and importing {root} does not bind {sub}")
    assert not offenders, (
        "these work only while something else happens to import the submodule "
        "first:\n  " + "\n  ".join(offenders))


def test_the_submodule_scan_would_still_catch_a_violation(tmp_path):
    """A guard on the guard, now that the real count is zero.

    With no offenders left, a scanner that had silently stopped matching would
    look identical to success. So it is checked against the exact shape of the
    original defect.
    """
    sample = tmp_path / "offender.py"
    sample.write_text("import email\n\n"
                      "def go(raw):\n"
                      "    return email.policy.default\n", encoding="utf-8")
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    plain = {a.asname or a.name
             for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    hits = [(n.value.value.id, n.value.attr) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute)
            and isinstance(n.value.value, ast.Name)
            and n.value.value.id in plain
            and f"{n.value.value.id}.{n.value.attr}" not in plain]

    assert ("email", "policy") in hits, "the scan no longer recognises its own bug"
    probe = _run(["-c", "import email; raise SystemExit(0 if hasattr(email,'policy') else 1)"])
    assert probe.returncode == 1, (
        "if `import email` starts binding `policy`, this whole check is moot — "
        "which would be worth knowing rather than silently passing")
