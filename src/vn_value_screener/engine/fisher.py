"""Philip Fisher's growth-stock lens, as far as numbers can take it.

Fisher (Common Stocks and Uncommon Profits) looks for a company that can grow sales for years,
earns a margin worth having, and grows without selling many new shares; then he buys when the
price is fair, ideally when a temporary trouble has knocked it down (ch. 5, "When to Buy").

The numbers here only shortlist. Whether the growth will last, whether newcomers can easily
enter (ch. 10), and whether a price fall comes from a temporary trouble are questions for the
reader: annual reports, news and scuttlebutt.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .config import Config
from .data import Company
from .graham import Check
from .quality import operating_margin_5y

# The measurable core of the Fisher list: sales growth (point 1), an above-median margin (point 5),
# a return that makes the growth worth having, and growth paid for without many new shares (point 13).
CORE = ("q_growth", "q_margin", "q_roe", "q_dilution")


@dataclass
class FisherView:
    sales_cagr5: float | None = None
    profit_cagr5: float | None = None  # parent profit; equal to EPS growth on today's share count
    op_margin5: float | None = None
    roe5: float | None = None
    ttm_profit: float | None = None  # last four quarters, parent profit
    ttm_growth: float | None = None  # last four quarters vs the four before
    ttm_to: str | None = None  # quarter-end date of the newest quarter used
    pe_ttm: float | None = None
    vs_high_1y: float | None = None
    change_1y: float | None = None


def _cagr(now: float | None, before: float | None, years: int) -> float | None:
    if not now or not before or now <= 0 or before <= 0:
        return None
    return (now / before) ** (1 / years) - 1


def _quarter_index(date: str) -> int:
    d = dt.date.fromisoformat(date)
    return d.year * 4 + (d.month - 1) // 3


def trailing(c: Company, item: str) -> tuple[float | None, float | None, str | None]:
    """(last 4 quarters, the 4 quarters before, newest quarter-end) — only if 8 quarters in a row exist."""
    dated = sorted((d, v[item]) for d, v in c.quarters.items() if v.get(item) is not None)
    if len(dated) < 8:
        return None, None, None
    last8 = dated[-8:]
    if _quarter_index(last8[-1][0]) - _quarter_index(last8[0][0]) != 7:  # a gap: don't guess
        return None, None, dated[-1][0]
    return sum(v for _, v in last8[4:]), sum(v for _, v in last8[:4]), last8[-1][0]


def fisher_view(c: Company) -> FisherView:
    view = FisherView(vs_high_1y=c.vs_high_1y, change_1y=c.change_1y, op_margin5=operating_margin_5y(c))
    year = c.latest_year
    if year is None:
        return view
    view.sales_cagr5 = _cagr(c.get(year, "sales"), c.get(year - 5, "sales"), 5)
    view.profit_cagr5 = _cagr(c.get(year, "npat_parent"), c.get(year - 5, "npat_parent"), 5)
    roes = [p / e for y in range(year - 4, year + 1)
            if (p := c.get(y, "npat_parent")) is not None and (e := c.get(y, "parent_equity")) and e > 0]
    view.roe5 = sum(roes) / len(roes) if len(roes) >= 3 else None
    now, before, view.ttm_to = trailing(c, "npat_parent")
    if now is not None:
        view.ttm_profit = now
        view.ttm_growth = now / before - 1 if before and before > 0 else None
        if now > 0 and c.market_cap:
            view.pe_ttm = c.market_cap / now
    return view


def is_candidate(quality_checks: list[Check], cfg: Config) -> bool:
    passed = {ch.key: ch.passed for ch in quality_checks}
    return (all(passed.get(key) is True for key in CORE)
            and sum(p is True for p in passed.values()) >= cfg.fisher_min_quality)


def fallen(view: FisherView, cfg: Config) -> bool:
    """Price well below its high of the year: maybe a ch. 5 buying range, maybe a real problem."""
    return view.vs_high_1y is not None and view.vs_high_1y <= -cfg.fisher_fall_from_high
