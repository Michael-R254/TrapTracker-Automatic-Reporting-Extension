"""Recover TrapTracker's rendered detection rectangles from the boxed frame.

Validated in Phase 1 over all 787 events: 835 of 909 rectangles (91.9%) pass the
closure self-check, and every one of those has 100% of its perimeter on painted
overlay — no geometrically wrong box passed. Failures announce themselves, which
is the property that makes a per-event fallback possible.

Renderer structure, established empirically:
  * one overlay green, RGB ~(60, 111, 19), 3px axis-aligned stroke;
  * a FILLED label banner "<Label> <conf>" in the same green with near-white
    glyphs, whose BOTTOM edge is the rectangle's TOP edge and whose LEFT edge
    aligns with the rectangle's left; the banner is often wider than its box.

Recovery is banner-anchored. Two properties of the data ruled out the obvious
approaches: naive contour finding pairs the gaps BETWEEN LETTERS as box edges,
and intersecting the green mask with "pixels that changed" punches holes straight
through the rectangle, because overlay green painted over similar-coloured grass
barely changes the pixel at all.

Heavy imports are deferred so this module imports without the ``[enrich]`` extra.
"""

from __future__ import annotations

from typing import Optional

from ..logging import get_logger
from .base import Box

logger = get_logger(__name__)

#: Measured from the corpus by differencing clean against boxed frames.
GREEN = (60, 111, 19)
#: |B - GREEN|inf < GTOL. At 30 this hits 0.011% of clean-frame area on average
#: (0.100% worst case) — the overlay green is very nearly unique against grass,
#: fence, sky and infrared grey.
GTOL = 30
WHITE = 225


def _np():
    import numpy as np
    return np


def load_rgb(path: str):
    """Read an image as an HxWx3 int16 array."""
    import numpy as np
    from PIL import Image
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.int16)


def masks(frame):
    """(green, text, green|text) boolean masks for the boxed frame."""
    np = _np()
    g = np.abs(frame - np.array(GREEN)).max(axis=2) < GTOL
    t = frame.min(axis=2) >= WHITE
    return g, t, (g | t)


def _runs(line) -> list[tuple[int, int]]:
    np = _np()
    idx = np.flatnonzero(np.diff(np.concatenate(([0], line.view(np.int8), [0]))))
    return list(zip(idx[0::2], idx[1::2] - 1))


def _bridge(line, gap: int = 6):
    """Close runs separated by <= gap px — anti-aliasing opens 1-2px holes
    between a glyph and the fill it sits on."""
    out = line.copy()
    rs = _runs(line)
    for (_, b1), (a2, _) in zip(rs, rs[1:]):
        if a2 - b1 - 1 <= gap:
            out[b1:a2] = True
    return out


def _run_at(line, x: int) -> Optional[tuple[int, int]]:
    for a, b in _runs(line):
        if a <= x <= b:
            return int(a), int(b)
    return None


