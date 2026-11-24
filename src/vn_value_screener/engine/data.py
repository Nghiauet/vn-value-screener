"""Builds one record per company from VNDirect: annual financials, share count, price, liquidity."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .config import Config
from .vndirect import CacheMiss, Client, IncompleteResult
from .vps import VpsError, stock_bars

# VNDirect item codes for the non-financial statement template (VAS). Values are in VND.
ITEMS = {
    # income statement
    "sales": 21001, "gross_profit": 23100, "operating_profit": 23110, "interest_expense": 22510,
    "pretax_profit": 23800, "npat": 23003, "npat_parent": 23000, "eps_reported": 23001,
    # balance sheet
    "current_assets": 11000, "cash": 11100, "st_investments": 11200, "inventory": 11400,
    "total_assets": 12700, "goodwill": 12690, "goodwill_pre2015": 412541,
    "liabilities": 13000, "current_liabilities": 13100, "st_debt": 13110, "lt_debt": 13340,
    "equity": 14000, "paid_in_capital": 14110, "minority_interest": 14240,
    # cash flow
    "cfo": 32000, "capex": 32100, "share_issue": 33100, "dividends_paid": 33600,
}
ITEM_NAMES = {code: name for name, code in ITEMS.items()}
# Quarterly items: values are for the quarter itself, not the year to date.
QUARTER_ITEMS = ("sales", "npat", "npat_parent", "paid_in_capital")

# ICB supersectors (VNDirect `industry_classification`, level 2) whose statements follow the bank,
# insurer or securities-company templates, which Graham's industrial tests don't fit. Brokers and
# some insurers still file a current-assets line, so the template check alone let 30 of them into
# the 2026-10-18 screen. Real estate (8600) also sits under ICB 8000 Financials but files ordinary
# statements, so it stays in.
FINANCIAL_SECTORS = {"8300": "banks", "8500": "insurance", "8700": "financial services"}
# Lenders, brokers and insurers that VNDirect's sector list leaves out (no ICB code at all), set by hand. Checked
# 2026-11-23 against the 19 listed tickers without a sector on the store: F88 (pawn and consumer loans, files a
# current-assets line, so the template backstop graded it as an industrial) and KAI (KAFI Securities, a broker). The
# other 17 are industrial by name (cement, beer, seafood, logistics, a tanker operator…). The screen report lists any
# analysed company without a sector, so the next one shows up.
FINANCIAL_OVERRIDES = {"F88": "consumer lending", "KAI": "securities"}
DAY = 24  # hours, for cache ages
BATCH = 20  # tickers per statements request
WINDOW_CHUNK_DAYS = 7  # all-stock date windows: ≤ 5 trading days × ~1,700 rows a day fits one 10,000-row page
PRICE_BATCH = 36  # tickers per one-year price query: 36 × ~250 trading days fits one page
MIN_HISTORY_DAYS = 120  # less than ~half a year of trading: the one-year high stays unknown
BONUS_FACTOR_MIN = 1.10  # an adClose/close rise this big between two days is new shares (backtest.SHARE_ACTION_MIN)
# What counts as "VNDirect failed" for the opt-in VPS fallback: HTTP and network errors (OSError covers
# urllib's HTTPError/URLError and timeouts), a body that isn't JSON (ValueError), paging that loses rows even
# for one ticker, and a response missing from the store when offline.
FALLBACK_ERRORS = (OSError, ValueError, IncompleteResult, CacheMiss)


@dataclass
class Company:
    code: str
    name: str
    exchange: str
    face_value: float = 10_000
    price: float | None = None  # latest close, VND
    price_date: str | None = None
    avg_value_20d: float | None = None  # VND traded per day
    shares: float | None = None  # market cap / that day's close; else paid-in capital / face value
    shares_as_of: str | None = None
    balance_sheet_shares: float | None = None  # paid-in capital / face value, for the cross-check
    cap_ignored: float | None = None  # a market cap implying this many fewer shares was not used (e.g. -0.90)
    sector: str | None = None  # ICB level-2 code, e.g. "8700"
    years: dict[int, dict[str, float]] = field(default_factory=dict)  # fiscal year -> item -> VND
    quarters: dict[str, dict[str, float]] = field(default_factory=dict)  # quarter-end date -> item -> VND
    high_1y: float | None = None  # highest adjusted close of the last year, VND
    change_1y: float | None = None  # adjusted (dividends and bonus shares) price change over the year
    history_source: str | None = None  # 'vps' when high_1y/change_1y came from the VPS fallback
    year_end_close: dict[int, float] = field(default_factory=dict)  # raw close on the last trading day, VND

    @property
    def market_cap(self) -> float | None:
        return self.price * self.shares if self.price and self.shares else None

    @property
    def share_count_gap(self) -> float | None:
        """How far today's share count is from the last balance sheet's (e.g. +3.0 = 4× more shares)."""
        if self.shares and self.balance_sheet_shares:
            return self.shares / self.balance_sheet_shares - 1
        return None

    @property
    def vs_high_1y(self) -> float | None:
        """Today's price vs the highest adjusted close of the last year (−0.30 = 30% below it)."""
        return self.price / self.high_1y - 1 if self.price and self.high_1y else None

    @property
    def latest_year(self) -> int | None:
        full = [y for y, v in self.years.items() if "sales" in v and ("npat_parent" in v or "npat" in v)]
        return max(full) if full else None

    @property
    def kind(self) -> str:
        """'industrial', 'financial' (banks, insurers, brokers use other templates) or 'no data'."""
        if self.sector in FINANCIAL_SECTORS or self.code in FINANCIAL_OVERRIDES:
            return "financial"
        if not self.years:
            return "no data"
        year = self.latest_year
        if year is None or "current_assets" not in self.years[year]:
            return "financial"
        return "industrial"

    def get(self, year: int, item: str) -> float | None:
        values = self.years.get(year, {})
        if item == "npat_parent":  # older filings sometimes lack the parent-only line
            return values.get("npat_parent", values.get("npat"))
        if item == "goodwill":
            return values.get("goodwill", values.get("goodwill_pre2015", 0.0))
        if item == "parent_equity":  # since 2015, non-controlling interests sit inside equity
            equity = values.get("equity")
            return None if equity is None else equity - values.get("minority_interest", 0.0)
        return values.get(item)


