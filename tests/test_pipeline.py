"""End to end: fake NOAA -> run_once -> manifest -> tiles (server and static export)."""
import gzip
import io
import json
from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pipeline import run, server
from pipeline.config import Settings
from pipeline.export import export
from pipeline.render import colorize, lut
from pipeline.config import ALL_LAYERS
from pipeline.sources import Cycle, gfs_url

from .synth import FakeFetcher, gfs_file, mrms_file, s3_listing

NOW = datetime(2026, 10, 30, 9, 40, tzinfo=timezone.utc)
HOURS = (0, 6, 12)


def noaa(settings, cycles=("2026103000",), overrides=None, radar=True):
    objects = {}
    for label in cycles:
        cyc = Cycle(datetime.strptime(label, "%Y%m%d%H").replace(tzinfo=timezone.utc))
        for fhr in HOURS:
            data, idx = gfs_file(fhr, step=2.0, cycle=cyc.time, overrides=(overrides or {}).get(fhr))
            url = gfs_url(settings.gfs_sources[0], cyc, fhr)
            objects[url], objects[url + ".idx"] = data, idx.encode()
    if radar:
        prod = settings.mrms_product
        key = f"CONUS/{prod}/20261030/MRMS_{prod}_20261030-093640.grib2.gz"
        objects[f"{settings.mrms_bucket}/?list-type=2&prefix=CONUS/{prod}/20261030/"] = s3_listing([key])
        objects[f"{settings.mrms_bucket}/{key}"] = gzip.compress(mrms_file(step=0.25))
    return objects


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", forecast_hours=HOURS)


def test_publishes_full_cycle(settings):
    m = run.run_once(settings, FakeFetcher(noaa(settings)), NOW)
    assert m["gfs"]["cycle"] == "2026103000"
    assert m["status"]["gfs"] == "published 2026103000"
    assert m["status"]["radar"].startswith("published")
    assert sorted(m["layers"]) == ["precip", "radar", "temp2m", "wind10m"]
    assert list(m["layers"]["precip"]["hours"]) == ["6", "12"]  # no PRATE at f000
    assert all(q["ok"] for q in m["qc"])
    for meta in m["layers"].values():
        for rel in meta["hours"].values():
            assert (settings.data_dir / rel).exists()
    w, s, e, n = m["layers"]["radar"]["bounds"]
    assert -131 < w < -129 and 54 < n < 56


def test_second_run_is_a_noop(settings):
    fetcher = FakeFetcher(noaa(settings))
    run.run_once(settings, fetcher, NOW)
    m = run.run_once(settings, fetcher, NOW)
    assert m["status"]["gfs"] == "up to date (2026103000)"
    assert m["status"]["radar"].startswith("up to date")


def test_bad_cycle_keeps_previous_good_one(settings):
    good = noaa(settings, cycles=("2026103000",))
    run.run_once(settings, FakeFetcher(good), NOW)

    # The 06z cycle arrives with a corrupted f012 temperature (all 400 K).
    bad_temp = {"TMP:2 m above ground": np.full((91, 180), 400.0)}
    newer = noaa(settings, cycles=("2026103006",), overrides={12: bad_temp}, radar=False)
    later = datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc)
    m = run.run_once(settings, FakeFetcher({**good, **newer}), later)

    assert m["gfs"]["cycle"] == "2026103000"  # still serving the good cycle
    assert "rejected" in m["status"]["gfs"]
    assert any(not q["ok"] for q in m["qc"])
    assert not list((settings.data_dir / "gfs").glob("*.partial"))
    assert not (settings.data_dir / "gfs" / "2026103006").exists()


def test_radar_outage_does_not_block_forecast(settings):
    m = run.run_once(settings, FakeFetcher(noaa(settings, radar=False)), NOW)
    assert m["gfs"]["cycle"] == "2026103000"
    assert m["status"]["radar"].startswith("kept previous")
    assert "radar" not in m["layers"]


def test_prune_keeps_newest_runs(settings):
    settings.keep_runs = 1
    run.run_once(settings, FakeFetcher(noaa(settings, cycles=("2026103000",))), NOW)
    both = noaa(settings, cycles=("2026103000", "2026103006"))
    m = run.run_once(settings, FakeFetcher(both), datetime(2026, 10, 30, 13, 0, tzinfo=timezone.utc))
    assert m["gfs"]["cycle"] == "2026103006"
    assert [p.name for p in (settings.data_dir / "gfs").iterdir()] == ["2026103006"]


def test_tile_server(settings, monkeypatch):
    run.run_once(settings, FakeFetcher(noaa(settings)), NOW)
    monkeypatch.setattr(server, "settings", settings)
    c = TestClient(server.app)

    m = c.get("/manifest.json").json()
    assert m["tiles"].startswith("/tiles/")

    r = c.get("/tiles/temp2m/6/0/0/0.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = np.array(Image.open(io.BytesIO(r.content)))
    assert img.shape == (256, 256, 4) and img[128, 128, 3] == 255

    # Radar over the Atlantic (outside MRMS bounds): transparent, not an error.
    r = c.get("/tiles/radar/0/4/7/5.png")
    assert r.status_code == 200
    assert np.array(Image.open(io.BytesIO(r.content)))[..., 3].max() == 0

    assert c.get("/tiles/precip/0/0/0/0.png").status_code == 404  # no f000 precip
    assert c.get("/tiles/nope/0/0/0/0.png").status_code == 404
    assert c.get("/tiles/temp2m/6/2/9/0.png").status_code == 400


def test_healthz_reports_stale(settings, monkeypatch):
    run.run_once(settings, FakeFetcher(noaa(settings)), NOW)
    monkeypatch.setattr(server, "settings", settings)
    r = TestClient(server.app).get("/healthz")
    # Synthetic cycle is from 2026-10-30 00z; how stale it is depends on the wall clock.
    assert r.status_code in (200, 503)
    assert ("gfs_cycle" in r.json()) if r.status_code == 200 else ("old" in r.json()["detail"])


def test_static_export(settings, tmp_path):
    run.run_once(settings, FakeFetcher(noaa(settings)), NOW)
    out = tmp_path / "site"
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html></html>")
    n = export(settings.data_dir, out, max_zoom=2, web_dir=web)
    m = json.loads((out / "manifest.json").read_text())
    assert m["tiles"] == "tiles/{layer}/{hour}/{z}/{x}/{y}.png" and m["tile_maxzoom"] == 2
    assert (out / "index.html").exists()
    assert (out / "tiles/temp2m/6/0/0/0.png").exists()
    # Global layers: 1 + 4 + 16 tiles per hour. Radar only where CONUS is.
    assert len(list((out / "tiles/temp2m/6").rglob("*.png"))) == 21
    assert 0 < len(list((out / "tiles/radar/0").rglob("*.png"))) < 21
    assert n > 0


def test_colorize_transparency_and_ramp_ends():
    layer = ALL_LAYERS["precip"]
    rgba = colorize(layer, np.array([[np.nan, 0.0, 0.1, 1e6]], dtype=np.float32))
    assert list(rgba[0, :, 3]) == [0, 0, 255, 255]
    table, lo, hi = lut(layer)
    assert tuple(rgba[0, 2, :3]) == tuple(table[0]) and tuple(rgba[0, 3, :3]) == tuple(table[-1])
