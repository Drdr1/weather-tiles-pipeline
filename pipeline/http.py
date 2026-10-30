"""Small HTTP layer: retries with backoff, byte ranges, and a seam for tests.

Everything that touches the network goes through a `Fetcher`, so the whole
pipeline can be exercised offline with a fake one.
"""
from __future__ import annotations

import logging
import time
from typing import Protocol

import requests

log = logging.getLogger(__name__)


class NotFound(Exception):
    """The object does not exist (yet). Not retried: it means "try another cycle/source"."""


class FetchError(Exception):
    """The source failed after all retries. Callers fail over to the next source."""


class Fetcher(Protocol):
    def get(self, url: str, byte_range: tuple[int, int | None] | None = None) -> bytes: ...


class HttpFetcher:
    def __init__(self, retries: int = 3, backoff: float = 2.0, timeout: float = 60.0):
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "weather-tiles-pipeline/0.1 (+https://github.com/Drdr1/weather-tiles-pipeline)"

    def get(self, url: str, byte_range: tuple[int, int | None] | None = None) -> bytes:
        headers = {}
        if byte_range is not None:
            start, end = byte_range
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
                if resp.status_code == 404 or (resp.status_code == 403 and "amazonaws" in url):
                    # S3 returns 403 for missing keys on buckets without public list.
                    raise NotFound(url)
                if resp.status_code in (200, 206):
                    return resp.content
                last_exc = FetchError(f"{url}: HTTP {resp.status_code}")
            except NotFound:
                raise
            except requests.RequestException as exc:
                last_exc = exc
            if attempt < self.retries:
                delay = self.backoff ** attempt
                log.warning("fetch failed (%s), retry %d/%d in %.0fs", last_exc, attempt, self.retries, delay)
                time.sleep(delay)
        raise FetchError(str(last_exc))