def find_banners(mask_all, mask_green, *, min_w=28, min_h=12, max_h=40,
                 cover=0.85, gap=6, green_frac=0.45) -> list[dict]:
    """Filled label banners.

    Rows are matched on COVERAGE of the band's span rather than on exact
    endpoints — a glyph straddling the fill can split one row into two runs
    without the banner having ended. The span is the MEDIAN of the per-row runs,
    so a partially rendered first row cannot pin the band narrow. Bands must be
    green-dominant, which rejects the camera's own near-white OSD text
    (timestamp, watermark, channel name).
    """
    np = _np()
    h, _ = mask_all.shape
    bridged = np.array([_bridge(mask_all[y], gap) for y in range(h)])
    bands: list[dict] = []
    cur: Optional[dict] = None

    def close(c):
        if not c or not (min_h <= c["y1"] - c["y0"] + 1 <= max_h):
            return
        x0 = int(np.median([r[0] for r in c["runs"]]))
        x1 = int(np.median([r[1] for r in c["runs"]]))
        if x1 - x0 + 1 < min_w:
            return
        sub = mask_green[c["y0"]:c["y1"] + 1, x0:x1 + 1]
        if sub.size and sub.mean() >= green_frac:
            bands.append({"x0": x0, "y0": c["y0"], "x1": x1, "y1": c["y1"]})

    for y in range(h):
        if cur is not None:
            seg = bridged[y, cur["x0"]:cur["x1"] + 1]
            if seg.size and seg.mean() >= cover:
                cur["y1"] = y
                r = _run_at(bridged[y], (cur["x0"] + cur["x1"]) // 2)
                if r:
                    cur["runs"].append(r)
                continue
            close(cur)
            cur = None
        rs = [(int(a), int(b)) for a, b in _runs(bridged[y]) if b - a + 1 >= min_w]
        if rs:
            a, b = max(rs, key=lambda r: r[1] - r[0])
            cur = {"x0": a, "y0": y, "x1": b, "y1": y, "runs": [(a, b)]}
    close(cur)
    return bands


def box_from_banner(mask_green, banner: dict, *, min_side=10, search=8) -> dict:
    """Trace the rectangle a banner labels. Always returns a record with a status.

    The banner fixes the box's left and top edges; the box's bottom edge — which
    no banner can overlap — then fixes the right. Small gaps are bridged because
    JPEG chroma subsampling smears the 3px stroke where it crosses a
    high-contrast background.
    """
    h, w = mask_green.shape
    bx0, by1 = banner["x0"], banner["y1"]
    rec: dict = {"banner": (banner["x0"], banner["y0"], banner["x1"], banner["y1"])}

    best = None
    for x in range(max(0, bx0 - 2), min(w, bx0 + 5)):
        col = _bridge(mask_green[:, x], 4)
        y = by1 + 1
        while y < h and not col[y] and y <= by1 + search:
            y += 1
        if y >= h or not col[y]:
            continue
        y0 = y
        while y + 1 < h and col[y + 1]:
            y += 1
        if best is None or (y - y0) > (best[2] - best[1]):
            best = (x, y0, y)
    if best is None:
        rec["status"] = "no_left_edge"
        return rec
    xl, ytop, ybot = best
    if ybot - ytop < min_side:
        rec["status"] = "left_edge_too_short"
        return rec

    rr = None
    for y in range(ybot, max(ybot - 3, 0) - 1, -1):
        r = _run_at(_bridge(mask_green[y], 6), xl)
        if r and r[1] - r[0] + 1 >= min_side:
            rr = r
            break
    if rr is None:
        rec["status"] = "no_bottom_edge"
        return rec
    xr = rr[1]

    def hf(y):
        return float(mask_green[y, xl:xr + 1].mean()) if 0 <= y < h else 0.0

    def vf(x):
        return float(mask_green[ytop:ybot + 1, x].mean()) if 0 <= x < w else 0.0

    edges = (max(hf(ytop + k) for k in (0, 1, 2)),
             max(hf(ybot - k) for k in (0, 1, 2)),
             max(vf(xl + k) for k in (0, 1, 2)),
             max(vf(xr - k) for k in (0, 1, 2)))
    rec.update(
        box=(int(xl), int(ytop), int(xr), int(ybot)),
        status="ok",
        edges=tuple(round(v, 3) for v in edges),
        # The TOP edge is anchored by the banner, which frequently covers it, so
        # closure is judged on the three edges drawn in the open.
        closed=bool(min(edges[1], edges[2], edges[3]) >= 0.70),
        clipped=bool(xl <= 2 or ytop <= 2 or xr >= w - 3 or ybot >= h - 3),
    )
    return rec


def recover(boxed_frame) -> list[dict]:
    """Every banner in the frame, each with the rectangle it labels (or a status)."""
    g, _t, a = masks(boxed_frame)
    return [box_from_banner(g, b) for b in find_banners(a, g)]
