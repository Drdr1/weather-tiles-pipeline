"""Pipeline entry point: fetch -> decode -> QC -> COG -> publish manifest.

Publishing is all-or-nothing per GFS cycle: a cycle is staged in a
".partial" directory and only becomes visible (manifest swap) when every
layer and hour passes QC. Any failure keeps the previous good cycle live.
Radar refreshes independently and never blocks the forecast layers.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import eccodes

from .config import ALL_LAYERS, GFS_LAYERS, RADAR_LAYER, Layer, Settings
from .grib import read_fields, read_first
from .http import FetchError, Fetcher, HttpFetcher, NotFound
from .process import QCError, check, derive, qc_dict, radar_field, write_cog
from .render import bounds
from .sources import GfsSource, MrmsSource

log = logging.getLogger("pipeline")


def layer_meta(layer: Layer) -> dict:
    return {
        "name": layer.name,
        "units": layer.units,
        "ramp": [[v, c] for v, c in layer.ramp],
        "transparent_below": layer.transparent_below,
        "hours": {},
    }


def load_manifest(data_dir: Path) -> dict:
    path = data_dir / "manifest.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"gfs": None, "radar": None, "layers": {}, "qc": [], "status": {}}


def save_manifest(data_dir: Path, manifest: dict) -> None:
    """Atomic replace so readers never see a half-written manifest."""
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, data_dir / "manifest.json")


def run_gfs(settings: Settings, fetcher: Fetcher, now: datetime, manifest: dict) -> str:
    source = GfsSource(fetcher, settings)
    try:
        cycle, origin = source.resolve_cycle(now)
    except FetchError as exc:
        return f"kept previous: {exc}"

    if manifest.get("gfs") and manifest["gfs"]["cycle"] == cycle.label:
        return f"up to date ({cycle.label})"

    final = settings.data_dir / "gfs" / cycle.label
    stage = settings.data_dir / "gfs" / f"{cycle.label}.partial"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    reports, hours = [], {layer.id: {} for layer in GFS_LAYERS}

    try:
        for fhr in settings.forecast_hours:
            grib = source.download(cycle, fhr, GFS_LAYERS, origin, stage)
            if grib is None:
                raise QCError(f"f{fhr:03d}: no records")
            fields = read_fields(grib)
            for layer in GFS_LAYERS:
                if fhr in layer.skip_hours:
                    continue
                field = derive(layer, fields)
                if field is None:
                    raise QCError(f"{layer.id} f{fhr:03d}: input records missing")
                report = check(layer, fhr, field)
                reports.append(qc_dict(report))
                if not report.ok:
                    raise QCError(f"{layer.id} f{fhr:03d}: {report.reason}")
                write_cog(field, stage / layer.id / f"f{fhr:03d}.tif")
                hours[layer.id][str(fhr)] = f"gfs/{cycle.label}/{layer.id}/f{fhr:03d}.tif"
            grib.unlink()
    except (FetchError, QCError, ValueError, eccodes.CodesInternalError) as exc:
        shutil.rmtree(stage, ignore_errors=True)
        manifest["qc"] = reports
        log.error("GFS cycle %s rejected: %s", cycle.label, exc)
        return f"kept previous: cycle {cycle.label} rejected ({exc})"

    shutil.rmtree(final, ignore_errors=True)
    stage.rename(final)
    for layer in GFS_LAYERS:
        meta = layer_meta(layer)
        meta["hours"] = hours[layer.id]
        manifest["layers"][layer.id] = meta
    manifest["gfs"] = {
        "cycle": cycle.label,
        "cycle_time": cycle.time.isoformat(),
        "source": origin,
        "hours": list(settings.forecast_hours),
    }
    manifest["qc"] = reports
    prune(settings.data_dir / "gfs", settings.keep_runs)
    return f"published {cycle.label}"


def run_radar(settings: Settings, fetcher: Fetcher, now: datetime, manifest: dict) -> str:
    out_dir = settings.data_dir / "radar"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        grib, scan_time = MrmsSource(fetcher, settings).latest(now, out_dir)
        label = scan_time.strftime("%Y%m%d%H%M%S")
        if manifest.get("radar") and manifest["radar"]["scan"] == label:
            grib.unlink()
            return f"up to date ({label})"
        field = radar_field(read_first(grib))
        grib.unlink()
        # Most of the MRMS grid is legitimately empty (no coverage), so allow NaN.
        report = check(RADAR_LAYER, 0, field, max_nan_fraction=1.0)
        if not report.ok:
            raise QCError(report.reason)
        rel = f"radar/{label}.tif"
        write_cog(field, settings.data_dir / rel)
    except (FetchError, NotFound, QCError, ValueError, eccodes.CodesInternalError) as exc:
        log.warning("radar not refreshed: %s", exc)
        return f"kept previous: {exc}"

    meta = layer_meta(RADAR_LAYER)
    meta["hours"] = {"0": rel}
    manifest["layers"]["radar"] = meta
    manifest["radar"] = {"scan": label, "scan_time": scan_time.isoformat()}
    prune(out_dir, settings.keep_runs, pattern="*.tif")
    return f"published {label}"


def prune(directory: Path, keep: int, pattern: str = "[0-9]*") -> None:
    """Delete all but the newest `keep` runs (names sort chronologically)."""
    entries = sorted(p for p in directory.glob(pattern) if not p.name.endswith(".partial"))
    for old in entries[:-keep]:
        shutil.rmtree(old) if old.is_dir() else old.unlink()


def add_bounds(settings: Settings, manifest: dict) -> None:
    for layer_id, meta in manifest["layers"].items():
        any_path = next(iter(meta["hours"].values()), None)
        if any_path and (settings.data_dir / any_path).exists():
            meta["bounds"] = bounds(str(settings.data_dir / any_path))


def run_once(settings: Settings, fetcher: Fetcher, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    manifest = load_manifest(settings.data_dir)
    manifest["status"] = {"gfs": run_gfs(settings, fetcher, now, manifest)}
    if settings.include_radar:
        manifest["status"]["radar"] = run_radar(settings, fetcher, now, manifest)
    # Never list a layer whose files were pruned or never written.
    manifest["layers"] = {k: v for k, v in manifest["layers"].items() if k in ALL_LAYERS}
    add_bounds(settings, manifest)
    manifest["generated_at"] = now.isoformat()
    save_manifest(settings.data_dir, manifest)
    for step, status in manifest["status"].items():
        log.info("%s: %s", step, status)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NOAA GFS + MRMS -> COG -> map tiles")
    parser.add_argument("--loop", action="store_true", help="run forever on an interval")
    parser.add_argument("--interval", type=int, default=600, help="seconds between runs in --loop mode")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings, fetcher = Settings(), HttpFetcher()
    if not args.loop:
        manifest = run_once(settings, fetcher)
        # Non-zero if no forecast has ever been published, so CI/cron notices.
        return 0 if manifest.get("gfs") else 1

    while True:
        started = time.monotonic()
        try:
            run_once(settings, fetcher)
        except Exception:  # keep the scheduler alive; the next run retries
            log.exception("run failed")
        time.sleep(max(0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
