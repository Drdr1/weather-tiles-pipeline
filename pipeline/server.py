"""Dynamic tile server: renders tiles on demand from the published COGs.

Put a caching proxy (nginx in docker-compose) in front of it: tiles are
immutable for a given cycle, so the cache key includes the cycle.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles

from .config import ALL_LAYERS, Settings
from .render import EMPTY_TILE, render_tile

settings = Settings()
app = FastAPI(title="weather-tiles-pipeline")
MAX_ZOOM = 10
STALE_AFTER_HOURS = 14  # a cycle is ~4-10h old in normal operation; older means runs are failing


def _manifest() -> dict:
    path = settings.data_dir / "manifest.json"
    if not path.exists():
        raise HTTPException(503, "no data published yet")
    return json.loads(path.read_text())


@app.get("/manifest.json")
def manifest() -> Response:
    m = _manifest()
    m["tiles"] = "/tiles/{layer}/{hour}/{z}/{x}/{y}.png"
    m["tile_maxzoom"] = MAX_ZOOM
    return Response(json.dumps(m), media_type="application/json", headers={"Cache-Control": "no-cache"})


@app.get("/tiles/{layer_id}/{hour}/{z}/{x}/{y}.png")
def tile(layer_id: str, hour: int, z: int, x: int, y: int) -> Response:
    layer = ALL_LAYERS.get(layer_id)
    meta = _manifest()["layers"].get(layer_id)
    if layer is None or meta is None:
        raise HTTPException(404, "unknown layer")
    rel = meta["hours"].get(str(hour))
    if rel is None:
        raise HTTPException(404, "hour not available for this layer")
    if not (0 <= z <= MAX_ZOOM and 0 <= x < 2**z and 0 <= y < 2**z):
        raise HTTPException(400, "tile out of range")
    body = render_tile(str(settings.data_dir / rel), layer, z, x, y) or EMPTY_TILE
    # The COG path embeds the cycle/scan, so a tile URL's content can only change
    # when the manifest points somewhere new. Short max-age keeps radar fresh.
    return Response(body, media_type="image/png", headers={"Cache-Control": "public, max-age=300", "ETag": f'"{rel}-{z}-{x}-{y}"'})


@app.get("/healthz")
def healthz() -> dict:
    m = _manifest()
    if not m.get("gfs"):
        raise HTTPException(503, "no forecast published")
    age = datetime.now(timezone.utc) - datetime.fromisoformat(m["gfs"]["cycle_time"])
    hours = age.total_seconds() / 3600
    if hours > STALE_AFTER_HOURS:
        raise HTTPException(503, f"forecast cycle is {hours:.1f}h old")
    return {"status": "ok", "gfs_cycle": m["gfs"]["cycle"], "cycle_age_hours": round(hours, 1), "radar": m.get("radar")}


web_dir = Path(os.getenv("WTP_WEB_DIR", Path(__file__).resolve().parent.parent / "web"))
if web_dir.exists():
    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
