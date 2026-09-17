"""Find photographs anywhere in the tracked tree, including inside containers.

Written because a path-and-filename check missed one. `faunal-monitoring-report_
sample.pdf` sits at the repository root with a wholly innocuous name, and embeds
three camera frames of a private garden on page 7. Every grep-based check in the
publication verification set — excluded directories, known strings, known paths —
passes it cleanly, because none of them opens the file.

So this scan opens things. It walks every tracked file, extracts raster images
from the containers that can hold them, and classifies each as a photograph or a
synthetic graphic by CONTINUOUS TONE.

WHY CONTINUOUS TONE. A camera frame is a smooth surface: illumination varies
across it, JPEG quantisation leaves thousands of near-neighbour colours, and a
sample of a few thousand pixels finds hundreds of distinct values. A chart, an
icon or a 1x1 test fixture is drawn from a small fixed palette and does not.
The two populations are nowhere near each other. Measured on this repository:
photographs sample 2747-3450 distinct colours; charts and figures sample 52-76;
the 1x1 test fixtures sample 1. The threshold sits in a gap of two orders of
magnitude, so it does not have to be delicate to be right - and the band between
UNCERTAIN_COLOURS and PHOTO_COLOURS is reported rather than decided, because a
scan that guesses in the middle is worse than one that asks.

It is a heuristic and it is defensible as one: it is a classifier over a property
photographs have and drawings do not, it reports its evidence (dimensions, colour
count) rather than just a verdict, and it is wrong in the safe direction. A
photograph misread as a graphic is the failure that matters, and continuous tone
is the property hardest for a photograph to lack.

CLEARED FILES. `REVIEWED` lists files a person has opened and cleared, by path,
with the date and what was checked. It applies to the UNCERTAIN BAND ONLY: a
photograph is still a photograph and still fails, and a cleared file that has
become one is reported louder than an unlisted one, because that means it changed
since it was looked at. The list exists so the uncertain column keeps meaning
"look at this" — six permanently unresolved entries would teach a reader to skip
it, which is the same failure the address guard's docstring warns about.

READ-ONLY. Opens files, writes nothing, and needs no network.

    python docs/evaluation/embedded_image_scan.py            # tracked files
    python docs/evaluation/embedded_image_scan.py --all      # ignore git, walk the tree
"""

from __future__ import annotations

import argparse
import io
import pathlib
import subprocess
import zipfile

#: Distinct sampled colours above which an image is called a photograph. Set from
#: the observed gap, not guessed: photographs here sample 2747-3450, charts 52-76.
#: Anything between the two populations is reported for a human to look at.
PHOTO_COLOURS = 256
UNCERTAIN_COLOURS = 64

#: Files a person has opened, looked at, and cleared - with the date and what was
#: checked. A UI screenshot samples 70-133 colours: anti-aliased text and a tinted
#: map pane put it in the uncertain band, above a chart and nowhere near a camera
#: frame. Six of them sitting in the uncertain list permanently would be six files
#: a reader has to re-examine every release, which is how a list stops being read.
#:
#: A list rather than a rule, and by PATH rather than by directory, for the same
#: reason `_ALLOWED_COORDS` is a list in `tests/test_no_real_addresses.py`: a new
#: uncertain image should have to be added here, deliberately, by someone who has
#: looked at it.
#:
#: IT DOES NOT SILENCE A PHOTOGRAPH. Clearing applies to the uncertain band only.
#: If one of these ever classifies as a PHOTOGRAPH the file has materially changed
#: since it was reviewed, and that is reported louder than an unlisted one.
REVIEWED = {
    "docs/screenshots/1-ProjectPage1.png":
        "reviewed 2026-09-17: projects page, Example Site only",
    "docs/screenshots/2-ProjectPage2.png":
        "reviewed 2026-09-17: browser-or-terminal panel, no project data",
    "docs/screenshots/3-CreateProject.png":
        "reviewed 2026-09-17: empty create form, password field blank",
    "docs/screenshots/4-MonitoringPage.png":
        "reviewed 2026-09-17: reports page, Example Site only",
    "docs/screenshots/5-ReportTypes.png":
        "reviewed 2026-09-17: report-type menu, four example classes",
    "docs/screenshots/6-IngestAlerts.png":
        "reviewed 2026-09-17: ingest page, no mailbox configured",
}

