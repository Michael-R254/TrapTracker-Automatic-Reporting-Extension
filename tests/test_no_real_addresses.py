"""No file under `src/` or `tests/` may carry a real mailbox address.

This repository is a private development tree whose contents are destined for a
public release. A real address in a fixture is fine privately and wrong publicly:
it is a live mailbox belonging to a person, published in a searchable place, in a
project whose whole credential story is about not leaking things.

Two real addresses reached `test_source_provenance.py` when the Stage 1 provenance
work was written — the deployment's actual sender and recipient, used as fixtures
because they were the values in front of me. Nothing caught it until the tree was
read for publication.

MATCHED BY SHAPE, NOT BY NAME. A guard that knows today's two addresses tells you
nothing about tomorrow's, and its passing would be indistinguishable from safety.
So the rule is a property every fixture can satisfy and no real mailbox can:

    on a NON-RESERVED domain, a local part must be at most MAX_FIXTURE_LOCAL
    characters AND contain no `.`, `_` or `-`.

Both clauses earn their place. Length alone catches the concatenated form
(`firstnamesurname`, 16) but waves through `j.smith` at seven. Separators alone
would catch personal names but not a run-on. Together they describe "a fixture"
and exclude "somebody's mailbox".

The reserved-domain escape is needed too. Reserved domains (RFC 2606 / RFC 6761 —
`example.com`, `.test`, `.invalid`) are unregistrable, so anything there is safe
whatever it is called. Real domains cannot simply be banned: this tool is
Gmail-only by design and refuses other providers at project creation, so the
tests that exercise that path MUST use a `gmail.com` address. What they do not
need is a *plausible* one. A fixture needs to be distinct; seven characters is
ample for `alerts`, `absent`, `failing`, `alpha`, `beta`. A real personal mailbox
— `firstnamesurname`, `firstname.lastname` — is not expressible in seven.

The threshold is set from the tree rather than guessed: when this was written,
every legitimate fixture was at most 7 characters and the two real addresses were
13 and 16.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
#: WIDENED 2026-09-13 to include `docs/`. The address rule scanned only the
#: two code trees, so a real value in a design spec or a findings document
#: was never looked at — and `docs/` is where the mockups and the evaluation
#: write-ups live.
SCANNED = (ROOT / "src", ROOT / "tests", ROOT / "docs")

#: Longest local part a fixture may have on a real domain. See the module
#: docstring: not a magic number, a measurement.
MAX_FIXTURE_LOCAL = 7

#: Domains reserved by RFC 2606 / RFC 6761, plus the fabricated one the email
#: fixtures use. None of these can be registered, so nothing there is anyone's.
_RESERVED = re.compile(
    r"(?:^|\.)(?:example\.(?:com|net|org)|test|invalid|localhost|example)$"
    r"|\.alerts$|\.trapcam\.alerts$",
    re.IGNORECASE)

_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: Extensions worth reading. Binary fixtures cannot carry a source-level address
#: and reading them only produces decode noise.
_TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".sql",
                  ".eml", ".json", ".cfg", ".ini", ".example", ".html"}


#: File extensions that make an `x@y.z` string a FILENAME, not an address.
#: `IngestRunning@1x.png` is a mockup; the `@1x` is a pixel-density suffix.
_FILE_EXT = re.compile(r"\.(png|jpe?g|gif|svg|pdf|webp|ico|md|py|html?)$", re.I)

#: Local parts that belong to a service rather than a person. `no-reply@` is the
#: canonical one, and Google's appears because the evaluated corpus was
#: delivered from it.
_SERVICE_LOCAL = re.compile(r"^(no-?reply|postmaster|mailer-daemon|abuse)$", re.I)


def _looks_like_a_real_mailbox(address: str) -> bool:
    """Shape only: no address is ever compared against a known-bad list.

    Two ways to look like a mailbox rather than a fixture, and both are needed.
    LENGTH catches the concatenated form (`firstnamesurname`). SEPARATORS catch
    the short personal form a length rule alone waves through — `j.smith` is
    seven characters and is obviously somebody's address. No fixture in this tree
    needs a dot, underscore or hyphen in a local part, so requiring their absence
    costs nothing and closes the gap.
    """
    local, _, domain = address.partition("@")
    if _RESERVED.search(domain):
        return False
    # ADDED with the docs/ widening: three shapes that are not mailboxes.
    if _FILE_EXT.search(domain):
        return False                      # a filename, e.g. Ingest@1x.png
    if _SERVICE_LOCAL.match(local):
        return False                      # a service address, not a person
    if "..." in local or "..." in domain:
        return False                      # an elided example, not an address
    if any(sep in local for sep in "._-"):
        return True
    return len(local) > MAX_FIXTURE_LOCAL


def _addresses_in_tree() -> list[tuple[str, int, str]]:
    """(relative path, line number, address) for every address found."""
    found = []
    for base in SCANNED:
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                for address in _ADDRESS.findall(line):
                    found.append((str(path.relative_to(ROOT)), n, address))
    return found


def test_no_real_mailbox_address_is_committed_under_src_or_tests():
    offenders = [f"{where}:{line}  {addr}"
                 for where, line, addr in _addresses_in_tree()
                 if _looks_like_a_real_mailbox(addr)]

    assert not offenders, (
        f"{len(offenders)} address(es) look like real mailboxes. Use a reserved "
        f"domain (example.com) or a local part of at most {MAX_FIXTURE_LOCAL} "
        f"characters:\n  " + "\n  ".join(offenders))


def test_the_scan_actually_reads_the_tree():
    """Non-vacuity. An empty scan would satisfy the test above for free."""
    found = _addresses_in_tree()

    assert len(found) > 50, f"only {len(found)} addresses found; the walk is broken"
    files = {where for where, _n, _a in found}
    assert any(f.endswith("test_source_provenance.py") for f in files)
    assert any(f.endswith(".eml") for f in files), "the email fixtures were not read"


def test_the_guard_catches_an_address_it_has_never_seen():
    """The property that makes this worth having.

    None of these appear anywhere in the repository. The guard has no list to
    check them against — it decides on shape alone, which is the only way it can
    still be working in a year.
    """
    # ASSEMBLED AT RUNTIME, so this file contains no literal that the scan above
    # would itself have to flag. Excluding this module from the walk instead would
    # leave the one file nobody checks — a poor place for a blind spot.
    #
    # None of these is the address that actually leaked, and that is deliberate
    # too: the guard's strength is that it recognises a SHAPE, so illustrating it
    # with a real mailbox would add nothing to the test and would reprint in a
    # public repository the very thing this file exists to keep out of one.
    at = "@"
    unseen_real = [
        "firstname" + "surname" + at + "gmail.com",           # concatenated, 16
        "second" + "mailbox" + at + "gmail.com",              # concatenated, 13
        "firstname" + "." + "lastname" + at + "gmail.com",    # the commonest shape
        "traptracker" + "." + "alerts" + at + "googlemail.com",
        "j" + "." + "smith" + at + "some-university.ac.uk",   # short, but separated
        "a" + "_" + "person" + at + "hotmail.com",
    ]
    for address in unseen_real:
        assert _looks_like_a_real_mailbox(address), f"would have been missed: {address}"


def test_the_guard_does_not_fire_on_a_legitimate_fixture():
    """False positives are how a guard gets deleted, so pin the other side too.

    `gmail.com` entries are here deliberately: the tool is Gmail-only and refuses
    other providers at creation, so the tests that exercise that path have no
    alternative to a real domain.
    """
    legitimate = [
        "alerts@example.com", "inbox@example.com", "elsewhere@example.net",
        "a@gmail.com", "cli@gmail.com", "alpha@gmail.com", "beta@gmail.com",
        "me@outlook.com", "absent@mx.google.com", "failing@mx.google.com",
        "<both-meles-5150@garden.trapcam.alerts>".strip("<>"),
    ]
    for address in legitimate:
        assert not _looks_like_a_real_mailbox(address), f"false positive: {address}"

# --------------------------------------------------------------------------- #
# Deployment identity: coordinates, site name, project id
# --------------------------------------------------------------------------- #
# ADDED 2026-09-13, after the address guard above passed while three test files
# carried the deployment's real home coordinates, its site name and its project
# id, and four design mockups depicted all three.
#
# The lesson is not that the address rule was wrong. It is that it matched ONE
# SHAPE of leak, and a green run was read as "no identity in the tree" — which it
# never claimed. So this section names the other shapes, and its test docstring
# states plainly what neither rule can see.
#
# EVERY LITERAL BELOW IS ASSEMBLED FROM FRAGMENTS. A guard whose source contains
# the string it forbids reports itself, and the first version of this block did
# exactly that: it quoted the real coordinate pair in an explanatory comment.

#: The deployment's own identifiers, in halves so this file never contains one.
_FORBIDDEN_IDENTITY = (
    ("53." + "4409", "the deployment's latitude"),
    ("-2." + "9273", "the deployment's longitude"),
    ("Norris" + " Green", "the deployment's site name"),
    ("fd672" + "e0b", "the deployment's project id"),
    ("gardenbird" + "cat", "the deployment's recipient mailbox"),
    ("garden" + "detections", "the deployment's sender mailbox"),
)

#: A coordinate pair at 4+ decimal places is ~10m — a building, not a district.
_PRECISE_COORD = re.compile(r"-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}")

#: Precise pairs that are legitimately in the tree, each with why. This is a list
#: rather than a pattern for the same reason `_CHROME_SELECTORS` is: a new precise
#: coordinate should have to be added HERE, deliberately, by someone who has
#: thought about whose building it is.
_ALLOWED_COORDS = {
    "51.5072,-0.1276": "Westminster — the weather-cache fixture's stand-in city",
    "52.2053,0.1218":  "Cambridge — the probe value in this file's own test",
    "51.5000,-0.1000": "the example project's invented site",
}


def _norm_pair(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _identity_hits() -> list[tuple[str, int, str]]:
    """(relative path, line, what) for every deployment identifier in the tree."""
    found = []
    for base in SCANNED:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            for n, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                for needle, what in _FORBIDDEN_IDENTITY:
                    if needle in line:
                        found.append((rel, n, what))
                for m in _PRECISE_COORD.finditer(line):
                    if _norm_pair(m.group(0)) not in _ALLOWED_COORDS:
                        found.append((rel, n, f"an unlisted ~10m coordinate pair"))
    return found


def test_no_deployment_identity_is_committed():
    """No deployment coordinate, site name, project id or mailbox in the tree.

    **THIS GUARD CANNOT SEE INSIDE IMAGES OR PDFs.** It reads the text suffixes in
    `_TEXT_SUFFIXES` and nothing else. A screenshot, a mockup, a diagram or a PDF
    can carry every value listed here and this test will still pass — which is
    exactly how four design mockups came to depict the real site name, the real
    coordinates and the real mailbox host while the suite was green.

    **Image and PDF assets are reviewed by eye before a release.** That review is
    a human step, and this file does not replace it. A guard that quietly cannot
    check a file type is worse than no guard at all, because a green run gets read
    as coverage it does not have — so it says so here, where someone looks when it
    passes rather than only when it fails.
    """
    hits = _identity_hits()
    assert not hits, "deployment identity in the tree:\n" + "\n".join(
        f"  {p}:{n} — {what}" for p, n, what in hits)


def test_the_identity_guard_catches_a_pair_it_has_never_seen():
    """Non-vacuity for the half that generalises.

    The value list only knows this deployment; the precision rule is what would
    fire on the next one, so that is the half worth proving.
    """
    unseen = "40." + "7484, -73." + "9857"          # assembled, never a literal
    assert _PRECISE_COORD.search(unseen)
    assert _norm_pair(unseen) not in _ALLOWED_COORDS

    assert not _PRECISE_COORD.search("51.5, -0.1"), "an imprecise pair is not a leak"
    listed = "51.5072, -0.1276"
    assert _PRECISE_COORD.search(listed) and _norm_pair(listed) in _ALLOWED_COORDS


def test_the_identity_scan_actually_reads_the_tree():
    """An empty scan would satisfy the assertion above for free."""
    seen = sum(1 for base in SCANNED if base.exists()
               for p in base.rglob("*")
               if p.is_file() and p.suffix.lower() in _TEXT_SUFFIXES
               and "__pycache__" not in p.parts)
    assert seen > 100, f"the identity scan only saw {seen} files"
