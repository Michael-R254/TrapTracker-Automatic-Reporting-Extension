"""`_CHART_TAGS` is the only policy permitting SVG. Guard what may reach it.

After Stage 3 the document policy renders no SVG at all, so every chart on the
page owes its existence to `_CHART_TAGS`. That policy is safe for exactly one
reason: it is applied to held-aside generator output and to nothing else. Applied
to document text — or to a model-authored string that reached the wrong variable
— it would hand back the permissive behaviour the whole sequence removed, and
every other test here would still pass.

That is a discipline, and this file turns it into an invariant. The check is
STRUCTURAL, in the spirit of `test_isolation.py`'s moved-setting scan: it reads
the source rather than the behaviour, because a behavioural assertion only covers
the paths someone thought to exercise, and the failure being guarded against is a
new call site nobody thought about at all.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "ttr"
APP = SRC / "web" / "app.py"

#: The privileged names. Any use outside the sanctioned call site is a finding.
PRIVILEGED = {"_CHART_TAGS", "_CHART_ATTRS", "_SVG_TAGS", "_SVG_ATTRS"}

#: The one function permitted to apply the chart policy, and the keyword it must
#: be passed through. Charts reach the page as `normalise=` on `substitute_into`;
#: any other route is what this file exists to catch.
SANCTIONED_FUNCTION = "_render_report_body"
SANCTIONED_KEYWORD = "normalise"


def _module(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _enclosing_function(tree: ast.Module, node: ast.AST) -> str | None:
    """Name of the function a node sits inside, or None at module level."""
    best = None
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if fn.lineno <= node.lineno and node.lineno <= (fn.end_lineno or fn.lineno):
                if best is None or fn.lineno > best.lineno:
                    best = fn
    return best.name if best else None


def _privileged_uses(tree: ast.Module) -> list[tuple[str, int, str]]:
    """(name, line, enclosing function) for every read of a privileged name.

    The module-level definitions are skipped: assignment is not application.
    """
    assigned_at = {
        t.id for stmt in tree.body if isinstance(stmt, ast.Assign)
        for t in stmt.targets if isinstance(t, ast.Name)
    }
    uses = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id in PRIVILEGED):
            continue
        if isinstance(node.ctx, ast.Store):
            continue                                   # the definition itself
        fn = _enclosing_function(tree, node)
        if fn is None and node.id in assigned_at:
            continue                                   # derivation between definitions
        uses.append((node.id, node.lineno, fn))
    return uses


# --------------------------------------------------------------------------- #
# Where the privileged policy may be read.
# --------------------------------------------------------------------------- #
def test_the_chart_policy_is_read_in_exactly_one_function():
    """One call site, and it is the chart-injection one."""
    uses = _privileged_uses(_module(APP))

    assert uses, "precondition: the policy is used somewhere"
    functions = {fn for _name, _line, fn in uses}
    assert functions == {SANCTIONED_FUNCTION}, (
        "the chart policy is applied outside the chart-injection path: "
        + ", ".join(sorted(f"{n} in {f}" for n, _l, f in uses if f != SANCTIONED_FUNCTION)))


def test_the_chart_policy_is_reached_only_through_the_normalise_keyword():
    """It must be handed to `substitute_into(normalise=...)`, not used loose.

    That keyword is the seam: whatever is passed through it is applied to the
    held-aside charts and to nothing else, because `substitute_into` only ever
    maps it over `self.charts`.
    """
    tree = _module(APP)
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id in PRIVILEGED
                and isinstance(node.ctx, ast.Load)):
            continue
        if _enclosing_function(tree, node) is None:
            continue
        if not _inside_normalise_keyword(tree, node):
            offenders.append(f"{node.id} at line {node.lineno}")
    assert not offenders, (
        "privileged policy used outside `normalise=`: " + ", ".join(offenders))


def _inside_normalise_keyword(tree: ast.Module, target: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != SANCTIONED_KEYWORD:
                continue
            lo, hi = kw.value.lineno, kw.value.end_lineno or kw.value.lineno
            if lo <= target.lineno <= hi:
                return True
    return False


def test_no_other_module_reads_the_chart_policy():
    """It is private to the web layer; nothing else may import it.

    The agent in particular owns no sanitiser and imports none — it hands over
    markup and the caller applies its own policy. That is the layering rule the
    rest of the stages follow, and importing the policy back into the agent would
    quietly undo it.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path == APP:
            continue
        text = path.read_text(encoding="utf-8")
        for name in PRIVILEGED:
            if name in text:
                offenders.append(f"{path.relative_to(SRC.parent.parent)}: {name}")
    assert not offenders, "the chart policy escaped web/app.py:\n  " + "\n  ".join(offenders)


def test_the_agent_imports_no_sanitiser():
    report = (SRC / "agents" / "report.py").read_text(encoding="utf-8")
    assert "import nh3" not in report, (
        "the agent acquired a sanitiser; policy belongs to the caller")


# --------------------------------------------------------------------------- #
# The document sanitiser is the one that guards model text.
# --------------------------------------------------------------------------- #
def test_the_document_body_is_sanitised_with_the_document_policy():
    """The `nh3.clean` that sees model text must use the NARROW policy.

    Asserted structurally so that swapping the two constants — the single edit
    that would undo this entire sequence while leaving every rendering test green
    — fails here.
    """
    tree = _module(APP)
    document_cleans = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "clean"):
            continue
        tags = next((kw.value for kw in node.keywords if kw.arg == "tags"), None)
        if isinstance(tags, ast.Name) and _enclosing_function(tree, node) == SANCTIONED_FUNCTION:
            document_cleans.append((tags.id, node.lineno))

    by_name = {name for name, _line in document_cleans}
    assert "_ALLOWED_TAGS" in by_name, (
        "the report body is no longer sanitised with the document policy")
    assert "_CHART_TAGS" in by_name, "the chart policy is no longer applied at all"


# --------------------------------------------------------------------------- #
# A guard on the guard.
# --------------------------------------------------------------------------- #
def test_the_scan_would_catch_a_new_call_site(tmp_path):
    """With the real count at one, a scanner that stopped matching looks identical
    to success. So it is checked against the exact shape of the mistake."""
    sample = tmp_path / "leak.py"
    sample.write_text(
        "import nh3\n"
        "_CHART_TAGS = {'svg'}\n"
        "_ALLOWED_TAGS = {'p'}\n"
        "\n"
        "def render_body(md):\n"
        "    return nh3.clean(md, tags=_CHART_TAGS)\n",       # the whole document!
        encoding="utf-8")

    uses = _privileged_uses(_module(sample))
    assert ("_CHART_TAGS", 6, "render_body") in uses, (
        "the scan no longer sees the policy being applied to a document")


def test_the_scan_ignores_the_definition_itself():
    """Assignment is not application; the derivation must not read as a finding."""
    tree = _module(APP)
    module_level = [u for u in _privileged_uses(tree) if u[2] is None]
    assert not module_level, f"definitions reported as call sites: {module_level}"