def load(client: Client, cfg: Config, log: Callable[[str], None] = print,
         backup: Client | None = None, fresh_prices: bool = False) -> tuple[list[Company], dict]:
    """Listed stocks that trade enough to matter, with prices and annual statements.

    `backup` (a vps.VpsClient, opt-in: `--vps-fallback`) fills a ticker's one-year high and change from VPS's
    adjusted prices when VNDirect's one-year history fails or lacks it. Nothing else falls back.

    `fresh_prices` (`paper freeze`): the latest-date check, the 40-day window, the market caps and the one-year
    prices are fetched now, whatever is stored (max age 0: 21 requests for ~285 liquid stocks), so a response stored
    during the session (a `screen` at 11:00, reused for 12 hours) never becomes a cohort's close. The stock list and
    the statements still come from the store while young enough."""
    age = 0 if fresh_prices else None
    stocks = client.get_all("stocks", "type:STOCK~status:LISTED", 7 * DAY)
    companies = {
        s["code"]: Company(s["code"], s.get("shortName") or s.get("companyName") or s["code"],
                           s["floor"], float(s.get("faceValue") or 10_000))
        for s in stocks if s.get("floor") in cfg.exchanges
    }
    log(f"{len(companies)} listed stocks on {', '.join(cfg.exchanges)}")
    _add_sectors(client, companies.values())
    _add_prices(client, companies, age)
    # By ticker, not in the stock list's order: VNDirect reorders that list between fetches.
    liquid = sorted((c for c in companies.values() if c.price and (c.avg_value_20d or 0) >= cfg.min_daily_value),
                    key=lambda c: c.code)
    log(f"{len(liquid)} trade at least {cfg.min_daily_value / 1e9:g} bn VND a day; fetching their statements")
    _add_financials(client, liquid, cfg, log)
    history = _add_price_history(client, liquid, cfg, backup, log, age)
    unmatched = _add_market_caps(client, liquid, history, age)
    if unmatched:
        log(f"  {len(unmatched)} share counts are market cap ÷ today's price (no close on the cap's day): "
            + ", ".join(unmatched))
    stats = {"listed": len(companies), "liquid": len(liquid),
             "price_date": max((c.price_date for c in liquid if c.price_date), default=None)}
    if backup is not None:
        stats["vps_history"] = sorted(c.code for c in liquid if c.history_source == "vps")
    return liquid, stats


