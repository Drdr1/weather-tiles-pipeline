"""Decode GRIB2 messages into north-up numpy grids with a geographic transform.

Uses ecCodes directly rather than xarray/cfgrib: we only need regular
lat/lon grids, and reading the grid keys ourselves makes scanning order and
0-360 longitudes explicit instead of implicit.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import eccodes
import numpy as np
from rasterio.transform import Affine, from_origin

# "VAR:LEVEL" (as written in .idx files) -> GRIB2 identity
# (discipline, parameterCategory, parameterNumber, typeOfFirstFixedSurface, level)
GRIB_IDS: dict[str, tuple[int, int, int, int, int]] = {
    "TMP:2 m above ground": (0, 0, 0, 103, 2),
    "UGRD:10 m above ground": (0, 2, 2, 103, 10),
    "VGRD:10 m above ground": (0, 2, 3, 103, 10),
    "PRATE:surface": (0, 1, 7, 1, 0),
}


@dataclass
class Field:
    key: str
    data: np.ndarray  # float32, rows north->south, cols west->east, NaN = missing
    transform: Affine  # EPSG:4326, pixel-edge origin

    @property
    def shape(self) -> tuple[int, int]:
        return self.data.shape


def _identity(h) -> tuple[int, int, int, int, int]:
    # Code-table keys default to their string abbreviation ("sfc"); ask for the number.
    def num(key: str) -> int:
        return int(eccodes.codes_get(h, key, ktype=int)) if eccodes.codes_is_defined(h, key) else 0

    return (num("discipline"), num("parameterCategory"), num("parameterNumber"),
            num("typeOfFirstFixedSurface"), num("level"))


def _decode(h) -> tuple[np.ndarray, Affine]:
    grid = eccodes.codes_get(h, "gridType")
    if grid != "regular_ll":
        raise ValueError(f"unsupported grid type {grid}")
    ni = eccodes.codes_get(h, "Ni")
    nj = eccodes.codes_get(h, "Nj")
    lat0 = eccodes.codes_get(h, "latitudeOfFirstGridPointInDegrees")
    lon0 = eccodes.codes_get(h, "longitudeOfFirstGridPointInDegrees")
    lat1 = eccodes.codes_get(h, "latitudeOfLastGridPointInDegrees")
    dx = eccodes.codes_get(h, "iDirectionIncrementInDegrees")
    dy = eccodes.codes_get(h, "jDirectionIncrementInDegrees")
    if eccodes.codes_get(h, "iScansNegatively"):
        raise ValueError("east-to-west scanning not supported")

    values = eccodes.codes_get_values(h).astype(np.float32)
    if eccodes.codes_get(h, "bitmapPresent"):
        values[values == eccodes.codes_get(h, "missingValue")] = np.nan
    data = values.reshape(nj, ni)

    # Make it north-up.
    north = max(lat0, lat1)
    if lat0 < lat1:
        data = data[::-1]

    # Normalise longitudes to -180..180.
    if abs(ni * dx - 360.0) < 1e-6:
        # Global grid starting at 0 E: roll so it starts at 180 W.
        if abs(lon0) < 1e-6:
            data = np.roll(data, ni // 2, axis=1)
            lon0 = -180.0
    elif lon0 >= 180.0:
        lon0 -= 360.0

    # Drop rows that sit exactly on the poles: Web Mercator can't project them.
    if abs(north - 90.0) < 1e-6:
        data = data[1:]
        north -= dy
    if abs(min(lat0, lat1) + 90.0) < 1e-6:
        data = data[:-1]

    transform = from_origin(lon0 - dx / 2, north + dy / 2, dx, dy)
    return np.ascontiguousarray(data), transform


def read_fields(path: Path) -> dict[str, Field]:
    """Every recognised message in the file, keyed by "VAR:LEVEL"."""
    wanted = {v: k for k, v in GRIB_IDS.items()}
    fields: dict[str, Field] = {}
    with open(path, "rb") as fh:
        while (h := eccodes.codes_grib_new_from_file(fh)) is not None:
            try:
                key = wanted.get(_identity(h))
                if key and key not in fields:
                    data, transform = _decode(h)
                    fields[key] = Field(key, data, transform)
            finally:
                eccodes.codes_release(h)
    return fields


def read_first(path: Path, key: str = "radar") -> Field:
    """First message of any parameter (MRMS files hold exactly one)."""
    with open(path, "rb") as fh:
        h = eccodes.codes_grib_new_from_file(fh)
        if h is None:
            raise ValueError(f"no GRIB messages in {path}")
        try:
            data, transform = _decode(h)
        finally:
            eccodes.codes_release(h)
    return Field(key, data, transform)
