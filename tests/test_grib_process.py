import numpy as np
import pytest
import rasterio

from pipeline.config import ALL_LAYERS
from pipeline.grib import GRIB_IDS, read_fields, read_first
from pipeline.process import check, derive, radar_field, write_cog

from .synth import gfs_file, grib_message, mrms_file


def _write(tmp_path, data: bytes, name="x.grib2"):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_global_grid_is_rolled_to_minus180_and_poles_dropped(tmp_path):
    data, _ = gfs_file(6, step=1.0)
    f = read_fields(_write(tmp_path, data))["TMP:2 m above ground"]
    assert f.shape == (179, 360)  # 181 rows minus both pole rows
    west, north = f.transform @ (0, 0)
    assert west == pytest.approx(-180.5) and north == pytest.approx(89.5)
    # Synthetic field adds 3*sin(lon) K: after the roll, column 270 is 90 E (warm
    # bump) and column 90 is 90 W (cold dip). Without the roll they'd be swapped.
    row = f.data[89]
    assert row[270] - row[90] > 5


def test_south_to_north_scanning_is_flipped(tmp_path):
    vals = np.arange(4 * 6, dtype=float).reshape(4, 6)  # north-up
    msg = grib_message(GRIB_IDS["TMP:2 m above ground"], vals, 40, 10, 10, 60, 10, 10, south_to_north=True)
    f = read_fields(_write(tmp_path, msg))["TMP:2 m above ground"]
    np.testing.assert_allclose(f.data, vals, atol=0.01)
    assert f.transform @ (0, 0) == pytest.approx((5, 45))


def test_bitmap_missing_becomes_nan(tmp_path):
    vals = np.full((3, 3), 280.0)
    mask = np.zeros((3, 3), bool)
    mask[1, 1] = True
    msg = grib_message(GRIB_IDS["TMP:2 m above ground"], vals, 30, 10, 0, 20, 10, 10, missing_mask=mask)
    f = read_fields(_write(tmp_path, msg))["TMP:2 m above ground"]
    assert np.isnan(f.data[1, 1]) and np.isfinite(f.data).sum() == 8


def test_mrms_longitudes_and_sentinels(tmp_path):
    raw = read_first(_write(tmp_path, mrms_file()))
    west, north = raw.transform @ (0, 0)
    assert west == pytest.approx(-130.05) and north == pytest.approx(55.05)
    field = radar_field(raw)
    assert np.isnan(field.data[:, :40]).all()  # -999 Pacific -> NaN
    assert np.nanmax(field.data) > 50  # storm cell survives


def test_unit_conversions(tmp_path):
    fields = read_fields(_write(tmp_path, gfs_file(6, step=10.0)[0]))
    t = derive(ALL_LAYERS["temp2m"], fields)
    raw = fields["TMP:2 m above ground"].data
    np.testing.assert_allclose(t.data, raw - 273.15, atol=1e-4)
    w = derive(ALL_LAYERS["wind10m"], fields)
    u, v = fields["UGRD:10 m above ground"].data, fields["VGRD:10 m above ground"].data
    np.testing.assert_allclose(w.data, np.hypot(u, v), atol=1e-4)
    p = derive(ALL_LAYERS["precip"], fields)
    np.testing.assert_allclose(p.data, fields["PRATE:surface"].data * 3600, atol=1e-3)


def test_precip_absent_at_analysis_time(tmp_path):
    fields = read_fields(_write(tmp_path, gfs_file(0, step=10.0)[0]))
    assert derive(ALL_LAYERS["precip"], fields) is None


@pytest.mark.parametrize("data,reason", [
    (np.full((4, 4), np.nan, np.float32), "missing"),
    (np.full((4, 4), 500.0, np.float32), "outside"),
    (np.full((4, 4), 12.0, np.float32), "constant"),
])
def test_qc_rejects_bad_fields(data, reason):
    from pipeline.grib import Field
    from rasterio.transform import from_origin
    rep = check(ALL_LAYERS["temp2m"], 0, Field("temp2m", data, from_origin(0, 10, 1, 1)))
    assert not rep.ok and reason in rep.reason


def test_qc_passes_real_looking_field(tmp_path):
    fields = read_fields(_write(tmp_path, gfs_file(6, step=10.0)[0]))
    rep = check(ALL_LAYERS["temp2m"], 6, derive(ALL_LAYERS["temp2m"], fields))
    assert rep.ok, rep.reason


def test_cog_roundtrip(tmp_path):
    fields = read_fields(_write(tmp_path, gfs_file(6, step=0.25)[0]))  # real GFS size
    field = derive(ALL_LAYERS["temp2m"], fields)
    out = write_cog(field, tmp_path / "t" / "f006.tif")
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326
        assert ds.profile["tiled"] and ds.overviews(1)
        np.testing.assert_allclose(ds.read(1), field.data, atol=1e-5)
        assert ds.transform.almost_equals(field.transform)
