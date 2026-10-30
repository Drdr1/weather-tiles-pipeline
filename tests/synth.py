"""Synthetic NOAA-shaped data so the full pipeline runs offline.

Messages are real GRIB2 encoded with ecCodes using the same grid layout and
parameter identities as GFS 0.25° and MRMS, just at coarser resolution.
"""
from __future__ import annotations

from datetime import datetime

import eccodes
import numpy as np

from pipeline.grib import GRIB_IDS
from pipeline.http import FetchError, NotFound

MISSING = 9999.0


def grib_message(key_ids, values_north_up, lat_north, lat_south, lon_west, lon_east,
                 dx, dy, south_to_north=False, date=20261030, hour=0, missing_mask=None,
                 bits=16) -> bytes:
    discipline, cat, num, level_type, level = key_ids
    nj, ni = values_north_up.shape
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        eccodes.codes_set(h, "centre", 7)  # NCEP
        eccodes.codes_set(h, "dataDate", date)
        eccodes.codes_set(h, "dataTime", hour * 100)
        eccodes.codes_set(h, "discipline", discipline)
        eccodes.codes_set(h, "parameterCategory", cat)
        eccodes.codes_set(h, "parameterNumber", num)
        eccodes.codes_set(h, "typeOfFirstFixedSurface", level_type)
        if level_type == 103:
            eccodes.codes_set(h, "scaleFactorOfFirstFixedSurface", 0)
            eccodes.codes_set(h, "scaledValueOfFirstFixedSurface", level)
        eccodes.codes_set(h, "Ni", ni)
        eccodes.codes_set(h, "Nj", nj)
        eccodes.codes_set(h, "iDirectionIncrementInDegrees", dx)
        eccodes.codes_set(h, "jDirectionIncrementInDegrees", dy)
        eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", lon_west)
        eccodes.codes_set(h, "longitudeOfLastGridPointInDegrees", lon_east)
        values = values_north_up.astype(np.float64)
        if south_to_north:
            eccodes.codes_set(h, "jScansPositively", 1)
            eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", lat_south)
            eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", lat_north)
            values = values[::-1]
        else:
            eccodes.codes_set(h, "jScansPositively", 0)
            eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", lat_north)
            eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", lat_south)
        eccodes.codes_set(h, "bitsPerValue", bits)
        if missing_mask is not None:
            mask = missing_mask[::-1] if south_to_north else missing_mask
            eccodes.codes_set(h, "bitmapPresent", 1)
            eccodes.codes_set(h, "missingValue", MISSING)
            values = np.where(mask, MISSING, values)
        eccodes.codes_set_values(h, values.ravel())
        return eccodes.codes_get_message(h)
    finally:
        eccodes.codes_release(h)


def global_grid(step=1.0):
    """GFS-style global grid: lat 90..-90, lon 0..360-step."""
    lats = np.arange(90, -90 - step / 2, -step)
    lons = np.arange(0, 360, step)
    return lats, lons


def gfs_fields(fhr: int, step=1.0) -> dict[str, np.ndarray]:
    lats, lons = global_grid(step)
    lon2, lat2 = np.meshgrid(lons, lats)
    # Warm tropics, cold poles, plus a smooth east/west wave so columns are distinguishable
    # (continuous across 0°, so tiles show no artificial seam).
    temp_k = 273.15 + 30 * np.cos(np.radians(lat2)) - 25 + 3 * np.sin(np.radians(lon2)) + fhr * 0.1
    u = 10 * np.sin(np.radians(lat2) * 3)
    v = 5 * np.cos(np.radians(lon2))
    prate = np.clip(np.sin(np.radians(lon2) * 4) * np.cos(np.radians(lat2)), 0, None) * 0.002  # kg m-2 s-1
    out = {
        "TMP:2 m above ground": temp_k,
        "UGRD:10 m above ground": u,
        "VGRD:10 m above ground": v,
    }
    if fhr != 0:
        out["PRATE:surface"] = prate
    return out


def gfs_file(fhr: int, step=1.0, cycle: datetime | None = None, overrides=None) -> tuple[bytes, str]:
    """(grib bytes, .idx text) for one forecast hour, like NOAA publishes them."""
    cycle = cycle or datetime(2026, 10, 30, 0)
    fields = gfs_fields(fhr, step)
    fields.update(overrides or {})
    data, lines, offset = b"", [], 0
    # A decoy record first so byte-range selection is actually exercised.
    decoy = np.full_like(next(iter(fields.values())), 101325.0)
    records = [("PRMSL:mean sea level", decoy, (0, 3, 1, 101, 0))]
    records += [(k, v, GRIB_IDS[k]) for k, v in fields.items()]
    for n, (key, values, ids) in enumerate(records, start=1):
        msg = grib_message(ids, values, 90, -90, 0, 360 - step, step, step,
                           date=int(cycle.strftime("%Y%m%d")), hour=cycle.hour)
        fcst = "anl" if fhr == 0 else f"{fhr} hour fcst"
        lines.append(f"{n}:{offset}:d={cycle:%Y%m%d%H}:{key}:{fcst}:")
        data += msg
        offset += len(msg)
    return data, "\n".join(lines) + "\n"


def mrms_file(step=0.1, echo_center=(-97.0, 37.0)) -> bytes:
    """MRMS-shaped CONUS grid: lat 55..20, lon 230..300 (0-360), -999 = no coverage."""
    lats = np.arange(55, 20 - step / 2, -step)
    lons = np.arange(230, 300 + step / 2, step)
    lon2, lat2 = np.meshgrid(lons - 360, lats)
    dist = np.hypot(lon2 - echo_center[0], lat2 - echo_center[1])
    refl = np.where(dist < 3, 55 - dist * 15, -99.0)  # storm cell, rest "no echo"
    refl = np.where(lon2 < -125, -999.0, refl)  # Pacific: no coverage
    # Real MRMS uses NCEP local discipline 209; read_first() doesn't care which parameter it is.
    return grib_message((0, 15, 1, 1, 0), refl, 55, 20, 230, 300, step, step, bits=12)


def s3_listing(keys: list[str]) -> bytes:
    body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    return (f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult '
            f'xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{body}</ListBucketResult>').encode()


class FakeFetcher:
    """URL -> bytes map with byte-range support; hosts in `down` raise FetchError."""

    def __init__(self, objects: dict[str, bytes] | None = None, down: set[str] | None = None):
        self.objects = objects or {}
        self.down = down or set()
        self.calls: list[str] = []

    def get(self, url, byte_range=None):
        self.calls.append(url)
        if any(host in url for host in self.down):
            raise FetchError(f"{url}: simulated outage")
        if url not in self.objects:
            raise NotFound(url)
        body = self.objects[url]
        if byte_range:
            start, end = byte_range
            return body[start: None if end is None else end + 1]
        return body
