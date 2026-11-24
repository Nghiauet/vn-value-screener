"""What the MCP tools do, without MCP: load the market once an hour, run the screens, return plain JSON-ready dicts.

Everything here runs on one thread (the server gives it a single worker): the SQLite store's connection belongs to
the thread that opened it.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path
from typing import Any

from . import REPOSITORY, __version__
from .engine import vndirect
from .engine.config import Config
from .engine.data import FINANCIAL_OVERRIDES, Company, add_year_end_prices, load, load_one
from .engine.graham import Check
from .engine.quality import FISHER_POINTS
from .engine.report import DISCLAIMER, LISTS, Result, analyse, select_lists, year_end_pe
from .engine.store import Store
from .engine.vndirect import CacheMiss, Client, IncompleteResult

# What a failed or unexpected answer from the data API raises inside the engine (network errors, bad JSON, a page
# that lost rows, an empty or reshaped response).
SOURCE_ERRORS = (OSError, ValueError, LookupError, TypeError, IncompleteResult)

# Our own honest User-Agent, so VNDirect can tell this package's traffic from the author's research tool.
USER_AGENT = f"vn-value-screener/{__version__} (+{REPOSITORY})"
vndirect.USER_AGENT = USER_AGENT

UNIVERSE_TTL = 3600.0  # seconds an analysed market is reused before it is rebuilt from the store
TICKER = re.compile(r"[A-Z0-9]{3,10}")
LIST_KEYS = ("all", *LISTS)
MAX_LIMIT = 50

DATA_SOURCE = ("VNDirect finfo (api-finfo.vndirect.com.vn), the public API behind VNDirect's website: annual and "
               "quarterly consolidated statements, daily prices, market caps. Not affiliated with VNDirect; the API is "
               "undocumented and may change or stop. Figures can be restated or wrong: check them against the "
               "audited annual report.")

LIST_RULES = {
    "defensive": "Passes at least 6 of Graham's 7 tests for the defensive investor (The Intelligent Investor, ch. 14).",
    "qarp": ("Quality at a reasonable price: at least 6 of the 8 quality signals, and a P/E on 3-year earnings within "
             "Graham's limit or a price at or below the Graham number."),
    "enterprising": "Passes all 6 of Graham's tests for the enterprising investor (ch. 15).",
    "net_net": "Price below two thirds of net current asset value: Graham's deepest bargains, often small or troubled.",
    "leaders": "At least 7 of the 8 quality signals, at any price: a watchlist to wait on for a better price.",
    "fisher": ("Fisher growth candidates: sales growth, an above-median operating margin, ROE and little dilution all "
               "pass, with quality at least 6 of 8."),
}

ProgressLog = Callable[[str], None]


class ScreenerError(Exception):
    """A problem to show the caller as it is: bad input, an unknown ticker, or the data source not answering."""


def default_data_dir() -> Path:
    """`VN_VALUE_SCREENER_DATA_DIR`, else `$XDG_CACHE_HOME/vn-value-screener`, else `~/.cache/vn-value-screener`."""
    if env := os.environ.get("VN_VALUE_SCREENER_DATA_DIR"):
        return Path(env).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "vn-value-screener"


def _quiet(_: str) -> None:
    pass


def _r(x: float | None, digits: int = 3) -> float | None:
    return None if x is None else round(x, digits)


def _bn(x: float | None) -> float | None:
    """VND → billions of VND, one decimal."""
    return None if x is None else round(x / 1e9, 1)


def _vnd(x: float | None) -> int | None:
    return None if x is None else round(x)


def _score(checks: list[Check]) -> dict[str, int]:
    return {"passed": Result.score(checks), "of": len(checks)}


def _checks(checks: list[Check]) -> list[dict[str, Any]]:
    return [{"key": ch.key, "test": ch.label, "passed": ch.passed, "detail": ch.detail} for ch in checks]


def _price_vs_graham(c: Company, r: Result) -> float | None:
    """Price ÷ Graham number − 1: −0.30 = 30% below the most Graham's defensive investor would pay."""
    g = r.value.graham_number
    return _r(c.price / g - 1) if c.price and g and g > 0 else None