def load_one(client: Client, cfg: Config, ticker: str, log: Callable[[str], None] = print,
             backup: Client | None = None) -> Company | None:
    """A single ticker, even one too illiquid for the screen's universe."""
    rows = client.get_all("stocks", f"code:{ticker}", 7 * DAY)
    if not rows:
        return None
    s = rows[0]
    company = Company(s["code"], s.get("shortName") or s.get("companyName") or s["code"], s.get("floor", "?"),
                      float(s.get("faceValue") or 10_000))
    days = client.get("stock_prices", {"sort": "date:desc", "q": f"code:{ticker}", "size": 20}, 12).get("data") or []
    if days:
        company.price, company.price_date = days[0]["close"] * 1000, days[0]["date"]
        company.avg_value_20d = sum(d.get("nmValue") or 0 for d in days) / len(days)
    _add_sectors(client, [company])
    _add_financials(client, [company], cfg, log)
    _add_market_caps(client, [company], _add_price_history(client, [company], cfg, backup, log))
    return company


def _add_sectors(client: Client, companies: Iterable[Company]) -> None:
    """Each company's ICB supersector, from one request that lists every sector's tickers."""
    rows = client.get("industry_classification", {"q": "industryLevel:2", "size": 100}, 7 * DAY).get("data") or []
    sector_of = {code: r["industryCode"] for r in rows for code in (r.get("codeList") or "").split(",") if code}
    for c in companies:
        c.sector = sector_of.get(c.code)


def bonus_factor(later: dict | None, earlier: dict | None) -> float:
    """New shares from stock dividends / bonus issues that went ex between two days of one ticker, from rows of one
    response (adClose rows of different fetches aren't on one scale). adClose / close is the cumulative adjustment at a
    date; a cash dividend moves it a few percent, so only a rise ≥ BONUS_FACTOR_MIN counts (backtest.share_factor's
    rule; a very large cash dividend could be misread)."""
    try:
        factor = (later["adClose"] / later["close"]) / (earlier["adClose"] / earlier["close"])
    except (TypeError, KeyError, ZeroDivisionError):
        return 1.0
    return factor if factor >= BONUS_FACTOR_MIN else 1.0


def _add_market_caps(client: Client, companies: list[Company], history: dict[str, dict[str, dict]] | None = None,
                     max_age: float | None = None) -> list[str]:
    """Share count = VNDirect's market cap ÷ the raw close of the cap's own day (its `reportDate`).

    VNDirect posts a day's market caps only that evening or night, so a run during the day or early evening gets the
    previous trading day's. Until 2026-11-08 the cap was divided by today's price, which moved the share count by the
    day's price change (Mon 2026-11-02 with Friday's caps: 7 of 135 list memberships). The close comes from `history`
    (ticker → date → row of the one-year price response, `_add_price_history`); a stock dividend or bonus issue that
    went ex after the cap's day carries the count forward by that response's adClose/close factor (`bonus_factor`).
    Without a close on the cap's day the count stays cap ÷ today's price, and `shares_as_of` says so; those tickers
    are returned.

    The balance sheet lags: a bonus-share issue after the last quarterly report cuts the price
    at once but leaves paid-in capital unchanged, which makes the stock look far cheaper than it
    is. The balance-sheet count stays as the fallback and as a cross-check.

    A cap implying more than CAP_MAX_SHARE_FALL fewer shares than the balance sheet is not used: a bonus issue only adds
    shares, and on 2026-10-26 VNDirect's TLD cap fell 648 -> 64.6 bn with the price flat (P/E 2.2 instead of ~21, a
    false net-net). The balance-sheet count stays, `cap_ignored` holds the implied gap, and the screen flags it.
    """
    by_code = {c.code: c for c in companies}
    codes = sorted(by_code)  # stable batches: a stored response is found again whatever order the stock list had
    unmatched = []
    for i in range(0, len(codes), 50):
        rows = client.get("ratios/latest", {
            "filter": "ratioCode:MARKETCAP", "where": f"code:{','.join(codes[i:i + 50])}",
            "order": "reportDate", "fields": "code,ratioCode,value,reportDate"},
            12 if max_age is None else max_age).get("data") or []
        for row in rows:
            company = by_code.get(row.get("code"))
            if company and company.price and row.get("value"):
                company.balance_sheet_shares, bs_as_of = company.shares, company.shares_as_of
                value, day = float(row["value"]), (row.get("reportDate") or "")[:10]
                days = (history or {}).get(company.code, {})
                close = (days.get(day) or {}).get("close")
                if day == company.price_date:  # an evening run: the cap and the price are of one day
                    company.shares, company.shares_as_of = value / company.price, f"market cap {day}"
                elif close:
                    factor = bonus_factor(days.get(company.price_date), days[day]) \
                        if day < (company.price_date or "") else 1.0
                    company.shares = value / (close * 1000) * factor
                    company.shares_as_of = f"market cap {day}" + (
                        f" × {factor:.2f} (bonus shares since)" if factor > 1 else "")
                else:
                    company.shares = value / company.price
                    company.shares_as_of = f"market cap {day} ÷ the {company.price_date} price (no {day} close)"
                    unmatched.append(company.code)
                bs = company.balance_sheet_shares
                if bs and company.shares < (1 - CAP_MAX_SHARE_FALL) * bs:
                    company.cap_ignored = company.shares / bs - 1
                    company.shares_as_of = (f"{bs_as_of} (market cap {day} implies {company.cap_ignored:+.0%} shares: "
                                            "not trusted)")
                    company.shares = bs
    return unmatched


