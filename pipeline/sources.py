"""Locate and download NOAA data.

GFS: pick the newest cycle whose last requested forecast hour is published,
then pull only the needed GRIB2 records using the ".idx" byte offsets
(a few MB per hour instead of ~500 MB per file). AWS Open Data is primary,
NOMADS is the failover mirror.

MRMS: list the day's prefix in the public bucket and take the newest scan,
rejecting it if it is older than the staleness limit.
"""
from __future__ import annotations

import gzip
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Layer, Settings
from .http import FetchError, Fetcher, NotFound

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IdxRecord:
    num: int
    start: int
    end: int | None  # inclusive; None = to end of file
    var: str
    level: str
    forecast: str

    @property
    def key(self) -> str:
        return f"{self.var}:{self.level}"


def parse_idx(text: str) -> list[IdxRecord]:
    """Parse a wgrib2-style inventory. Each record ends one byte before the next starts."""
    rows = []
    for line in text.strip().splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        rows.append((int(parts[0]), int(parts[1]), parts[3], parts[4], parts[5]))
    rows.sort(key=lambda r: r[1])
    records = []
    for i, (num, start, var, level, fcst) in enumerate(rows):
        end = rows[i + 1][1] - 1 if i + 1 < len(rows) else None
        records.append(IdxRecord(num, start, end, var, level, fcst))
    return records


def select_records(records: list[IdxRecord], wanted: tuple[str, ...]) -> list[IdxRecord]:
    """Pick the first record matching each "VAR:LEVEL". Missing records are simply absent."""
    picked = []
    for key in wanted:
        match = next((r for r in records if r.key == key), None)
        if match is not None:
            picked.append(match)
    return picked


@dataclass(frozen=True)
class Cycle:
    time: datetime  # UTC, hour in {0,6,12,18}

    @property
    def ymd(self) -> str:
        return self.time.strftime("%Y%m%d")

    @property
    def hh(self) -> str:
        return self.time.strftime("%H")

    @property
    def label(self) -> str:
        return self.time.strftime("%Y%m%d%H")


def latest_cycles(now: datetime, lookback: int) -> list[Cycle]:
    """Newest-first candidate cycles, starting at the most recent 6-hourly boundary."""
    base = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    base = base.replace(hour=base.hour - base.hour % 6)
    return [Cycle(base - timedelta(hours=6 * i)) for i in range(lookback)]


def gfs_url(source: str, cycle: Cycle, fhr: int) -> str:
    return f"{source}/gfs.{cycle.ymd}/{cycle.hh}/atmos/gfs.t{cycle.hh}z.pgrb2.0p25.f{fhr:03d}"


class GfsSource:
    def __init__(self, fetcher: Fetcher, settings: Settings):
        self.fetcher = fetcher
        self.settings = settings

    def resolve_cycle(self, now: datetime) -> tuple[Cycle, str]:
        """Newest cycle where the LAST requested hour exists, on any source.

        Checking the last hour (not f000) avoids starting a run on a cycle that
        is still being published and ending up with a partial forecast.
        """
        last_fhr = max(self.settings.forecast_hours)
        for cycle in latest_cycles(now, self.settings.max_cycle_lookback):
            for source in self.settings.gfs_sources:
                try:
                    self.fetcher.get(gfs_url(source, cycle, last_fhr) + ".idx")
                    log.info("using GFS cycle %s from %s", cycle.label, source)
                    return cycle, source
                except NotFound:
                    log.info("cycle %s not on %s yet", cycle.label, source)
                except FetchError as exc:
                    log.warning("source %s failed for %s: %s", source, cycle.label, exc)
        raise FetchError("no complete GFS cycle found on any source")

    def download(self, cycle: Cycle, fhr: int, layers: tuple[Layer, ...], preferred: str, out_dir: Path) -> Path | None:
        """Fetch the records for all layers at one forecast hour into a single GRIB2 file."""
        wanted = tuple(rec for layer in layers if fhr not in layer.skip_hours for rec in layer.records)
        sources = (preferred, *[s for s in self.settings.gfs_sources if s != preferred])
        last_exc: Exception | None = None
        for source in sources:
            url = gfs_url(source, cycle, fhr)
            try:
                idx = parse_idx(self.fetcher.get(url + ".idx").decode())
                records = select_records(idx, wanted)
                if not records:
                    log.warning("no wanted records in %s", url)
                    return None
                out = out_dir / f"gfs.f{fhr:03d}.grib2"
                with out.open("wb") as fh:
                    for rec in records:
                        fh.write(self.fetcher.get(url, (rec.start, rec.end)))
                log.info("f%03d: %d records from %s", fhr, len(records), source)
                return out
            except (NotFound, FetchError) as exc:
                last_exc = exc
                log.warning("f%03d failed on %s (%s), failing over", fhr, source, exc)
        raise FetchError(f"f{fhr:03d} unavailable on all sources: {last_exc}")


_MRMS_TS = re.compile(r"_(\d{8}-\d{6})\.grib2\.gz$")


class MrmsSource:
    def __init__(self, fetcher: Fetcher, settings: Settings):
        self.fetcher = fetcher
        self.settings = settings

    def _list(self, day: datetime) -> list[str]:
        prefix = f"CONUS/{self.settings.mrms_product}/{day:%Y%m%d}/"
        xml = self.fetcher.get(f"{self.settings.mrms_bucket}/?list-type=2&prefix={prefix}")
        root = ET.fromstring(xml)
        ns = {"s3": root.tag.split("}")[0].strip("{")} if root.tag.startswith("{") else {}
        path = ".//s3:Key" if ns else ".//Key"
        return [k.text for k in root.findall(path, ns) if k.text and k.text.endswith(".grib2.gz")]

    def latest(self, now: datetime, out_dir: Path) -> tuple[Path, datetime]:
        now = now.astimezone(timezone.utc)
        keys: list[str] = []
        # Listing is per day; just after midnight UTC today's prefix may be empty.
        for day in (now, now - timedelta(days=1)):
            keys = self._list(day)
            if keys:
                break
        if not keys:
            raise FetchError("no MRMS scans listed")
        # Note: a full day (720 scans) fits in one 1000-key listing page.
        key = max(keys)
        m = _MRMS_TS.search(key)
        if not m:
            raise FetchError(f"unexpected MRMS key {key}")
        scan_time = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        age = now - scan_time
        if age > timedelta(minutes=self.settings.radar_max_age_minutes):
            raise FetchError(f"newest MRMS scan is stale ({age} old)")
        raw = gzip.decompress(self.fetcher.get(f"{self.settings.mrms_bucket}/{key}"))
        out = out_dir / "mrms.grib2"
        out.write_bytes(raw)
        log.info("radar scan %s (%s old)", scan_time.isoformat(), age)
        return out, scan_time