#: Containers worth opening. Everything else is read as a bare image.
PDF_SUFFIXES = {".pdf"}
ZIP_SUFFIXES = {".docx", ".pptx", ".xlsx", ".odt", ".zip", ".epub"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
#: Text that can still carry an image, as a data: URI.
TEXT_SUFFIXES = {".html", ".htm", ".md", ".svg", ".csv", ".json", ".py"}


def tracked_files(root: pathlib.Path) -> list[pathlib.Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                         capture_output=True, text=True, check=True).stdout
    return [root / p for p in out.split("\0") if p]


def sampled_colours(data: bytes) -> tuple[int, int, int] | None:
    """(width, height, distinct sampled colours) or None if it will not decode."""
    try:
        from PIL import Image
    except ImportError:                      # pragma: no cover - reported by main
        raise
    try:
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
    except Exception:
        return None
    w, h = im.size
    pixels = list(im.getdata())
    step = max(1, len(pixels) // 4000)
    return w, h, len(set(pixels[::step]))


def classify(colours: int) -> str:
    if colours >= PHOTO_COLOURS:
        return "PHOTOGRAPH"
    if colours >= UNCERTAIN_COLOURS:
        return "uncertain"
    return "synthetic"


def images_in(path: pathlib.Path):
    """Yield (where, bytes) for every raster image reachable from this file."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        yield "", path.read_bytes()
        return
    if suffix in PDF_SUFFIXES:
        try:
            import fitz
        except ImportError:
            print(f"  ! {path}: PDF support needs pymupdf ([dev] extra)")
            return
        with fitz.open(path) as doc:
            for page_no in range(doc.page_count):
                for img in doc[page_no].get_images(full=True):
                    try:
                        yield f"page {page_no + 1} xref {img[0]}", doc.extract_image(img[0])["image"]
                    except Exception:
                        continue
        return
    if suffix in ZIP_SUFFIXES:
        try:
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    if pathlib.Path(name).suffix.lower() in IMAGE_SUFFIXES:
                        yield name, zf.read(name)
        except zipfile.BadZipFile:
            pass
        return
    if suffix in TEXT_SUFFIXES:
        import base64
        import re

        text = path.read_text(encoding="utf-8", errors="replace")
        for n, m in enumerate(re.finditer(
                r"data:image/(png|jpe?g|gif|webp|bmp);base64,([A-Za-z0-9+/=\s]{64,})", text)):
            try:
                yield f"data-uri #{n + 1}", base64.b64decode(re.sub(r"\s", "", m.group(2)))
            except Exception:
                continue


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true",
                    help="Walk the whole tree rather than only tracked files.")
    ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    args = ap.parse_args()

    root = args.root.resolve()
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("error: needs Pillow — pip install -e \".[enrich]\"")
        return 2

    if args.all:
        files = [p for p in sorted(root.rglob("*"))
                 if p.is_file() and ".git" not in p.parts and ".venv" not in p.parts]
    else:
        files = tracked_files(root)

    findings, scanned, carriers = [], 0, 0
    for path in files:
        got_any = False
        for where, data in images_in(path):
            scanned += 1
            got_any = True
            result = sampled_colours(data)
            if result is None:
                continue
            w, h, colours = result
            findings.append((classify(colours), path.relative_to(root), where, w, h, colours))
        if got_any:
            carriers += 1

    # A cleared file leaves the uncertain band; it does NOT leave the photograph
    # one. See `REVIEWED`.
    changed = []
    for i, (verdict, rel, where, w, h, colours) in enumerate(findings):
        key = rel.as_posix()
        if key not in REVIEWED:
            continue
        if verdict == "uncertain":
            findings[i] = ("reviewed", rel, where, w, h, colours)
        elif verdict == "PHOTOGRAPH":
            changed.append((rel, colours))

    order = {"PHOTOGRAPH": 0, "uncertain": 1, "reviewed": 2, "synthetic": 3}
    findings.sort(key=lambda f: (order[f[0]], str(f[1])))

    print(f"files examined : {len(files)}")
    print(f"image carriers : {carriers}")
    print(f"images found   : {scanned}\n")
    for verdict, rel, where, w, h, colours in findings:
        loc = f"{rel}" + (f"  [{where}]" if where else "")
        note = f"  ({REVIEWED[rel.as_posix()]})" if verdict == "reviewed" else ""
        print(f"  {verdict:11} {w:>5}x{h:<5} {colours:>5} colours  {loc}{note}")

    for rel, colours in changed:
        print(f"\n!! {rel} is in REVIEWED but now samples {colours} colours and "
              f"classifies as a PHOTOGRAPH. It has changed since it was cleared; "
              f"look at it again rather than re-clearing it.")

    photos = [f for f in findings if f[0] == "PHOTOGRAPH"]
    uncertain = [f for f in findings if f[0] == "uncertain"]
    reviewed = [f for f in findings if f[0] == "reviewed"]
    print(f"\nPHOTOGRAPHS: {len(photos)}   uncertain: {len(uncertain)}   "
          f"reviewed: {len(reviewed)}   "
          f"synthetic: {len(findings) - len(photos) - len(uncertain) - len(reviewed)}")
    return 1 if photos else 0


if __name__ == "__main__":
    raise SystemExit(main())