CAP_MAX_SHARE_FALL = 0.30  # buybacks and treasury shares can lower the count; not by this much


def fetch_window(client: Client, start: dt.date, end: dt.date,
                 max_age: Callable[[dt.date], float] = lambda _: 12) -> dict[str, list[dict]]:
    """Every stock's daily rows between two dates (inclusive), by ticker, oldest first.

    A `stock_prices` query that spans pages loses rows (see `Client.get_all`), so the window is fetched
    in one-week chunks that fit one page; a chunk that still doesn't fit is halved until it does."""
    by_code: dict[str, dict[str, dict]] = defaultdict(dict)

    def chunk(a: dt.date, b: dt.date) -> None:
        try:
            rows = client.get_all("stock_prices", f"date:gte:{a}~date:lte:{b}", max_age(b), sort="date")
        except IncompleteResult:
            if a == b:
                raise
            mid = a + (b - a) // 2
            chunk(a, mid)
            chunk(mid + dt.timedelta(days=1), b)
            return
        for r in rows:
            by_code[r["code"]][r["date"]] = r

    day = start
    while day <= end:
        stop = min(day + dt.timedelta(days=WINDOW_CHUNK_DAYS - 1), end)
        chunk(day, stop)
        day = stop + dt.timedelta(days=1)
    return {code: [rows[d] for d in sorted(rows)] for code, rows in by_code.items()}


