"""Layer definitions and runtime settings.

Everything a layer needs (which GRIB2 records to fetch, how to convert them,
what range is physically plausible, and how to colour it) lives here, so
adding a variable is a config change rather than a code change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Layer:
    id: str
    name: str
    units: str
    # GRIB2 records to pull, matched against ".idx" lines as "VAR:LEVEL".
    records: tuple[str, ...]
    # Values outside this range fail QC (after unit conversion).
    valid_range: tuple[float, float]
    # Colour ramp: (value, "#rrggbb") stops, interpolated linearly.
    ramp: tuple[tuple[float, str], ...]
    # Values below this are drawn transparent (e.g. no rain, weak echoes).
    transparent_below: float | None = None
    # Forecast hours at which this record does not exist (e.g. accumulations at f000).
    skip_hours: tuple[int, ...] = ()


GFS_LAYERS: tuple[Layer, ...] = (
    Layer(
        id="temp2m",
        name="Temperature (2 m)",
        units="°C",
        records=("TMP:2 m above ground",),
        valid_range=(-95.0, 65.0),
        ramp=(
            (-40, "#2c0a59"), (-25, "#3b4cc0"), (-10, "#6f9cf6"), (0, "#cfe8f3"),
            (10, "#f7e08a"), (20, "#f4a259"), (30, "#d7301f"), (45, "#67000d"),
        ),
    ),
    Layer(
        id="wind10m",
        name="Wind speed (10 m)",
        units="m/s",
        records=("UGRD:10 m above ground", "VGRD:10 m above ground"),
        valid_range=(0.0, 110.0),
        ramp=(
            (0, "#f7fbff"), (5, "#c6dbef"), (10, "#6baed6"), (15, "#2171b5"),
            (20, "#6a51a3"), (30, "#ae017e"), (40, "#49006a"),
        ),
        transparent_below=1.0,
    ),
    Layer(
        id="precip",
        name="Precipitation rate",
        units="mm/h",
        records=("PRATE:surface",),
        valid_range=(0.0, 500.0),
        ramp=(
            (0.1, "#a1d99b"), (1, "#41ab5d"), (2.5, "#fed976"), (5, "#fd8d3c"),
            (10, "#e31a1c"), (25, "#800026"), (50, "#c51b8a"),
        ),
        transparent_below=0.1,
        skip_hours=(0,),  # PRATE is an average over the previous window; absent at analysis time.
    ),
)

RADAR_LAYER = Layer(
    id="radar",
    name="Radar reflectivity (MRMS, CONUS)",
    units="dBZ",
    records=(),
    valid_range=(-100.0, 100.0),
    ramp=(
        (5, "#04e9e7"), (15, "#019ff4"), (20, "#02fd02"), (30, "#01c501"),
        (35, "#fdf802"), (40, "#fd9500"), (45, "#fd0000"), (55, "#bc0000"),
        (65, "#f800fd"), (75, "#ffffff"),
    ),
    transparent_below=5.0,
)

ALL_LAYERS = {layer.id: layer for layer in (*GFS_LAYERS, RADAR_LAYER)}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("WTP_DATA_DIR", "data")))
    forecast_hours: tuple[int, ...] = field(
        default_factory=lambda: tuple(
            int(h) for h in os.getenv("WTP_FORECAST_HOURS", "0,6,12,18,24").split(",")
        )
    )
    # How many 6-hourly cycles to walk back when the newest one isn't published yet.
    max_cycle_lookback: int = int(os.getenv("WTP_MAX_CYCLE_LOOKBACK", "4"))
    include_radar: bool = os.getenv("WTP_INCLUDE_RADAR", "1") == "1"
    # Primary source first; later entries are failover mirrors.
    gfs_sources: tuple[str, ...] = (
        "https://noaa-gfs-bdp-pds.s3.amazonaws.com",
        "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod",
    )
    mrms_bucket: str = "https://noaa-mrms-pds.s3.amazonaws.com"
    mrms_product: str = "MergedReflectivityQCComposite_00.50"
    # Oldest radar scan we accept before declaring the radar layer stale.
    radar_max_age_minutes: int = int(os.getenv("WTP_RADAR_MAX_AGE_MIN", "30"))
    # Keep this many old runs on disk for rollback.
    keep_runs: int = int(os.getenv("WTP_KEEP_RUNS", "3"))
