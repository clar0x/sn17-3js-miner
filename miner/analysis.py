"""Deterministic measurements of the prompt photo (object mask, silhouette and colors) for the fallback lathe.

Only integer NumPy work on decoded pixels, so replay reproduces the same module byte for byte.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

BORDER = 8
MASK_DISTANCE = 30
MIN_SHARE = 30  # per mille of object pixels
MAX_COLORS = 5
BANDS = 16


@dataclass(frozen=True)
class Facts:
    size: tuple[int, int]
    box: tuple[int, int, int, int]  # x0, y0, x1, y1 (exclusive)
    background: str
    colors: tuple[tuple[str, int], ...]  # sRGB hex, per mille of object pixels
    widths: tuple[int, ...]  # silhouette width per horizontal band, per mille of box height
    band_colors: tuple[str, ...]

    @property
    def aspect(self) -> float:
        x0, y0, x1, y1 = self.box
        return (x1 - x0) / max(1, y1 - y0)



def _hex(rgb: np.ndarray) -> str:
    return "#" + "".join(f"{int(v):02x}" for v in rgb)


def _median(values: np.ndarray) -> int:
    ordered = np.sort(values)
    return int(ordered[len(ordered) // 2])


def measure(image: bytes) -> Facts | None:
    with Image.open(io.BytesIO(image)) as decoded:
        has_alpha = decoded.mode in ("RGBA", "LA") or "transparency" in decoded.info
        rgba = np.asarray(decoded.convert("RGBA"), dtype=np.int32)
    rgb = rgba[:, :, :3]
    height, width = rgb.shape[:2]
    if height <= 4 * BORDER or width <= 4 * BORDER:
        return None
    border = np.concatenate(
        [
            rgb[:BORDER].reshape(-1, 3),
            rgb[-BORDER:].reshape(-1, 3),
            rgb[:, :BORDER].reshape(-1, 3),
            rgb[:, -BORDER:].reshape(-1, 3),
        ]
    )
    background = np.array([_median(border[:, c]) for c in range(3)], dtype=np.int32)
    if has_alpha and int((rgba[:, :, 3] < 128).sum()) > 0:
        mask = rgba[:, :, 3] >= 128
    else:
        mask = np.abs(rgb - background).max(axis=2) > MASK_DISTANCE
    # Ignore isolated specks: rows and columns need a minimum count to bound the object.
    rows = np.flatnonzero(mask.sum(axis=1) > width // 200)
    cols = np.flatnonzero(mask.sum(axis=0) > height // 200)
    if len(rows) == 0 or len(cols) == 0:
        return None
    y0, y1, x0, x1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    inside = np.zeros_like(mask)
    inside[y0:y1, x0:x1] = True
    mask &= inside
    pixels = rgb[mask]
    if len(pixels) < 64:
        return None

    # Greedy clustering of 16-level bins: exact integer counts and sums, stable tie order.
    bins = ((pixels[:, 0] >> 4) << 8) | ((pixels[:, 1] >> 4) << 4) | (pixels[:, 2] >> 4)
    counts = np.bincount(bins, minlength=4096).astype(np.int64)
    sums = np.stack([np.bincount(bins, weights=pixels[:, c], minlength=4096) for c in range(3)], axis=1)
    sums = np.rint(sums).astype(np.int64)
    levels = np.stack([np.arange(4096) >> 8, (np.arange(4096) >> 4) & 15, np.arange(4096) & 15], axis=1)
    free = counts > 0
    total = int(counts.sum())
    colors: list[tuple[str, int]] = []
    while free.any() and len(colors) < MAX_COLORS:
        seed = int(np.flatnonzero(free)[np.argmax(counts[free])])
        near = free & (np.abs(levels - levels[seed]).max(axis=1) <= 2)
        count = int(counts[near].sum())
        free &= ~near
        share = count * 1000 // total
        if share < MIN_SHARE:
            continue
        mean = sums[near].sum(axis=0) // count
        colors.append((_hex(mean), share))

    # Silhouette width and mean color per horizontal band, top to bottom.
    box_height = y1 - y0
    widths: list[int] = []
    band_colors: list[str] = []
    for band in range(BANDS):
        top = y0 + box_height * band // BANDS
        bottom = max(top + 1, y0 + box_height * (band + 1) // BANDS)
        region = mask[top:bottom, x0:x1]
        filled = np.flatnonzero(region.any(axis=0))
        widths.append(0 if len(filled) == 0 else (int(filled[-1]) - int(filled[0]) + 1) * 1000 // box_height)
        selected = rgb[top:bottom, x0:x1][region]
        band_colors.append(_hex(selected.sum(axis=0) // len(selected)) if len(selected) else colors[0][0])
    return Facts(
        size=(width, height),
        box=(x0, y0, x1, y1),
        background=_hex(background),
        colors=tuple(colors),
        widths=tuple(widths),
        band_colors=tuple(band_colors),
    )
