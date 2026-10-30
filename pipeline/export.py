"""Pre-render low-zoom tiles to static files for serverless hosting (GitHub Pages).

The map overzooms past --max-zoom, which is fine at GFS's 0.25° (~28 km)
resolution. Radar loses detail beyond the exported zoom; the dynamic server
has no such limit.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
from pathlib import Path

import mercantile

from .config import ALL_LAYERS, Settings
from .render import render_tile

log = logging.getLogger("export")


def export(data_dir: Path, out_dir: Path, max_zoom: int, web_dir: Path | None = None) -> int:
    manifest = json.loads((data_dir / "manifest.json").read_text())
    if web_dir and web_dir.exists():
        shutil.copytree(web_dir, out_dir, dirs_exist_ok=True)
    tiles_root = out_dir / "tiles"
    shutil.rmtree(tiles_root, ignore_errors=True)
    written = 0
    for layer_id, meta in manifest["layers"].items():
        layer = ALL_LAYERS[layer_id]
        w, s, e, n = meta.get("bounds", (-180, -85.0511, 180, 85.0511))
        for hour, rel in meta["hours"].items():
            cog = str(data_dir / rel)
            for t in mercantile.tiles(w, s, e, n, zooms=range(0, max_zoom + 1)):
                body = render_tile(cog, layer, t.z, t.x, t.y)
                if body is None:
                    continue
                path = tiles_root / layer_id / hour / str(t.z) / str(t.x) / f"{t.y}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                written += 1
        log.info("%s: exported %d hours", layer_id, len(meta["hours"]))
    manifest["tiles"] = "tiles/{layer}/{hour}/{z}/{x}/{y}.png"
    manifest["tile_maxzoom"] = max_zoom
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export static XYZ tiles + web app")
    parser.add_argument("--out", type=Path, default=Path("site"))
    parser.add_argument("--max-zoom", type=int, default=4)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    web = Path(os.getenv("WTP_WEB_DIR", Path(__file__).resolve().parent.parent / "web"))
    n = export(Settings().data_dir, args.out, args.max_zoom, web)
    log.info("wrote %d tiles to %s", n, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
