import gzip
from datetime import datetime, timezone

import pytest

from pipeline.config import GFS_LAYERS, Settings
from pipeline.http import FetchError
from pipeline.sources import Cycle, GfsSource, MrmsSource, gfs_url, latest_cycles, parse_idx, select_records

from .synth import FakeFetcher, gfs_file, s3_listing

S3, NOMADS = Settings().gfs_sources
NOW = datetime(2026, 10, 30, 9, 40, tzinfo=timezone.utc)


def test_parse_idx_ranges_are_contiguous_and_last_is_open():
    text = "1:0:d=2026103000:PRMSL:mean sea level:anl:\n2:100:d=2026103000:TMP:2 m above ground:anl:\n3:250:d=2026103000:UGRD:10 m above ground:anl:\n"
    recs = parse_idx(text)
    assert [(r.start, r.end) for r in recs] == [(0, 99), (100, 249), (250, None)]
    assert recs[1].key == "TMP:2 m above ground"


def test_select_records_ignores_missing():
    recs = parse_idx("1:0:d=x:TMP:2 m above ground:anl:\n2:9:d=x:UGRD:10 m above ground:anl:\n")
    picked = select_records(recs, ("UGRD:10 m above ground", "PRATE:surface"))
    assert [r.var for r in picked] == ["UGRD"]


def test_latest_cycles_floor_to_six_hours():
    cycles = latest_cycles(NOW, 3)
    assert [c.label for c in cycles] == ["2026103006", "2026103000", "2026102918"]


def _publish(objects, source, cycle_label, hours):
    cycle = Cycle(datetime.strptime(cycle_label, "%Y%m%d%H").replace(tzinfo=timezone.utc))
    for fhr in hours:
        data, idx = gfs_file(fhr, step=10.0, cycle=cycle.time)
        objects[gfs_url(source, cycle, fhr)] = data
        objects[gfs_url(source, cycle, fhr) + ".idx"] = idx.encode()
    return cycle


def test_resolve_skips_cycle_still_being_published():
    objects = {}
    _publish(objects, S3, "2026103000", [0, 6, 12])
    _publish(objects, S3, "2026103006", [0, 6])  # newest cycle, f012 not out yet
    settings = Settings(forecast_hours=(0, 6, 12))
    cycle, source = GfsSource(FakeFetcher(objects), settings).resolve_cycle(NOW)
    assert (cycle.label, source) == ("2026103000", S3)


def test_resolve_fails_over_to_nomads_when_s3_is_down():
    objects = {}
    _publish(objects, NOMADS, "2026103006", [0, 6])
    settings = Settings(forecast_hours=(0, 6))
    cycle, source = GfsSource(FakeFetcher(objects, down={"amazonaws"}), settings).resolve_cycle(NOW)
    assert (cycle.label, source) == ("2026103006", NOMADS)


def test_resolve_raises_when_nothing_published():
    with pytest.raises(FetchError):
        GfsSource(FakeFetcher({}), Settings(forecast_hours=(0,))).resolve_cycle(NOW)


def test_download_pulls_only_wanted_records(tmp_path):
    objects = {}
    cycle = _publish(objects, S3, "2026103000", [6])
    fetcher = FakeFetcher(objects)
    out = GfsSource(fetcher, Settings()).download(cycle, 6, GFS_LAYERS, S3, tmp_path)
    full = objects[gfs_url(S3, cycle, 6)]
    # Decoy PRMSL record is skipped, so we downloaded less than the full file.
    assert 0 < out.stat().st_size < len(full)
    from pipeline.grib import read_fields
    assert set(read_fields(out)) == {"TMP:2 m above ground", "UGRD:10 m above ground",
                                     "VGRD:10 m above ground", "PRATE:surface"}


def test_download_fails_over_midway(tmp_path):
    objects = {}
    cycle = _publish(objects, NOMADS, "2026103000", [0])
    out = GfsSource(FakeFetcher(objects, down={"amazonaws"}), Settings()).download(cycle, 0, GFS_LAYERS, S3, tmp_path)
    assert out is not None and out.exists()


def _mrms_objects(settings, keys, payload=b"GRIB"):
    objects = {}
    for day in ("20261030", "20261029"):
        prefix = f"CONUS/{settings.mrms_product}/{day}/"
        objects[f"{settings.mrms_bucket}/?list-type=2&prefix={prefix}"] = s3_listing([k for k in keys if prefix in k])
    for k in keys:
        objects[f"{settings.mrms_bucket}/{k}"] = gzip.compress(payload)
    return objects


def test_mrms_takes_newest_scan(tmp_path):
    s = Settings()
    keys = [f"CONUS/{s.mrms_product}/20261030/MRMS_{s.mrms_product}_20261030-09{m:02d}40.grib2.gz" for m in (20, 30, 36)]
    path, scan = MrmsSource(FakeFetcher(_mrms_objects(s, keys)), s).latest(NOW, tmp_path)
    assert scan == datetime(2026, 10, 30, 9, 36, 40, tzinfo=timezone.utc)
    assert path.read_bytes() == b"GRIB"


def test_mrms_falls_back_to_yesterday_just_after_midnight(tmp_path):
    s = Settings()
    keys = [f"CONUS/{s.mrms_product}/20261029/MRMS_{s.mrms_product}_20261029-235840.grib2.gz"]
    now = datetime(2026, 10, 30, 0, 5, tzinfo=timezone.utc)
    _, scan = MrmsSource(FakeFetcher(_mrms_objects(s, keys)), s).latest(now, tmp_path)
    assert scan.day == 29


def test_mrms_rejects_stale_scan(tmp_path):
    s = Settings()
    keys = [f"CONUS/{s.mrms_product}/20261030/MRMS_{s.mrms_product}_20261030-070000.grib2.gz"]
    with pytest.raises(FetchError, match="stale"):
        MrmsSource(FakeFetcher(_mrms_objects(s, keys)), s).latest(NOW, tmp_path)
