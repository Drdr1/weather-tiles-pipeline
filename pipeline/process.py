"""Unit conversion, quality control and Cloud-Optimized GeoTIFF output."""
from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.shutil import copy as rio_copy

from .config import Layer
from .grib import Field

log = logging.getLogger(__name__)

KELVIN = 273.15


class QCError(Exception):
    pass


def derive(layer: Layer, fields: dict[str, Field]) -> Field | None:
    """Turn raw GRIB records into the layer's display quantity, or None if inputs are absent."""
    if any(rec not in fields for rec in layer.records):
        return None
    if layer.id == "temp2m":
        src = fields["TMP:2 m above ground"]
        return Field(layer.id, src.data - KELVIN, src.transform)
    if layer.id == "wind10m":
        u, v = fields["UGRD:10 m above ground"], fields["VGRD:10 m above ground"]
        if u.shape != v.shape:
            raise QCError("u/v grids differ in shape")
        return Field(layer.id, np.hypot(u.data, v.data).astype(np.float32), u.transform)
    if layer.id == "precip":
        src = fields["PRATE:surface"]  # kg m-2 s-1 == mm/s
        return Field(layer.id, src.data * 3600.0, src.transform)
    raise KeyError(f"no derivation for layer {layer.id}")


def radar_field(raw: Field) -> Field:
    # MRMS encodes "missing" and "no coverage" as large negative sentinels.
    data = raw.data.copy()
    data[data < -90] = np.nan
    return Field("radar", data, raw.transform)


@dataclass
class QCReport:
    layer: str
    hour: int
    ok: bool
    nan_fraction: float
    min: float | None
    max: float | None
    reason: str = ""


def check(layer: Layer, hour: int, field: Field, max_nan_fraction: float = 0.01) -> QCReport:
    """Reject fields that are empty, mostly missing, flat, or physically implausible."""
    data = field.data
    finite = np.isfinite(data)
    nan_frac = float(1.0 - finite.mean()) if data.size else 1.0
    lo = float(np.nanmin(data)) if finite.any() else None
    hi = float(np.nanmax(data)) if finite.any() else None

    def report(ok: bool, reason: str = "") -> QCReport:
        return QCReport(layer.id, hour, ok, round(nan_frac, 4), lo, hi, reason)

    if data.size == 0:
        return report(False, "empty grid")
    if nan_frac > max_nan_fraction:
        return report(False, f"{nan_frac:.1%} missing (limit {max_nan_fraction:.0%})")
    if lo is not None and (lo < layer.valid_range[0] or hi > layer.valid_range[1]):
        return report(False, f"values {lo:.2f}..{hi:.2f} outside {layer.valid_range}")
    # A perfectly flat temperature or wind field means a bad decode, not weather.
    # (Precip and radar can legitimately be flat zero over a dry domain.)
    if layer.id in ("temp2m", "wind10m") and lo == hi:
        return report(False, "field is constant")
    return report(True)


def write_cog(field: Field, path: Path) -> Path:
    """Float32 COG in EPSG:4326 with internal overviews, written atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    h, w = field.shape
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        plain = Path(tmp) / "plain.tif"
        with rasterio.open(
            plain, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
            crs="EPSG:4326", transform=field.transform, nodata=np.nan,
        ) as dst:
            dst.write(field.data.astype(np.float32), 1)
        staged = Path(tmp) / "cog.tif"
        rio_copy(plain, staged, driver="COG", compress="DEFLATE", predictor=3,
                 overview_resampling="average", blocksize=512)
        shutil.move(staged, path)
    return path


def qc_dict(report: QCReport) -> dict:
    return asdict(report)
