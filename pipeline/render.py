"""Colour a COG window into a 256x256 Web Mercator PNG tile.

Shared by the live tile server and the static exporter, so both produce
byte-identical tiles.
"""
from __future__ import annotations

import io
import warnings
from functools import lru_cache

import numpy as np
from PIL import Image
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

from .config import Layer

TILE = 256

# Global grids reach ±89.875°; rio-tiler clamps them to the Mercator limit and says so on every tile.
warnings.filterwarnings("ignore", message="Adjusting dataset latitudes")


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


@lru_cache(maxsize=32)
def lut(layer: Layer) -> tuple[np.ndarray, float, float]:
    """256-entry RGB lookup spanning the ramp's first..last stop."""
    values = np.array([v for v, _ in layer.ramp], dtype=np.float64)
    colors = np.array([_hex(c) for _, c in layer.ramp], dtype=np.float64)
    lo, hi = float(values[0]), float(values[-1])
    xs = np.linspace(lo, hi, 256)
    table = np.stack([np.interp(xs, values, colors[:, i]) for i in range(3)], axis=1)
    return table.round().astype(np.uint8), lo, hi


def colorize(layer: Layer, data: np.ndarray) -> np.ndarray:
    """float grid (NaN = missing) -> RGBA uint8."""
    table, lo, hi = lut(layer)
    finite = np.isfinite(data)
    scaled = np.zeros(data.shape, dtype=np.int32)
    scaled[finite] = np.clip(((data[finite] - lo) / (hi - lo) * 255).round(), 0, 255)
    rgba = np.zeros((*data.shape, 4), dtype=np.uint8)
    rgba[..., :3] = table[scaled]
    visible = finite.copy()
    if layer.transparent_below is not None:
        visible &= np.where(finite, data, -np.inf) >= layer.transparent_below
    rgba[..., 3] = np.where(visible, 255, 0)
    return rgba


def png(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


EMPTY_TILE = png(np.zeros((TILE, TILE, 4), dtype=np.uint8))


def render_tile(cog_path: str, layer: Layer, z: int, x: int, y: int) -> bytes | None:
    """PNG bytes, or None when the tile does not touch the dataset."""
    with Reader(cog_path) as src:
        try:
            img = src.tile(x, y, z, tilesize=TILE, resampling_method="bilinear")
        except TileOutsideBounds:
            return None
    data = img.array[0].astype(np.float32).filled(np.nan)
    return png(colorize(layer, data))


def bounds(cog_path: str) -> tuple[float, float, float, float]:
    """Dataset bounds in lon/lat, clamped to the Web Mercator latitude limit."""
    with Reader(cog_path) as src:
        w, s, e, n = src.get_geographic_bounds("EPSG:4326")
    return max(w, -180.0), max(s, -85.0511), min(e, 180.0), min(n, 85.0511)
