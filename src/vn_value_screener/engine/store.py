"""SQLite data store (`data/stock.db`): an append-only log of every API response, plus tables built from it.

Raw layer (never updated or deleted; triggers enforce it):
- `raw_requests`  one row per distinct request: source, endpoint, canonical JSON params.
- `raw_responses` one row per distinct body of a request: sha256 of the canonical JSON, zlib-compressed body,
                  and when that body was first fetched. A body identical to the request's latest one is not
                  stored again.
- `raw_fetches`   one row per fetch (UTC time → the response it returned), so a re-seen body still records
                  the date it was last confirmed.

Normalized layer (rebuilt from raw at any time with `rebuild()`; filled as responses arrive):
- `listings`      one row per ticker *listing period* (ticker reuse such as VPL 2011 vs 2025, floor moves).
- `daily_prices`  close / adjusted close in VND, volume, traded value, per source, ticker and day.
- `statements`    one row per distinct value of (source, ticker, report type, model type, fiscal date, item):
                  a restated figure becomes a new row with its own first_seen_at, so the first-reported value
                  is kept. `first_seen_at` / `last_seen_at` are our fetch times, not publication dates.
- `market_caps`   VNDirect's MARKETCAP per ticker and day (VND).

Paper portfolio (paper.py; migration 3): `paper_cohorts` / `paper_members`, the screen's lists frozen on a screen
date. Records, not derived from the raw log: `rebuild()` keeps them, and triggers stop any UPDATE or DELETE.

Filings studies (filings/; migration 4): `filing_figures` (figures read in audited filings, V/I, page, method),
`filing_findings` (what the readers found, by checklist category) and `graham_scorecards` (one row per
`filings graham` run). Records, not derived from the raw log: `rebuild()` keeps them.

Bulk report database (filings/bulk.py; migration 5): `filing_reports` (one row per audited report read by `filings
bulk`: auditor's opinion, scope, template, pages, identity results) and `filing_lines` (every line of its three
statements, both columns, with a check status), views `filing_lines_latest` and `filing_restatements`.

Sources (the `source` column): 'vndirect' for everything the screens use; 'vps' (vps.py) for the backtest's
VN-Index and, opt-in, stocks' adjusted prices; kept in the raw layer and not normalized (index points, and
adjusted-only prices that have no raw close).

The JSON cache in data/cache/ is replaced by this store: `Client.get` reads the latest stored response of a
request instead of a cache file. `import_json_cache` loads an old cache once.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import zlib
from collections.abc import Iterable
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: raw layer
    """
    CREATE TABLE raw_requests (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        params TEXT NOT NULL,                 -- canonical JSON (sorted keys)
        UNIQUE (source, endpoint, params)
    );
    CREATE TABLE raw_responses (
        id INTEGER PRIMARY KEY,
        request_id INTEGER NOT NULL REFERENCES raw_requests(id),
        fetched_at TEXT NOT NULL,             -- UTC, first fetch that returned this body
        sha256 TEXT NOT NULL,                 -- of the canonical JSON body
        body BLOB NOT NULL                    -- zlib(canonical JSON)
    );
    CREATE INDEX raw_responses_request ON raw_responses(request_id);
    CREATE TABLE raw_fetches (
        id INTEGER PRIMARY KEY,
        request_id INTEGER NOT NULL REFERENCES raw_requests(id),
        response_id INTEGER NOT NULL REFERENCES raw_responses(id),
        fetched_at TEXT NOT NULL              -- UTC
    );
    CREATE INDEX raw_fetches_request ON raw_fetches(request_id, fetched_at);
    CREATE TRIGGER raw_requests_append_only_u BEFORE UPDATE ON raw_requests
        BEGIN SELECT RAISE(ABORT, 'raw layer is append-only'); END;
    CREATE TRIGGER raw_requests_append_only_d BEFORE DELETE ON raw_requests
        BEGIN SELECT RAISE(ABORT, 'raw layer is append-only'); END;
    CREATE TRIGGER raw_responses_append_only_u BEFORE UPDATE ON raw_responses
        BEGIN SELECT RAISE(ABORT, 'raw layer is append-only'); END;
    CREATE TRIGGER raw_responses_append_only_d BEFORE DELETE ON raw_responses
        BEGIN SELECT RAISE(ABORT, 'raw layer is append-only'); END;
    CREATE TRIGGER raw_fetches_append_only_u BEFORE UPDATE ON raw_fetches
        BEGIN SELECT RAISE(ABORT, 'raw layer is append-only'); END;
    CREATE TRIGGER raw_fetches_append_only_d BEFORE DELETE ON raw_fetches
        BEGIN SELECT RAISE(ABORT, 'raw layer is append-only'); END;
    """,
    # 2: normalized layer
    """
    CREATE TABLE listings (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        ticker TEXT NOT NULL,
        period_key TEXT NOT NULL,             -- listed_date, or '..' || delisted_date when the start is unknown
        listed_date TEXT,
        delisted_date TEXT,                   -- NULL while listed
        exchange TEXT,                        -- NULL when unknown (an earlier period only known by its end)
        status TEXT,
        type TEXT,
        company_id TEXT,
        isin TEXT,
        company_name TEXT,
        short_name TEXT,
        face_value REAL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE (source, ticker, period_key)
    );
    CREATE TABLE daily_prices (
        source TEXT NOT NULL,
        ticker TEXT NOT NULL,
        date TEXT NOT NULL,
        close REAL,                           -- VND (VNDirect serves thousands; stored × 1000)
        ad_close REAL,                        -- VND, adjusted as of fetched_at (rescaled by later events)
        volume REAL,                          -- shares, order-matched
        value REAL,                           -- VND, order-matched
        exchange TEXT,
        type TEXT,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (source, ticker, date)
    ) WITHOUT ROWID;
    CREATE INDEX daily_prices_date ON daily_prices(date);
    CREATE TABLE statements (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        ticker TEXT NOT NULL,
        report_type TEXT NOT NULL,            -- ANNUAL, QUARTER, ...
        model_type INTEGER NOT NULL,          -- VNDirect template; one figure can be filed under two
        fiscal_date TEXT NOT NULL,
        item_code INTEGER NOT NULL,
        value REAL NOT NULL,                  -- VND
        source_created TEXT,                  -- VNDirect createdDate / modifiedDate (latest seen)
        source_modified TEXT,
        first_seen_at TEXT NOT NULL,          -- our first fetch that returned this value
        last_seen_at TEXT NOT NULL,           -- our last fetch that returned it
        UNIQUE (source, ticker, report_type, model_type, fiscal_date, item_code, value)
    );
    CREATE INDEX statements_ticker ON statements(ticker, fiscal_date);
    CREATE TABLE market_caps (
        source TEXT NOT NULL,
        ticker TEXT NOT NULL,
        date TEXT NOT NULL,
        value REAL NOT NULL,                  -- VND
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (source, ticker, date)
    ) WITHOUT ROWID;
    -- Each statement figure's value as of our latest fetch, and its first-reported value.
    CREATE VIEW statement_history AS
    SELECT source, ticker, report_type, model_type, fiscal_date, item_code,
           COUNT(*) AS versions,
           (SELECT s2.value FROM statements s2 WHERE s2.source = s.source AND s2.ticker = s.ticker
              AND s2.report_type = s.report_type AND s2.model_type = s.model_type
              AND s2.fiscal_date = s.fiscal_date AND s2.item_code = s.item_code
            ORDER BY s2.first_seen_at, s2.id LIMIT 1) AS first_value,
           (SELECT s2.value FROM statements s2 WHERE s2.source = s.source AND s2.ticker = s.ticker
              AND s2.report_type = s.report_type AND s2.model_type = s.model_type
              AND s2.fiscal_date = s.fiscal_date AND s2.item_code = s.item_code
            ORDER BY s2.last_seen_at DESC, s2.id DESC LIMIT 1) AS latest_value,
           MIN(first_seen_at) AS first_seen_at, MAX(last_seen_at) AS last_seen_at
    FROM statements s
    GROUP BY source, ticker, report_type, model_type, fiscal_date, item_code;
    """,
    # 3: paper portfolio (paper.py): the screen's lists frozen on a screen date, tracked forward. Records of what the
    # screen said then, not derived from the raw log: `rebuild()` leaves them alone, and they are never edited.
    """
    CREATE TABLE paper_cohorts (
        freeze_date TEXT PRIMARY KEY,         -- the screen's price date (newest last close in the universe)
        frozen_at TEXT NOT NULL,              -- UTC, when `paper freeze` ran
        config_hash TEXT NOT NULL,            -- sha256[:12] of config.py's thresholds (canonical JSON)
        config_json TEXT NOT NULL,            -- the thresholds themselves
        code_version TEXT,                    -- `git describe --dirty` of the code that made the lists
        offline INTEGER NOT NULL              -- 1: frozen from stored data only (`--offline`)
    );
    CREATE TABLE paper_members (
        freeze_date TEXT NOT NULL REFERENCES paper_cohorts(freeze_date),
        list TEXT NOT NULL,                   -- a screen list (report.LISTS), 'pool' (analysed) or 'universe' (liquid)
        ticker TEXT NOT NULL,
        rank INTEGER NOT NULL,                -- 1 = first in the list's display order; pool and universe by ticker
        exchange TEXT,
        ref_date TEXT,                        -- the member's last close the screen used
        ref_close REAL,                       -- VND, raw close on ref_date
        ref_ad_close REAL,                    -- VND, adClose on ref_date as stored at the freeze
        avg_value REAL,                       -- VND a day, 20-day average traded value at the freeze
        PRIMARY KEY (freeze_date, list, ticker)
    ) WITHOUT ROWID;
    CREATE TRIGGER paper_cohorts_append_only_u BEFORE UPDATE ON paper_cohorts
        BEGIN SELECT RAISE(ABORT, 'paper cohorts are never edited'); END;
    CREATE TRIGGER paper_cohorts_append_only_d BEFORE DELETE ON paper_cohorts
        BEGIN SELECT RAISE(ABORT, 'paper cohorts are never edited'); END;
    CREATE TRIGGER paper_members_append_only_u BEFORE UPDATE ON paper_members
        BEGIN SELECT RAISE(ABORT, 'paper cohorts are never edited'); END;
    CREATE TRIGGER paper_members_append_only_d BEFORE DELETE ON paper_members
        BEGIN SELECT RAISE(ABORT, 'paper cohorts are never edited'); END;
    """,
    # 4: filings studies (filings/importer.py): figures read in the audited filings, the reader's findings, and each
    # run's Graham scorecard. Records of what a study read, not derived from the raw log: `rebuild()` keeps them.
    """
    CREATE TABLE filing_figures (
        id INTEGER PRIMARY KEY,
        ticker TEXT NOT NULL,
        fiscal_year INTEGER NOT NULL,         -- the year the figure is for
        filing_year INTEGER NOT NULL,         -- the filing it was read in (fiscal_year + 1: a comparative column)
        item TEXT NOT NULL,                   -- schema item, e.g. 'is.parent', 'bs.cash'
        value REAL NOT NULL,                  -- bn VND (EPS in VND)
        vi TEXT NOT NULL,                     -- 'V' read in the filing, 'I' inferred
        page TEXT,                            -- page range of the statement in the filing
        method TEXT,                          -- how the page was read (text layer, Tesseract, NVIDIA model, reader)
        source_sha256 TEXT,                   -- sha256 of the filing PDF when known
        study TEXT,                           -- e.g. 'first-study-2026-11-07', 'v1'
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE (ticker, fiscal_year, filing_year, item, value, vi)
    );
    CREATE INDEX filing_figures_ticker ON filing_figures(ticker, fiscal_year);
    CREATE TABLE filing_findings (
        id INTEGER PRIMARY KEY,
        ticker TEXT NOT NULL,
        fiscal_year INTEGER,                  -- NULL: about the whole record
        category TEXT NOT NULL,               -- a reading-checklist category (filings/importer.CATEGORIES)
        severity TEXT NOT NULL,               -- info | watch | concern | red-flag
        text TEXT NOT NULL,
        page TEXT,
        note_path TEXT,                       -- the study note it comes from
        created_at TEXT NOT NULL
    );
    -- one row per finding; a whole-record finding (NULL year) is unique too
    CREATE UNIQUE INDEX filing_findings_key ON filing_findings(ticker, COALESCE(fiscal_year, 0), category, text);
    CREATE TABLE graham_scorecards (
        ticker TEXT NOT NULL,
        run_at TEXT NOT NULL,                 -- UTC
        price_date TEXT,
        price REAL,                           -- VND
        defensive_passed INTEGER,
        defensive_known INTEGER,
        enterprising_passed INTEGER,
        enterprising_known INTEGER,
        scorecard_json TEXT NOT NULL,         -- the full scorecard (filings/graham.py), as written to graham.json
        PRIMARY KEY (ticker, run_at)
    ) WITHOUT ROWID;
    """,
    # 5: the bulk report database (filings/bulk.py): every line of the three statements of each audited report,
    # machine-read (no reader agents), with its check status, plus one row per report (auditor's opinion, scope,
    # template, pages, identity results). Records, not derived from the raw log: `rebuild()` keeps them. A value is
    # never overwritten: a re-read that changes it adds a row (the latest `last_seen_at` per line is the current
    # reading), and a figure's own filing (filing_year = fiscal_year) and the next filing's comparative column
    # (filing_year = fiscal_year + 1) are separate rows: the restatement record.
    """
    CREATE TABLE filing_reports (
        ticker TEXT NOT NULL,
        fiscal_year INTEGER NOT NULL,
        file TEXT NOT NULL,                   -- PDF name in data/filings/<T>/
        file_sha256 TEXT NOT NULL,
        scope TEXT,                           -- consolidated | single (no split: no subsidiaries) | standalone (parent
                                              -- only, read because no consolidated set exists: flagged)
        template TEXT,                        -- c200 (Circular 200, FY2015+) | d15 (Decision 15 / Circ. 244) | d1141
        cf_method TEXT,                       -- indirect | direct
        n_pages INTEGER,
        text_pages INTEGER,                   -- pages with a real text layer (no OCR)
        pages_json TEXT,                      -- {"audit": [..], "bs": [..], "is": [..], "cf": [..], "read": [..]}
        methods_json TEXT,                    -- {page: method}
        auditor TEXT,
        opinion TEXT,                         -- unqualified | qualified | adverse | disclaimer | unknown
        emphasis INTEGER,                     -- 1 emphasis-of-matter paragraph, 0 none, NULL opinion text not read
        report_date TEXT,
        opinion_json TEXT,                    -- the parse (other matter, excerpt of a non-clean opinion)
        checks_json TEXT,                     -- per statement: identities pass/fail/skip, status, reader; failures
        vndirect_json TEXT,                   -- lines compared with VNDirect and those that differ
        status TEXT NOT NULL,                 -- identity-ok | identity-fail | second-reader-fixed | auto | no-statements
        lines INTEGER,                        -- line values stored (both columns)
        notes TEXT,                           -- what the parser did (swapped columns put back, fallback pages …)
        run_at TEXT NOT NULL,                 -- UTC
        PRIMARY KEY (ticker, fiscal_year, file_sha256)
    ) WITHOUT ROWID;
    CREATE TABLE filing_lines (
        id INTEGER PRIMARY KEY,
        ticker TEXT NOT NULL,
        fiscal_year INTEGER NOT NULL,         -- the year the value is for
        filing_year INTEGER NOT NULL,         -- the report it was read in (fiscal_year + 1 = its comparative column)
        statement TEXT NOT NULL,              -- bs | is | cf
        line_key TEXT NOT NULL,               -- the VAS code, or 'label:<folded label>' for a line printed without one
        code TEXT NOT NULL,                   -- VAS line code as printed ('' = none)
        label TEXT NOT NULL,
        item TEXT,                            -- schema item when mapped ('is.parent'), else NULL
        value REAL NOT NULL,                  -- as printed (sign included), bn VND; EPS in VND
        seq INTEGER,                          -- order on the statement
        page INTEGER,                         -- PDF page
        method TEXT,                          -- text | qwen:qwen3-vl-8b | nvidia:nvidia/nemotron-parse | tesseract:…
        file_sha256 TEXT NOT NULL,
        status TEXT NOT NULL,                 -- auto | identity-ok | identity-fail | second-reader-fixed
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE (ticker, fiscal_year, filing_year, statement, line_key, value, file_sha256)
    );
    CREATE INDEX filing_lines_key ON filing_lines(ticker, fiscal_year, filing_year, statement, line_key, last_seen_at);
    -- the current reading of every line: the latest import of each (ticker, year, filing, statement, line)
    CREATE VIEW filing_lines_latest AS
    SELECT l.* FROM filing_lines l
    WHERE l.last_seen_at = (SELECT MAX(m.last_seen_at) FROM filing_lines m WHERE m.ticker = l.ticker
        AND m.fiscal_year = l.fiscal_year AND m.filing_year = l.filing_year AND m.statement = l.statement
        AND m.line_key = l.line_key);
    -- a year's own figure vs the next report's comparative column: restated, reclassified or misread
    CREATE VIEW filing_restatements AS
    SELECT o.ticker, o.fiscal_year, o.statement, o.line_key, o.code, o.item, o.label, o.value AS own_value,
           n.value AS next_prior_value, o.status AS own_status, n.status AS next_status, o.page AS own_page,
           n.page AS next_page
    FROM filing_lines_latest o JOIN filing_lines_latest n ON n.ticker = o.ticker AND n.fiscal_year = o.fiscal_year
        AND n.filing_year = o.fiscal_year + 1 AND n.statement = o.statement AND n.line_key = o.line_key
    WHERE o.filing_year = o.fiscal_year
      AND ABS(o.value - n.value) > (CASE WHEN o.item LIKE 'is.eps%' THEN 1.0
                                        ELSE MAX(1.0, 0.001 * MAX(ABS(o.value), ABS(n.value))) END);
    """,
    # 6: the restatement view matched lines by code, but Circular 200 (FY2015) renumbered the lines: the first C200
    # report's comparative column (FY2014 re-presented) shares codes like 311 or 25 with different lines (DHA FY2014: 33
    # false differences). Same template on both reports → match by code; different templates → by schema item.
    """
    DROP VIEW filing_restatements;
    CREATE VIEW filing_restatements AS
    SELECT o.ticker, o.fiscal_year, o.statement, o.line_key, o.code, o.item, o.label, o.value AS own_value,
           n.value AS next_prior_value, o.status AS own_status, n.status AS next_status, o.page AS own_page,
           n.page AS next_page, ro.template AS own_template, rn.template AS next_template
    FROM filing_lines_latest o
    JOIN filing_reports ro ON ro.ticker = o.ticker AND ro.fiscal_year = o.filing_year AND ro.file_sha256 = o.file_sha256
    JOIN filing_lines_latest n ON n.ticker = o.ticker AND n.fiscal_year = o.fiscal_year
        AND n.filing_year = o.fiscal_year + 1 AND n.statement = o.statement
    JOIN filing_reports rn ON rn.ticker = n.ticker AND rn.fiscal_year = n.filing_year AND rn.file_sha256 = n.file_sha256
    WHERE o.filing_year = o.fiscal_year
      AND CASE WHEN COALESCE(ro.template, '') = COALESCE(rn.template, '') THEN n.line_key = o.line_key
               ELSE o.item IS NOT NULL AND n.item = o.item END
      AND ABS(o.value - n.value) > (CASE WHEN o.item LIKE 'is.eps%' THEN 1.0
                                        ELSE MAX(1.0, 0.001 * MAX(ABS(o.value), ABS(n.value))) END);
    """,
    # 7: the current reading of a report is its latest import (filing_reports.run_at), not each line's own latest row:
    # a line an earlier reading produced and a later one doesn't (a parser fix) must drop out.
    """
    DROP VIEW filing_restatements;
    DROP VIEW filing_lines_latest;
    CREATE VIEW filing_lines_latest AS
    SELECT l.* FROM filing_lines l
    JOIN filing_reports r ON r.ticker = l.ticker AND r.fiscal_year = l.filing_year AND r.file_sha256 = l.file_sha256
    WHERE l.last_seen_at = r.run_at;
    CREATE VIEW filing_restatements AS
    SELECT o.ticker, o.fiscal_year, o.statement, o.line_key, o.code, o.item, o.label, o.value AS own_value,
           n.value AS next_prior_value, o.status AS own_status, n.status AS next_status, o.page AS own_page,
           n.page AS next_page, ro.template AS own_template, rn.template AS next_template
    FROM filing_lines_latest o
    JOIN filing_reports ro ON ro.ticker = o.ticker AND ro.fiscal_year = o.filing_year AND ro.file_sha256 = o.file_sha256
    JOIN filing_lines_latest n ON n.ticker = o.ticker AND n.fiscal_year = o.fiscal_year
        AND n.filing_year = o.fiscal_year + 1 AND n.statement = o.statement
    JOIN filing_reports rn ON rn.ticker = n.ticker AND rn.fiscal_year = n.filing_year AND rn.file_sha256 = n.file_sha256
    WHERE o.filing_year = o.fiscal_year
      AND CASE WHEN COALESCE(ro.template, '') = COALESCE(rn.template, '') THEN n.line_key = o.line_key
               ELSE o.item IS NOT NULL AND n.item = o.item END
      AND ABS(o.value - n.value) > (CASE WHEN o.item LIKE 'is.eps%' THEN 1.0
                                        ELSE MAX(1.0, 0.001 * MAX(ABS(o.value), ABS(n.value))) END);
    """,
]

NORMALIZED = ("listings", "daily_prices", "statements", "market_caps")


TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # UTC, fixed width, so text order is time order


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime(TIME_FORMAT)


def utc_from_timestamp(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.UTC).strftime(TIME_FORMAT)


def parse_utc(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, TIME_FORMAT).replace(tzinfo=dt.UTC)


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def migrate(db: sqlite3.Connection, migrations: list[str] = MIGRATIONS) -> int:
    """Apply the migrations this database hasn't had yet, each in its own transaction. Returns the version.

    Migrations are append-only too: never edit one that has shipped; add the next one to MIGRATIONS."""
    db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    current = db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    if current > len(migrations):
        raise RuntimeError(f"database schema v{current} is newer than this code (v{len(migrations)})")
    for version, sql in enumerate(migrations[current:], current + 1):
        try:
            db.executescript(f"BEGIN;\n{sql}\nINSERT INTO schema_version VALUES ({version}, '{utc_now()}');\nCOMMIT;")
        except sqlite3.Error:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    return db.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


class Store:
    def __init__(self, path: Path | str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=300, isolation_level=None)  # explicit transactions below
        self.db.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.version = migrate(self.db)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ raw layer

    def _request_id(self, source: str, endpoint: str, params: dict, create: bool) -> int | None:
        key = canonical(params).decode()
        row = self.db.execute("SELECT id FROM raw_requests WHERE source = ? AND endpoint = ? AND params = ?",
                              (source, endpoint, key)).fetchone()
        if row or not create:
            return row[0] if row else None
        return self.db.execute("INSERT INTO raw_requests (source, endpoint, params) VALUES (?, ?, ?)",
                               (source, endpoint, key)).lastrowid

    def latest(self, source: str, endpoint: str, params: dict) -> tuple[dict, str] | None:
        """The body of the most recent fetch of this request and that fetch's time (UTC), or None."""
        request_id = self._request_id(source, endpoint, params, create=False)
        if request_id is None:
            return None
        row = self.db.execute(
            "SELECT r.body, f.fetched_at FROM raw_fetches f JOIN raw_responses r ON r.id = f.response_id "
            "WHERE f.request_id = ? ORDER BY f.fetched_at DESC, f.id DESC LIMIT 1", (request_id,)).fetchone()
        return (json.loads(zlib.decompress(row[0])), row[1]) if row else None

    def record(self, source: str, endpoint: str, params: dict, body, fetched_at: str | None = None) -> str:
        """Log one fetch. Returns 'new' (body stored), 'seen' (same body as the latest one: only the fetch is
        logged) or 'known' (this exact fetch was logged already, e.g. a cache file imported twice)."""
        fetched_at = fetched_at or utc_now()
        data = canonical(body)
        digest = hashlib.sha256(data).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            request_id = self._request_id(source, endpoint, params, create=True)
            prior = self.db.execute(
                "SELECT f.response_id, r.sha256, f.fetched_at FROM raw_fetches f "
                "JOIN raw_responses r ON r.id = f.response_id WHERE f.request_id = ? AND f.fetched_at <= ? "
                "ORDER BY f.fetched_at DESC, f.id DESC LIMIT 1", (request_id, fetched_at)).fetchone()
            if prior and prior[1] == digest and prior[2] == fetched_at:
                self.db.execute("COMMIT")
                return "known"
            if prior and prior[1] == digest:
                response_id, outcome = prior[0], "seen"
            else:
                response_id = self.db.execute(
                    "INSERT INTO raw_responses (request_id, fetched_at, sha256, body) VALUES (?, ?, ?, ?)",
                    (request_id, fetched_at, digest, zlib.compress(data, 6))).lastrowid
                outcome = "new"
            self.db.execute("INSERT INTO raw_fetches (request_id, response_id, fetched_at) VALUES (?, ?, ?)",
                            (request_id, response_id, fetched_at))
            self._normalize(source, endpoint, body, fetched_at)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return outcome

    # ------------------------------------------------------------ normalized layer

    def _normalize(self, source: str, endpoint: str, body, fetched_at: str) -> None:
        rows = body.get("data") if isinstance(body, dict) else None
        if not rows:
            return
        if source == "vndirect":  # 'vps' (index points, adjusted-only stock bars) stays raw only
            handler = {"stocks": self._listings, "stock_prices": self._prices,
                       "financial_statements": self._statements, "ratios": self._market_caps,
                       "ratios/latest": self._market_caps}.get(endpoint)
            if handler:
                handler(source, rows, fetched_at)

    def _listings(self, source: str, rows: list[dict], seen: str) -> None:
        records = []
        for s in rows:
            if not s.get("code"):
                continue
            listed, delisted, status = s.get("listedDate") or None, s.get("delistedDate") or None, s.get("status")
            base = (s.get("type"), s.get("companyId"), s.get("isin"), s.get("companyName"), s.get("shortName"),
                    float(s["faceValue"]) if s.get("faceValue") else None)
            if listed:
                # VNDirect keeps one row per ticker: its latest listing, plus the end of an earlier one in
                # delistedDate when that is before listedDate (a floor move, or a reused ticker like VPL).
                end = delisted if status == "delisted" and delisted and delisted >= listed else None
                records.append((s["code"], listed, listed, end, s.get("floor"), status, *base))
                if delisted and delisted < listed:
                    records.append((s["code"], ".." + delisted, None, delisted, None, "delisted", *base))
            elif delisted:
                records.append((s["code"], ".." + delisted, None, delisted, s.get("floor"), status, *base))
        self.db.executemany(
            """INSERT INTO listings (source, ticker, period_key, listed_date, delisted_date, exchange, status, type,
                   company_id, isin, company_name, short_name, face_value, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, ticker, period_key) DO UPDATE SET
                   delisted_date = CASE WHEN excluded.last_seen_at >= last_seen_at THEN excluded.delisted_date
                                        ELSE delisted_date END,
                   exchange = CASE WHEN excluded.last_seen_at >= last_seen_at
                                   THEN COALESCE(excluded.exchange, exchange) ELSE exchange END,
                   status = CASE WHEN excluded.last_seen_at >= last_seen_at THEN excluded.status ELSE status END,
                   company_name = CASE WHEN excluded.last_seen_at >= last_seen_at THEN excluded.company_name
                                       ELSE company_name END,
                   short_name = CASE WHEN excluded.last_seen_at >= last_seen_at THEN excluded.short_name
                                     ELSE short_name END,
                   first_seen_at = MIN(first_seen_at, excluded.first_seen_at),
                   last_seen_at = MAX(last_seen_at, excluded.last_seen_at)""",
            [(source, *r, seen, seen) for r in records])

    def _prices(self, source: str, rows: list[dict], fetched: str) -> None:
        def vnd(x):
            return None if x is None else x * 1000

        self.db.executemany(
            """INSERT INTO daily_prices (source, ticker, date, close, ad_close, volume, value, exchange, type,
                   fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, ticker, date) DO UPDATE SET
                   close = excluded.close, ad_close = excluded.ad_close, volume = excluded.volume,
                   value = excluded.value, exchange = excluded.exchange, type = excluded.type,
                   fetched_at = excluded.fetched_at
               WHERE excluded.fetched_at >= daily_prices.fetched_at""",
            [(source, r["code"], r["date"], vnd(r.get("close")), vnd(r.get("adClose")), r.get("nmVolume"),
              r.get("nmValue"), r.get("floor"), r.get("type"), fetched)
             for r in rows if r.get("code") and r.get("date")])

    def _statements(self, source: str, rows: list[dict], seen: str) -> None:
        self.db.executemany(
            """INSERT INTO statements (source, ticker, report_type, model_type, fiscal_date, item_code, value,
                   source_created, source_modified, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, ticker, report_type, model_type, fiscal_date, item_code, value) DO UPDATE SET
                   source_created = CASE WHEN excluded.last_seen_at >= last_seen_at THEN excluded.source_created
                                         ELSE source_created END,
                   source_modified = CASE WHEN excluded.last_seen_at >= last_seen_at THEN excluded.source_modified
                                          ELSE source_modified END,
                   first_seen_at = MIN(first_seen_at, excluded.first_seen_at),
                   last_seen_at = MAX(last_seen_at, excluded.last_seen_at)""",
            [(source, r["code"], r.get("reportType") or "", int(r.get("modelType") or 0), r["fiscalDate"][:10],
              int(r["itemCode"]), float(r["numericValue"]), r.get("createdDate"), r.get("modifiedDate"), seen, seen)
             for r in rows if r.get("code") and r.get("fiscalDate") and r.get("itemCode") is not None
             and r.get("numericValue") is not None])

    def _market_caps(self, source: str, rows: list[dict], fetched: str) -> None:
        self.db.executemany(
            """INSERT INTO market_caps (source, ticker, date, value, fetched_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (source, ticker, date) DO UPDATE SET value = excluded.value,
                   fetched_at = excluded.fetched_at WHERE excluded.fetched_at >= market_caps.fetched_at""",
            [(source, r["code"], r["reportDate"][:10], float(r["value"]), fetched) for r in rows
             if r.get("ratioCode") in (None, "MARKETCAP") and r.get("code") and r.get("reportDate")
             and r.get("value") is not None])

    def rebuild(self) -> None:
        """Rebuild every normalized table from the raw layer, replaying fetches in time order."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for table in NORMALIZED:
                self.db.execute(f"DELETE FROM {table}")
            fetches = self.db.execute(
                "SELECT q.source, q.endpoint, r.body, f.fetched_at FROM raw_fetches f "
                "JOIN raw_requests q ON q.id = f.request_id JOIN raw_responses r ON r.id = f.response_id "
                "ORDER BY f.fetched_at, f.id")
            for source, endpoint, body, fetched_at in fetches:
                self._normalize(source, endpoint, json.loads(zlib.decompress(body)), fetched_at)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ reporting

    def counts(self, tables: Iterable[str] = ("raw_requests", "raw_responses", "raw_fetches", *NORMALIZED,
                                              "paper_cohorts", "paper_members")) -> dict:
        return {t: self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

    def size_bytes(self) -> int:
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        return page_size * self.db.execute("PRAGMA page_count").fetchone()[0]