def fetch_codes(client: Client, path: str, codes: list[str], q_rest: str, max_age: float, batch: int,
                **params) -> list[dict]:
    """`path` rows for many tickers, `batch` tickers per query (`code:A,B,...` + `q_rest`).

    Batches are sized to fit one page; one that loses rows to paging anyway is halved until it fits."""
    out: list[dict] = []

    def query(group: list[str]) -> None:
        try:
            out.extend(client.get_all(path, f"code:{','.join(group)}{q_rest}", max_age, **params))
        except IncompleteResult:
            if len(group) == 1:
                raise
            query(group[:len(group) // 2])
            query(group[len(group) // 2:])

    for i in range(0, len(codes), batch):
        query(codes[i:i + batch])
    return out


def _add_prices(client: Client, companies: dict[str, Company], max_age: float | None = None) -> None:
    latest = client.get("stock_prices", {"sort": "date:desc", "q": "code:FPT,VNM,VCB", "size": 1},
                        6 if max_age is None else max_age)
    last_day = dt.date.fromisoformat(latest["data"][0]["date"])
    start = last_day - dt.timedelta(days=40)  # comfortably more than 20 trading days
    window_age = (lambda _: 12) if max_age is None else (lambda _: max_age)
    for code, days in fetch_window(client, start, last_day, window_age).items():
        company = companies.get(code)
        if company is None:
            continue
        recent = days[-20:]
        company.price = days[-1]["close"] * 1000  # VNDirect quotes prices in thousands of VND
        company.price_date = days[-1]["date"]
        company.avg_value_20d = sum(r.get("nmValue") or 0 for r in recent) / len(recent)


def year_stats(rows: list[dict], price: float | None = None, price_date: str | None = None
               ) -> tuple[float, float] | None:
    """(one-year high in VND, change over the rows) from one ticker's adjusted closes (`adClose`, thousands of
    VND, oldest first, all from one response so they share one scale); None for too short a history.

    The high is put on the scale of today's raw price (`price`, VND, on `price_date`): high × price ÷ that day's
    adClose. Usually the factor is 1, but between VNDirect posting a dividend and its ex-date the whole adjusted
    series, the newest day included, is already on the post-dividend scale while the raw close isn't (SLS,
    2026-10-30: close 140.0, adClose 130.004 before its 10,000 VND dividend went ex on 2026-11-02)."""
    if len(rows) < MIN_HISTORY_DAYS:
        return None
    newest = rows[-1]["adClose"]
    scale = 1.0
    if price and newest and rows[-1].get("date") == price_date:
        scale = price / (newest * 1000)
    return max(r["adClose"] for r in rows) * 1000 * scale, newest / rows[0]["adClose"] - 1


def _add_price_history(client: Client, companies: list[Company], cfg: Config, backup: Client | None = None,
                       log: Callable[[str], None] = print, max_age: float | None = None) -> dict[str, dict[str, dict]]:
    """One year of adjusted closes (adClose: corrected for cash and stock dividends), for the
    high-of-the-year and the year's change. Batches of PRICE_BATCH tickers, one page each. Returns VNDirect's rows
    (ticker → date → row, one response per ticker) for `_add_market_caps`.

    Without `backup` a failed batch raises, as before. With it (opt-in), a ticker whose batch failed or that
    came back with too short a history gets the same two figures from VPS's adjusted closes (`vps.stock_bars`,
    one request per ticker) over the same window, cut at the ticker's VNDirect price date; it is marked
    `history_source = 'vps'`. VPS has adjusted prices only, so nothing raw is taken from it."""
    age = 12 if max_age is None else max_age
    by_code = {c.code: c for c in companies if c.price_date}
    if not by_code:
        return {}
    last_day = max(c.price_date for c in by_code.values())
    start = dt.date.fromisoformat(last_day) - dt.timedelta(days=cfg.price_history_days)
    codes = sorted(by_code)
    by_ticker: dict[str, dict[str, dict]] = defaultdict(dict)
    failed: set[str] = set()
    for i in range(0, len(codes), PRICE_BATCH):  # one batch per query, exactly as fetch_codes would send them
        group = codes[i:i + PRICE_BATCH]
        try:
            rows = fetch_codes(client, "stock_prices", group, f"~date:gte:{start}", age, PRICE_BATCH)
        except FALLBACK_ERRORS as e:
            if backup is None:
                raise
            log(f"  VNDirect one-year prices failed for {group[0]}…{group[-1]} ({type(e).__name__}: {e}); "
                f"trying VPS for {len(group)} tickers")
            failed.update(group)
            continue
        for row in rows:
            if row.get("adClose"):
                by_ticker[row["code"]][row["date"]] = row
    for code, company in by_code.items():
        by_date = by_ticker.get(code, {})
        stats = year_stats([by_date[d] for d in sorted(by_date)], company.price, company.price_date)
        if stats is None and backup is not None:
            stats = _vps_year_stats(backup, code, start.isoformat(), company.price_date, code in failed, log,
                                    company.price, age)
            if stats is not None:
                company.history_source = "vps"
        if stats is not None:
            company.high_1y, company.change_1y = stats
    return by_ticker


def _vps_year_stats(backup: Client, code: str, start: str, end: str, batch_failed: bool,
                    log: Callable[[str], None], price: float | None = None,
                    max_age: float = 12) -> tuple[float, float] | None:
    try:
        bars = [r for r in stock_bars(backup, code, max_age) if start <= r["date"] <= end]
    except (*FALLBACK_ERRORS, VpsError) as e:
        log(f"  VPS fallback for {code} failed too ({type(e).__name__}: {e}); one-year high stays n/a")
        return None
    stats = year_stats(bars, price, end)
    why = "VNDirect failed" if batch_failed else "VNDirect history too short"
    log(f"  {code}: one-year high from VPS ({why}; {len(bars)} VPS bars)" if stats else
        f"  {code}: VPS has only {len(bars)} bars since {start} either; one-year high stays n/a")
    return stats


def add_year_end_prices(client: Client, company: Company, years: int = 11) -> None:
    """Raw closing price on the last trading day of each year, for the year-end P/E history.

    Raw (not adjusted) closes pair with that year's balance-sheet share count, so price × shares is
    the market value on the day. One request per company (~2,700 rows), so only the deep dive uses it.
    """
    if company.latest_year is None:
        return
    start = company.latest_year - years + 1
    rows = client.get_all("stock_prices", f"code:{company.code}~date:gte:{start}-12-01", 12)
    last: dict[int, dict] = {}
    for row in rows:
        year = int(row["date"][:4])
        if row.get("close") and (year not in last or row["date"] > last[year]["date"]):
            last[year] = row
    for year, row in last.items():
        if row["date"][5:7] == "12":  # the current year has no year-end yet
            company.year_end_close[year] = row["close"] * 1000


def annual_years(rows: Iterable[dict]) -> dict[int, dict[str, float]]:
    """One company's annual statement rows → fiscal year → item name → VND (latest restatement wins)."""
    years: dict[int, dict[str, float]] = {}
    for row in sorted(rows, key=lambda r: r.get("modifiedDate") or ""):
        name = ITEM_NAMES.get(int(row["itemCode"]))
        if name and row.get("numericValue") is not None:
            years.setdefault(int(row["fiscalDate"][:4]), {})[name] = float(row["numericValue"])
    return years


def _add_financials(client: Client, companies: list[Company], cfg: Config, log: Callable[[str], None]) -> None:
    by_code = {c.code: c for c in companies}
    # Sorted, so the batches (and their stored responses) don't change when VNDirect reorders the stock list:
    # each of the 7 changes of the stored list from 2026-10-31 to 11-06 reordered it (4 changed nothing else),
    # which made every batch new (an offline run after the daily snapshot failed with CacheMiss). A change of
    # the liquid universe still re-composes the batches after it (most days 5–13 of 15).
    codes = sorted(by_code)
    item_codes = ",".join(str(code) for code in ITEMS.values())
    first_year = dt.date.today().year - cfg.history_years - 1
    batches = [codes[i:i + BATCH] for i in range(0, len(codes), BATCH)]
    for n, batch in enumerate(batches, 1):
        log(f"  statements {n}/{len(batches)}")
        annual = fetch_codes(
            client, "financial_statements", batch,
            f"~reportType:ANNUAL~fiscalDate:gte:{first_year}-01-01~itemCode:{item_codes}", 7 * DAY, BATCH)
        rows_by_code: dict[str, list[dict]] = defaultdict(list)
        for row in annual:
            rows_by_code[row["code"]].append(row)
        for code, rows in rows_by_code.items():
            for year, values in annual_years(rows).items():
                by_code[code].years.setdefault(year, {}).update(values)
        # Recent quarters: sales and profit for the last-twelve-months trend, and the share count
        # from the newest balance sheet, so stock dividends paid since the last annual report
        # don't make a stock look cheaper than it is. From 1 Jan three years back: the trend needs eight
        # quarters in a row, and from two years back January to mid-February (before the newest Q4 is in
        # VNDirect) has only seven, so the last-four-quarters figures were n/a for most companies then.
        quarterly = fetch_codes(
            client, "financial_statements", batch,
            f"~reportType:QUARTER~fiscalDate:gte:{dt.date.today().year - 3}-01-01"
            f"~itemCode:{','.join(str(ITEMS[name]) for name in QUARTER_ITEMS)}", 7 * DAY, BATCH)
        for row in sorted(quarterly, key=lambda r: (r["fiscalDate"], r.get("modifiedDate") or "")):
            company = by_code[row["code"]]
            name = ITEM_NAMES.get(int(row["itemCode"]))
            if name is None or row.get("numericValue") is None:
                continue
            company.quarters.setdefault(row["fiscalDate"], {})[name] = float(row["numericValue"])
            if name == "paid_in_capital" and row["numericValue"]:
                company.shares = float(row["numericValue"]) / company.face_value
                company.shares_as_of = row["fiscalDate"]
    for company in companies:
        year = company.latest_year
        if company.shares is None and year and company.get(year, "paid_in_capital"):
            company.shares = company.get(year, "paid_in_capital") / company.face_value
            company.shares_as_of = f"{year}-12-31"
