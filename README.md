# weather-tiles-pipeline

NOAA weather data to an interactive web map: scheduled ingestion of **GFS** forecast
output (GRIB2) and **MRMS** radar from AWS Open Data, unit conversion and quality
control, **Cloud-Optimized GeoTIFFs**, and **Web Mercator tiles** shown in a
**MapLibre** front end. It runs as a Docker Compose stack with a tile server and
cache, or serverless as a static site on GitHub Pages.

**Live demo:** https://drdr1.github.io/weather-tiles-pipeline/ (rebuilt after every GFS cycle)

```
NOAA AWS Open Data ──┐                  ┌─ QC gate ─┐
  (primary)          ├─► byte-range ──► decode ──► convert ──► COG (EPSG:4326) ──► manifest.json
NOMADS (failover) ───┘   GRIB2 fetch    ecCodes    K→°C, u/v→speed,          (atomic swap)
                                                    kg m⁻² s⁻¹→mm/h                │
MRMS radar (newest scan, staleness check) ─────────────────────────────────────────┤
                                                                                   ▼
                                     tile server (FastAPI + rio-tiler) ◄── nginx cache ◄── MapLibre
                                     static export (z0–4 PNGs) ──────────────────────────► GitHub Pages
```

## Layers

| Layer | Source | Records | Notes |
|---|---|---|---|
| Temperature 2 m | GFS 0.25° | `TMP:2 m above ground` | K → °C |
| Wind speed 10 m | GFS 0.25° | `UGRD` + `VGRD` at 10 m | √(u² + v²) |
| Precipitation rate | GFS 0.25° | `PRATE:surface` | → mm/h; absent at f000 by design |
| Radar reflectivity | MRMS CONUS | `MergedReflectivityQCComposite` | −999/−99 sentinels → transparent |

Adding a variable means adding a `Layer` in `pipeline/config.py` (records, valid range, colour ramp) and a line in `derive()`.

## What it handles

**Efficient ingest.** GFS publishes one ~500 MB GRIB2 file per forecast hour. The
pipeline reads the `.idx` inventory and downloads only the needed records with HTTP
range requests, a few MB per hour.

**Cycle selection.** A cycle is used only once its *last* requested forecast hour is
published. That avoids starting on a cycle that is still being written and ending up
with half a forecast. If the newest cycle isn't complete, it walks back to the previous one.

**Failover.**
- **Source:** AWS Open Data is primary. NOMADS takes over per request when S3 errors.
- **Retries:** exponential backoff on transient HTTP failures. A 404 is not retried; it means "not published yet".
- **Radar:** a radar outage never blocks the forecast layers, and vice versa.

**Quality control.** Every field is checked before publishing:
- missing-data fraction
- physically plausible range (e.g. 2 m temperature within −95…65 °C)
- non-constant fields (a flat temperature grid means a bad decode)
- radar scan staleness

**All-or-nothing publishing.** A cycle is staged in `gfs/<cycle>.partial/` and only
becomes visible through an atomic swap of `manifest.json` when every layer and hour
passes. A failure keeps the previous good cycle live, and the QC report is written
to the manifest and shown in the UI.

**Grid handling.** GRIB2 decoding uses ecCodes directly:
- scanning direction (north→south vs south→north)
- 0–360° longitudes (GFS global grid rolled to −180…180; MRMS shifted by −360)
- bitmap-masked missing values
- pole rows dropped before Web Mercator

**Serving.** Float32 DEFLATE COGs with internal overviews are coloured on demand by
rio-tiler. nginx caches tiles, with cache locking so a burst of requests renders each
tile once. `/healthz` returns 503 if the published cycle is older than 14 h, so
monitoring catches a stuck pipeline.

## Run it

```bash
docker compose up -d --build
open http://localhost:8080
```

The worker checks for new data every 10 minutes and keeps the last 3 cycles on disk
for rollback. Forecast hours are set with `WTP_FORECAST_HOURS`.

Without Docker:

```bash
pip install -e ".[dev]"
python -m pipeline.run                                    # one run into ./data
uvicorn pipeline.server:app --port 8000                   # dynamic tiles + map
python -m pipeline.export --out site --max-zoom 4         # or: static site
```

## Tests

```bash
pytest -q
```

The 31 tests run offline against **synthetic GRIB2 files encoded with ecCodes** in the
same grid layouts and parameter identities as GFS and MRMS. They exercise the real
decoder, not mocks. Coverage:
- `.idx` byte ranges
- cycle walk-back and NOMADS failover
- MRMS newest-scan, midnight rollover and staleness
- scanning order, longitude normalisation and bitmap handling
- unit conversions and QC rejections
- COG round-trip
- an end-to-end run where a corrupted cycle is rejected and the previous one stays live
- tile server status codes, static export and pruning

CI also runs a **live smoke test** against the real NOAA buckets: it publishes a cycle,
asserts QC passes and renders a tile from every layer, which catches upstream format changes.

## Scope and limits

- **Not included:** satellite imagery (GOES/Himawari) and AI weather models (GraphCast, Pangu, FourCastNet). Both would be new `Layer` sources feeding the same COG → tile path.
- **Static demo resolution:** tiles are pre-rendered to zoom 4 and overzoomed beyond it. That's fine for 0.25° GFS but softens radar detail. The Docker stack renders at any zoom.
- **Radar freshness on the demo:** the radar layer is a snapshot taken at build time (every 6 h). The Docker stack refreshes it every 10 minutes.
- **Basemap:** Esri World Dark Gray canvas tiles (no API key), with attribution.

Data: NOAA GFS and MRMS via the [NOAA Open Data Dissemination program](https://registry.opendata.aws/collab/noaa/) on AWS.

## License

MIT
