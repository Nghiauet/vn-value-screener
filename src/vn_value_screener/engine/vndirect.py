"""Small, polite client for VNDirect's public finfo API (api-finfo.vndirect.com.vn/v4).

It identifies itself honestly, leaves at least MIN_INTERVAL seconds between requests,
retries on 429/5xx with backoff, and logs every response in the SQLite store (store.py), so re-running a
screen reads the stored response instead of calling the API again.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .store import Store, canonical, parse_utc, utc_from_timestamp, utc_now

SOURCE = "vndirect"
BASE_URL = "https://api-finfo.vndirect.com.vn/v4"
USER_AGENT = "stock-analyzer/0.1 (personal value-investing research)"
MIN_INTERVAL = 1.0  # seconds between requests
RETRIES = 4


class CacheMiss(Exception):
    """Offline mode asked for a response that isn't cached yet."""


class IncompleteResult(Exception):
    """A paged query lost rows to VNDirect's unstable paging; narrow it until it fits one page."""

    def __init__(self, what: str, distinct: int, total: int, pages: int):
        super().__init__(f"{what}: {distinct} distinct rows of {total} over {pages} pages")
        self.distinct, self.total, self.pages = distinct, total, pages


def row_key(row) -> str:
    """A row's identity: the whole row. Paging duplicates are byte-identical; real rows that share a
    natural key but differ elsewhere (a statement filed under two `modelType`s) are both kept."""
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def check_complete(rows: list, total: int | None, what: str = "query", pages: int = 1) -> list:
    """Drop duplicate rows; raise IncompleteResult if fewer distinct rows than `totalElements` remain."""
    if pages > 1:
        seen: set[str] = set()
        unique = []
        for row in rows:
            key = row_key(row)
            if key not in seen:
                seen.add(key)
                unique.append(row)
        rows = unique
    if total is not None and len(rows) < total:
        raise IncompleteResult(what, len(rows), total, pages)
    return rows


def url_of(path: str, params: dict, base: str = BASE_URL) -> str:
    return f"{base}/{path}?{urllib.parse.urlencode(params, safe=':~,')}"


def _age_hours(fetched_at: str) -> float:
    return (dt.datetime.now(dt.UTC) - parse_utc(fetched_at)).total_seconds() / 3600


class Client:
    """`get` returns the newest stored response of a request if it is at most `max_age_hours` old (any age
    when offline); otherwise it fetches, and the store logs the response (append-only) under `source`.
    A client for another API (vps.py) overrides `source` and `url`; the politeness rules are shared."""

    source = SOURCE

    def __init__(self, store: Store, offline: bool = False):
        self.store = store
        self.offline = offline
        self.requests_made = 0
        self.lookups = 0  # responses asked for, cached or not: what a cold run would request
        self.seen: set[tuple[str, bytes]] = set()  # distinct requests asked for (several runs on one client share)
        self.served: dict[tuple[str, bytes], str] = {}  # request → when the response it got was fetched (UTC)
        self._last_request = 0.0

    def get(self, path: str, params: dict, max_age_hours: float) -> dict:
        self.lookups += 1
        key = (path, canonical(params))
        self.seen.add(key)
        stored = self.store.latest(self.source, path, params)
        if stored and (self.offline or _age_hours(stored[1]) <= max_age_hours):
            self.served[key] = stored[1]
            return stored[0]
        if self.offline:
            raise CacheMiss(self.url(path, params))
        data = self._fetch(self.url(path, params))
        self.served[key] = utc_now()
        self.store.record(self.source, path, params, data, self.served[key])
        return data

    def url(self, path: str, params: dict) -> str:
        return url_of(path, params)

    def get_all(self, path: str, q: str, max_age_hours: float, page_size: int = 10_000, **params) -> list[dict]:
        """Every row of a paged endpoint (VNDirect pages are 1-based), complete and without duplicates.

        VNDirect pages unstably when a query spans pages: rows move between pages, so some arrive twice
        and others never, while the row count still matches `totalElements`. A repeated row therefore
        means another row is missing. Rows are de-duplicated, and if fewer distinct rows than
        `totalElements` remain, IncompleteResult is raised: the caller must narrow the query until it
        fits one page (see `fetch_window` and `fetch_codes` in data.py)."""
        rows: list[dict] = []
        page = 1
        total = None  # VNDirect sends totalElements on page 1 only; later pages omit it
        while True:
            data = self.get(path, {"q": q, "size": page_size, "page": page, **params}, max_age_hours)
            batch = data.get("data") or []
            rows.extend(batch)
            total = data.get("totalElements", total)
            if len(batch) < page_size or (total is not None and len(rows) >= total):
                break
            page += 1
        return check_complete(rows, total, f"{path}?q={q}", pages=page)

    def _fetch(self, url: str) -> dict:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        for attempt in range(RETRIES):
            wait = MIN_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    self.requests_made += 1
                    return json.loads(response.read())
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == RETRIES - 1:
                    raise
                time.sleep(float(e.headers.get("Retry-After") or 2 ** (attempt + 2)))
            except (urllib.error.URLError, TimeoutError):
                if attempt == RETRIES - 1:
                    raise
                time.sleep(2 ** (attempt + 2))
        raise RuntimeError("unreachable")


class JsonCacheClient(Client):
    """Reads the JSON cache that came before the store (data/cache/<path>-<sha1(url)[:16]>.json) and logs each
    response it serves into the store, timed by the file's mtime. Never calls the API."""

    def __init__(self, store: Store, cache_dir: Path):
        super().__init__(store, offline=True)
        self.cache_dir = cache_dir
        self.files_used: set[Path] = set()
        self.outcomes: dict[str, int] = {}

    def get(self, path: str, params: dict, max_age_hours: float) -> dict:
        self.lookups += 1
        url = url_of(path, params)
        cached = self.cache_dir / f"{path.replace('/', '_')}-{hashlib.sha1(url.encode()).hexdigest()[:16]}.json"
        if not cached.exists():
            stored = self.store.latest(SOURCE, path, params)  # already in the store from an earlier run
            if stored:
                return stored[0]
            raise CacheMiss(url)
        data = json.loads(cached.read_text())
        outcome = self.store.record(SOURCE, path, params, data, utc_from_timestamp(cached.stat().st_mtime))
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        self.files_used.add(cached)
        return data