def _warnings(r: Result) -> list[str]:
    c, out = r.company, []
    gap = c.share_count_gap
    if gap is not None and abs(gap) > 0.10:
        out.append(f"Today's share count is {gap:+.0%} vs the last balance sheet (a bonus issue, rights issue or "
                   "buyback since then): per-share figures use today's count.")
    if c.cap_ignored is not None:
        out.append(f"VNDirect's market cap implied {c.cap_ignored:+.0%} shares vs the balance sheet and was ignored; "
                   "the balance-sheet share count is used.")
    return out


class Screener:
    """One store and one API client; the analysed market is kept for `UNIVERSE_TTL` seconds."""

    def __init__(self, client: Client, cfg: Config | None = None, clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.cfg = cfg or Config()
        self.clock = clock
        self._universe: tuple[list[Company], dict, list[Result]] | None = None
        self._loaded_at = 0.0

    @classmethod
    def open(cls, data_dir: Path | None = None, offline: bool | None = None) -> Screener:
        if offline is None:
            offline = os.environ.get("VN_VALUE_SCREENER_OFFLINE", "").lower() in ("1", "true", "yes")
        store = Store((data_dir or default_data_dir()) / "stock.db")
        return cls(Client(store, offline=offline))

    # ------------------------------------------------------------------ data

    def universe(self, log: ProgressLog = _quiet) -> tuple[list[Company], dict, list[Result]]:
        """The liquid universe, its stats, and every non-financial company's screen result."""
        if self._universe is None or self.clock() - self._loaded_at > UNIVERSE_TTL:
            try:
                companies, stats = load(self.client, self.cfg, log)
            except CacheMiss as e:
                raise ScreenerError(f"offline mode, and this data isn't stored yet ({e}); run once online") from e
            except SOURCE_ERRORS as e:
                raise ScreenerError(f"VNDirect's data API didn't answer as expected ({type(e).__name__}: {e}); "
                                    "try again in a few minutes") from e
            self._universe = companies, stats, analyse(companies, self.cfg)
            self._loaded_at = self.clock()
        return self._universe

    # ------------------------------------------------------------------ tools

    def screen(self, list_name: str = "all", limit: int = 10, log: ProgressLog = _quiet) -> dict[str, Any]:
        if list_name not in LIST_KEYS:
            raise ScreenerError(f"unknown list {list_name!r}; use one of: {', '.join(LIST_KEYS)}")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
            raise ScreenerError(f"limit must be a whole number from 1 to {MAX_LIMIT}")
        _, stats, results = self.universe(log)
        lists = select_lists(results, self.cfg)
        keys = list(LISTS) if list_name == "all" else [list_name]
        return {
            "price_date": stats.get("price_date"),
            "universe": {"listed": stats.get("listed"), "liquid": stats.get("liquid"), "analysed": len(results),
                         "min_daily_traded_bn_vnd": self.cfg.min_daily_value / 1e9,
                         "excluded": "banks, insurers, brokers and lenders (they need P/B, ROE and asset quality)"},
            "lists": {key: {"title": LISTS[key], "rule": LIST_RULES[key], "count": len(lists[key]),
                            "shown": min(limit, len(lists[key])),
                            "companies": [self._row(r) for r in lists[key][:limit]]} for key in keys},
            "no_icb_sector": sorted(r.company.code for r in results if r.company.sector is None),
            "data_source": DATA_SOURCE,
            "disclaimer": DISCLAIMER,
        }

    def company(self, ticker: str, log: ProgressLog = _quiet) -> dict[str, Any]:
        code = (ticker or "").strip().upper() if isinstance(ticker, str) else ""
        if not TICKER.fullmatch(code):
            raise ScreenerError("ticker must be 3 to 10 letters or digits, e.g. FPT or VNM")
        companies, _, results = self.universe(log)
        found = [r for r in results if r.company.code == code]
        liquid = any(c.code == code for c in companies)
        if not found and not liquid:  # too illiquid for the screen's universe: fetch it on its own
            try:
                extra = load_one(self.client, self.cfg, code, log)
            except CacheMiss as e:
                raise ScreenerError(f"offline mode, and {code} isn't stored yet; run once online") from e
            except SOURCE_ERRORS as e:
                raise ScreenerError(f"VNDirect's data API didn't answer as expected for {code} "
                                    f"({type(e).__name__}: {e}); try again in a few minutes") from e
            if extra is None:
                raise ScreenerError(f"{code}: not found among listed stocks on HOSE, HNX or UPCOM")
            if extra.kind == "financial" or code in FINANCIAL_OVERRIDES:
                raise ScreenerError(f"{code} is a bank, insurer, broker or lender: these screens don't apply to it")
            found = [r for r in analyse(companies + [extra], self.cfg, median_from=companies) if r.company.code == code]
        if not found:
            kind = next((c.kind for c in companies if c.code == code), "no data")
            if kind == "financial":
                raise ScreenerError(f"{code} is a bank, insurer, broker or lender: these screens don't apply to it")
            raise ScreenerError(f"{code}: no usable data (no price, or no annual statements)")
        r = found[0]
        try:
            add_year_end_prices(self.client, r.company)
        except (CacheMiss, *SOURCE_ERRORS):
            pass  # only the year-end P/E column needs them; it stays null
        return self._detail(r, liquid)

    # ------------------------------------------------------------------ shapes

    def _row(self, r: Result) -> dict[str, Any]:
        c, v, f = r.company, r.value, r.fisher
        return {
            "ticker": c.code, "name": c.name, "exchange": c.exchange, "price_vnd": _vnd(c.price),
            "pe_3y": _r(v.pe3, 1), "pe_last_4q": _r(f.pe_ttm, 1), "pb": _r(v.pb, 2),
            "graham_number_vnd": _vnd(v.graham_number), "price_vs_graham_number": _price_vs_graham(c, r),
            "graham_defensive": Result.score(r.defensive), "graham_enterprising": Result.score(r.enterprising),
            "quality": Result.score(r.quality), "net_net": r.net_net,
            "sales_growth_5y": _r(f.sales_cagr5), "profit_growth_5y": _r(f.profit_cagr5),
            "operating_margin_5y": _r(f.op_margin5), "roe_5y": _r(f.roe5),
            "profit_last_4q_vs_year_before": _r(f.ttm_growth), "vs_1y_high": _r(f.vs_high_1y),
            "failed": [ch.key for ch in r.defensive + r.quality if ch.passed is False],
            "warnings": _warnings(r),
        }

    def _detail(self, r: Result, liquid: bool) -> dict[str, Any]:
        c, v, f = r.company, r.value, r.fisher
        year = c.latest_year
        years = [y for y in range(year - 9, year + 1) if y in c.years] if year else []
        history = []
        for y in years:
            sales, op, profit = c.get(y, "sales"), c.get(y, "operating_profit"), c.get(y, "npat_parent")
            equity, ca, cl = c.get(y, "parent_equity"), c.get(y, "current_assets"), c.get(y, "current_liabilities")
            dividends = c.get(y, "dividends_paid")
            history.append({
                "year": y, "sales_bn": _bn(sales),
                "operating_margin": _r(op / sales) if sales and op is not None else None,
                "net_profit_parent_bn": _bn(profit),
                "eps_vnd_on_todays_shares": _vnd(profit / c.shares) if profit is not None and c.shares else None,
                "roe": _r(profit / equity) if profit is not None and equity else None,
                "current_ratio": _r(ca / cl, 2) if ca and cl else None,
                "long_term_debt_bn": _bn(c.get(y, "lt_debt")),
                "dividends_paid_bn": None if dividends is None else _bn(-dividends),
                "operating_cash_flow_bn": _bn(c.get(y, "cfo")),
                "shares_issued_for_cash_bn": _bn(c.get(y, "share_issue")),
                "year_end_pe": _r(year_end_pe(c, y), 1),
            })
        quarters = []
        for d in sorted(d for d, q in c.quarters.items() if "sales" in q or "npat_parent" in q)[-8:]:
            q = c.quarters[d]
            profit = q.get("npat_parent", q.get("npat"))
            prior = c.quarters.get(f"{int(d[:4]) - 1}{d[4:]}", {})
            before = prior.get("npat_parent", prior.get("npat"))
            quarters.append({"quarter_end": d, "sales_bn": _bn(q.get("sales")), "net_profit_parent_bn": _bn(profit),
                             "profit_vs_same_quarter_year_before":
                                 _r(profit / before - 1) if profit is not None and before and before > 0 else None})
        lists = select_lists([r], self.cfg)
        warnings = _warnings(r)
        if not liquid:
            warnings.append(f"Trades less than {self.cfg.min_daily_value / 1e9:g} bn VND a day, so it is outside the "
                            "screen's universe: prices can move a lot on small orders.")
        if v.pe3 is not None and v.pe3 < 6:
            warnings.append("A very low P/E often follows a crash or a one-off gain: the numbers can't say which. "
                            "Read the news and the annual report before calling it cheap.")
        return {
            "ticker": c.code, "name": c.name, "exchange": c.exchange, "icb_sector": c.sector,
            "price_vnd": _vnd(c.price), "price_date": c.price_date, "market_cap_bn": _bn(c.market_cap),
            "shares_million": _r(c.shares / 1e6, 1) if c.shares else None, "shares_as_of": c.shares_as_of,
            "avg_daily_traded_bn": _bn(c.avg_value_20d),
            "valuation": {
                "pe_3y": _r(v.pe3, 1), "pe_last_4q": _r(f.pe_ttm, 1), "pb": _r(v.pb, 2),
                "graham_number_vnd": _vnd(v.graham_number), "price_vs_graham_number": _price_vs_graham(c, r),
                "net_current_assets_per_share_vnd": _vnd(v.ncav_ps),
                "tangible_book_per_share_vnd": _vnd(v.tangible_ps),
            },
            "price_1y": {"change": _r(f.change_1y), "vs_high": _r(f.vs_high_1y), "high_vnd": _vnd(c.high_1y)},
            "scores": {"graham_defensive": _score(r.defensive), "graham_enterprising": _score(r.enterprising),
                       "quality": _score(r.quality)},
            "net_net": r.net_net,
            "on_lists": [key for key in LISTS if lists[key]],
            "checks": {"graham_defensive": _checks(r.defensive), "graham_enterprising": _checks(r.enterprising),
                       "quality": _checks(r.quality)},
            "growth": {"sales_growth_5y": _r(f.sales_cagr5), "profit_growth_5y": _r(f.profit_cagr5),
                       "operating_margin_5y": _r(f.op_margin5), "roe_5y": _r(f.roe5),
                       "profit_last_4q_bn": _bn(f.ttm_profit), "profit_last_4q_vs_year_before": _r(f.ttm_growth),
                       "last_quarter_used": f.ttm_to},
            "ten_years": history,
            "recent_quarters": quarters,
            "warnings": warnings,
            "fisher_questions_numbers_cannot_answer": [
                {"point": n, "question": question, "where_to_look": where} for n, question, where in FISHER_POINTS],
            "data_notes": [
                "Annual consolidated statements from VNDirect; check key figures against the audited report.",
                "EPS for every year uses today's share count, so stock dividends don't hide growth.",
                ("Shares = VNDirect market cap ÷ that day's close (else paid-in capital ÷ par value); both include "
                 "shares held by subsidiaries and preferred shares."),
                "Tangible book removes goodwill only: in Vietnam most intangible assets are land-use rights.",
                "One-off gains stay inside profit; P/B and debt use the latest year-end balance sheet.",
            ],
            "data_source": DATA_SOURCE,
            "disclaimer": DISCLAIMER,
        }


def methodology(cfg: Config | None = None) -> dict[str, Any]:
    """Every test and threshold, read from the engine's Config, so this never drifts from what the screens do."""
    cfg = cfg or Config()
    return {
        "purpose": ("Finds Vietnamese listed companies worth studying with Benjamin Graham's and Philip Fisher's "
                    "methods. A screen is a reading list, not a recommendation."),
        "universe": (f"Stocks listed on {', '.join(cfg.exchanges)} that trade at least "
                     f"{cfg.min_daily_value / 1e9:g} bn VND a day on average over 20 days. Banks, insurers, brokers "
                     "and lenders are left out: they need another method."),
        "lists": {key: {"title": LISTS[key], "rule": LIST_RULES[key]} for key in LISTS},
        "graham_defensive_tests": [
            f"Size: sales at least {cfg.min_sales / 1e9:,.0f} bn VND.",
            (f"Financial condition: current ratio at least {cfg.min_current_ratio:g}, and long-term debt no more than "
            "working capital."),
            f"Earnings stability: a profit in each of the last {cfg.earnings_years} years.",
            (f"Dividend record: cash dividends in each of the last {cfg.dividend_years} years (Graham asked 20; HOSE "
            f"opened in 2000), each at least {cfg.min_dividend_share:.0%} of that year's profit."),
            (f"Earnings growth: the last 3 years' average profit at least {cfg.min_growth:.0%} above the average of "
            "years 7 to 9 before."),
            f"Moderate P/E: price at most {cfg.max_pe3:g} × the last 3 years' average earnings.",
            f"Moderate price to assets: P/B at most {cfg.max_pb:g}, or P/E × P/B at most {cfg.max_pe_x_pb:g}.",
        ],
        "graham_number": "√(22.5 × EPS × book value per share): the most a defensive investor would pay.",
        "graham_enterprising_tests": [
            f"Current ratio at least {cfg.ent_current_ratio:g}.",
            f"Long-term debt at most {cfg.ent_debt_to_wc:.0%} of working capital.",
            f"A profit in each of the last {cfg.ent_earnings_years} years.",
            f"Some dividend: at least {cfg.min_dividend_share:.0%} of the latest year's profit paid in cash.",
            f"Last year's profit above the profit {cfg.ent_growth_years} years before.",
            f"Price at most {cfg.ent_max_price_to_tangible:.0%} of tangible book value.",
        ],
        "net_net": "Price below two thirds of net current asset value (current assets minus all liabilities).",
        "quality_signals": [
            f"Sales growth: 5-year compound growth at least {cfg.min_sales_cagr5:.0%} (Fisher point 1).",
            f"Steady sales: up in at least {cfg.min_sales_up_years} of the last 10 years (point 1).",
            "Operating margin above the median of the screened companies (point 5).",
            (f"Margins holding: the last 3 years' net margin at least {cfg.min_margin_retained:.0%} of the 3 before "
            "(point 6)."),
            f"Return on equity, 5-year average, at least {cfg.min_roe5:.0%}.",
            f"Cash conversion: operating cash flow at least {cfg.min_cash_conversion:.0%} of profit over 5 years.",
            (f"Little dilution: cash raised from share issues in 10 years at most {cfg.max_dilution_10y:.0%} of market "
            "cap (point 13)."),
            f"Debt at most {cfg.max_debt_to_equity:.0%} of equity.",
        ],
        "fisher_growth_list": (f"Sales growth, margin, ROE and dilution signals all pass, with quality at least "
                               f"{cfg.fisher_min_quality} of 8. A price at least {cfg.fisher_fall_from_high:.0%} below "
                               "its one-year high is flagged: a possible buying range only if the cause is temporary, "
                               "which needs reading, not numbers."),
        "known_blind_spots": [
            "Shares held by subsidiaries and preferred shares count as ordinary shares.",
            "Profit is before the bonus-and-welfare fund and staff share schemes, which flatters some companies' P/E.",
            "One-off gains (asset sales, divestments) stay inside profit.",
            "Cyclical companies pass growth tests at the top of their cycle.",
            "A very low P/E often follows a crash: the numbers can't say why.",
        ],
        "thresholds": {f.name: getattr(cfg, f.name) for f in fields(cfg)},
        "data_source": DATA_SOURCE,
        "disclaimer": DISCLAIMER,
        "version": __version__,
    }
