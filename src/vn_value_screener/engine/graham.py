"""Benjamin Graham's stock-selection rules (The Intelligent Investor, ch. 14 and 15) on VN data.

Earnings per share are computed on today's share count (parent profit / current shares).
Vietnamese companies pay many stock dividends, so reported EPS understates growth; using one
share count for every year measures the business's growth instead. Real dilution (shares
sold for cash) is checked separately in quality.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .data import Company


@dataclass
class Check:
    key: str
    label: str
    passed: bool | None  # None: not enough data to decide
    detail: str


@dataclass
class Valuation:
    eps3: float | None = None  # average EPS of the last 3 years, on today's shares
    pe3: float | None = None
    bvps: float | None = None
    pb: float | None = None
    graham_number: float | None = None  # √(22.5 × EPS × BVPS): the most a defensive investor pays
    vs_graham: float | None = None  # graham_number / price − 1; positive = price below it
    tangible_ps: float | None = None
    ncav_ps: float | None = None  # (current assets − all liabilities − minorities − preferred capital) per share


def bn(value: float | None) -> str:
    return "n/a" if value is None else f"{value / 1e9:,.0f} bn"


def series(c: Company, item: str, years: range) -> list[float | None]:
    return [c.get(y, item) for y in years]


def valuation(c: Company) -> Valuation:
    v = Valuation()
    year = c.latest_year
    if year is None or not c.shares or not c.price:
        return v
    profits = series(c, "npat_parent", range(year - 2, year + 1))
    if None not in profits:
        v.eps3 = sum(profits) / 3 / c.shares
        if v.eps3 > 0:
            v.pe3 = c.price / v.eps3
    equity = c.get(year, "parent_equity")
    if equity:
        v.bvps = equity / c.shares
        v.pb = c.price / v.bvps if v.bvps > 0 else None
        # Only goodwill is removed: in Vietnam "intangible assets" are mostly land-use rights.
        v.tangible_ps = (equity - (c.get(year, "goodwill") or 0)) / c.shares
    if v.eps3 and v.eps3 > 0 and v.bvps and v.bvps > 0:
        v.graham_number = math.sqrt(22.5 * v.eps3 * v.bvps)
        v.vs_graham = v.graham_number / c.price - 1
    current_assets, liabilities = c.get(year, "current_assets"), c.get(year, "liabilities")
    if current_assets is not None and liabilities is not None:
        v.ncav_ps = (current_assets - liabilities - (c.get(year, "minority_interest") or 0)
                     - (c.get(year, "preferred_equity") or 0)) / c.shares  # preferred: filings studies only
    return v


def _profit_every_year(c: Company, last: int, n: int) -> tuple[bool | None, str]:
    window = range(last - n + 1, last + 1)
    profits = series(c, "npat_parent", window)
    losses = [y for y, p in zip(window, profits) if p is not None and p <= 0]
    known = sum(p is not None for p in profits)
    if losses:
        return False, f"loss in {', '.join(map(str, losses))}"
    if known < n:
        return None, f"only {known} of {n} years on record (all profitable)"
    return True, f"profitable every year {window[0]}–{last}"


DECLARED_AMOUNT_UNKNOWN = 1.0  # VND: `filings graham`'s "declared for the year, amount unknown" (counts as paid)


def _dividend_year(c: Company, year: int, min_share: float) -> tuple[bool, str | None]:
    """Whether `year` counts as a cash-dividend year; when a payment was made but is too small to count, says so.

    Cash paid (cash-flow item 33600, negative) must be ≥ `min_share` (`Config.min_dividend_share`, 1%) of that year's
    parent profit: a few million VND on hundreds of billions of profit is leftovers of an old declaration or a
    subsidiary's payout to its minorities, not a dividend. In a loss year, or when the profit is missing, there is no
    profit to take 1% of, so any payment > 0 counts, as before 2026-11-23: paying through a loss is the record Graham
    asks for, and a missing profit line shouldn't fail a real payment. `filings graham` marks a dividend declared with
    no amount known as DECLARED_AMOUNT_UNKNOWN: it counts (nothing to judge; 2026-11-24: the floor failed it). The
    filings rank (`filings/rank.py`) applies the same 1% to consolidated net profit (`is.npat`), not parent profit: on
    the 2026-11-24 store 2 of 1,227 company-years fall on different sides (VCP FY2020, VHC FY2014)."""
    paid = -(c.get(year, "dividends_paid") or 0.0)
    if paid <= 0:
        return False, None
    if paid == DECLARED_AMOUNT_UNKNOWN:
        return True, None
    profit = c.get(year, "npat_parent")
    if profit is not None and profit > 0 and paid < min_share * profit:
        amount = f"{paid / 1e9:,.2f} bn" if paid >= 1e7 else f"{paid / 1e6:,.1f} M"
        share = "< 0.01%" if paid < 1e-4 * profit else f"{paid / profit:.2%}"
        return False, f"{amount} ({share} of profit)"
    return True, None


def _dividends_every_year(c: Company, last: int, n: int,
                          min_share: float = Config.min_dividend_share) -> tuple[bool | None, str]:
    window = range(last - n + 1, last + 1)
    unknown = [y for y in window if y not in c.years]
    unpaid, tiny = [], []
    for y in (y for y in window if y in c.years):
        counts, why = _dividend_year(c, y, min_share)
        if why:
            tiny.append(f"{y} {why}")
        elif not counts:
            unpaid.append(y)
    if unpaid or tiny:
        parts = [f"no cash dividend in {', '.join(map(str, unpaid))}"] if unpaid else []
        if tiny:
            parts.append(f"below {min_share:.0%} of profit, not counted: {', '.join(tiny)}")
        return False, "; ".join(parts)
    if unknown:
        return None, f"paid every year on record; {len(unknown)} of {n} years missing"
    return True, f"cash dividends every year {window[0]}–{last}"


def _working_capital(c: Company, year: int) -> tuple[float | None, float | None, float]:
    current_assets, current_liabilities = c.get(year, "current_assets"), c.get(year, "current_liabilities")
    lt_debt = c.get(year, "lt_debt") or 0.0
    if current_assets is None or not current_liabilities:
        return None, None, lt_debt
    return current_assets / current_liabilities, current_assets - current_liabilities, lt_debt


def defensive(c: Company, cfg: Config, v: Valuation) -> list[Check]:
    """The seven tests for the defensive investor."""
    year = c.latest_year
    checks: list[Check] = []

    sales = c.get(year, "sales")
    checks.append(Check("size", "Adequate size", None if sales is None else sales >= cfg.min_sales,
                        f"sales {bn(sales)} VND in {year} (rule: ≥ {bn(cfg.min_sales)})"))

    ratio, wc, lt_debt = _working_capital(c, year)
    if ratio is None:
        checks.append(Check("finance", "Strong financial condition", None, "current assets or liabilities missing"))
    else:
        checks.append(Check(
            "finance", "Strong financial condition", ratio >= cfg.min_current_ratio and lt_debt <= wc,
            f"current ratio {ratio:.2f} (rule ≥ {cfg.min_current_ratio:g}); long-term debt {bn(lt_debt)} "
            f"vs working capital {bn(wc)} (rule: debt ≤ working capital)"))

    passed, detail = _profit_every_year(c, year, cfg.earnings_years)
    checks.append(Check("stability", f"Profit in each of the last {cfg.earnings_years} years", passed, detail))

    passed, detail = _dividends_every_year(c, year, cfg.dividend_years, cfg.min_dividend_share)
    checks.append(Check("dividends", f"Cash dividend in each of the last {cfg.dividend_years} years", passed, detail))

    first_year = year - cfg.earnings_years + 1  # the windows' middles are 7 years apart, not 10 (docs/screen)
    first = series(c, "npat_parent", range(first_year, first_year + 3))
    last = series(c, "npat_parent", range(year - 2, year + 1))
    if None in first or None in last:
        checks.append(Check("growth", "Earnings up by a third in 10 years", None, "not enough years on record"))
    elif sum(first) <= 0:
        checks.append(Check("growth", "Earnings up by a third in 10 years", None, "losses at the start of the window"))
    else:
        growth = sum(last) / sum(first) - 1
        checks.append(Check("growth", "Earnings up by a third in 10 years", growth >= cfg.min_growth,
                            f"3-year average profit FY{year - 2}–{year} {growth:+.0%} vs FY{first_year}–{first_year + 2} "
                            f"(rule ≥ +{cfg.min_growth:.0%})"))

    if v.eps3 is None:
        checks.append(Check("pe", "Moderate price/earnings", None, "earnings history too short"))
    elif v.eps3 <= 0:
        checks.append(Check("pe", "Moderate price/earnings", False, "average earnings of the last 3 years ≤ 0"))
    else:
        checks.append(Check("pe", "Moderate price/earnings", v.pe3 <= cfg.max_pe3,
                            f"P/E on 3-year average earnings {v.pe3:.1f} (rule ≤ {cfg.max_pe3:g})"))

    if v.pb is None:
        checks.append(Check("pb", "Moderate price/book", None, "book value missing or negative"))
    else:
        product = v.pe3 * v.pb if v.pe3 else None
        ok = v.pb <= cfg.max_pb or (product is not None and product <= cfg.max_pe_x_pb)
        checks.append(Check("pb", "Moderate price/book", ok,
                            f"P/B {v.pb:.2f} (rule ≤ {cfg.max_pb:g}, or P/E × P/B ≤ {cfg.max_pe_x_pb:g}; "
                            f"P/E × P/B = {product:.1f})" if product else f"P/B {v.pb:.2f} (rule ≤ {cfg.max_pb:g})"))
    return checks


def enterprising(c: Company, cfg: Config, v: Valuation) -> list[Check]:
    """The six looser tests for the enterprising investor."""
    year = c.latest_year
    checks: list[Check] = []
    ratio, wc, lt_debt = _working_capital(c, year)
    checks.append(Check("e_current", "Current ratio ≥ 1.5", None if ratio is None else ratio >= cfg.ent_current_ratio,
                        "n/a" if ratio is None else f"current ratio {ratio:.2f}"))
    checks.append(Check("e_debt", "Long-term debt ≤ 110% of working capital",
                        None if wc is None else lt_debt <= cfg.ent_debt_to_wc * wc,
                        "n/a" if wc is None else f"long-term debt {bn(lt_debt)} vs working capital {bn(wc)}"))
    passed, detail = _profit_every_year(c, year, cfg.ent_earnings_years)
    checks.append(Check("e_stability", f"Profit in each of the last {cfg.ent_earnings_years} years", passed, detail))
    paid = c.get(year, "dividends_paid")
    counts, why = _dividend_year(c, year, cfg.min_dividend_share)
    checks.append(Check("e_dividend", "Some current dividend", counts,
                        f"cash dividends paid in {year}: "
                        + (f"{why}, below {cfg.min_dividend_share:.0%}: not counted" if why else bn(-(paid or 0)))))
    now, before = c.get(year, "npat_parent"), c.get(year - cfg.ent_growth_years, "npat_parent")
    checks.append(Check("e_growth", f"Profit above {cfg.ent_growth_years} years ago",
                        None if now is None or before is None else now > before,
                        f"{bn(now)} in {year} vs {bn(before)} in {year - cfg.ent_growth_years}"))
    if v.tangible_ps is None or v.tangible_ps <= 0:
        checks.append(Check("e_tangible", "Price ≤ 120% of net tangible assets", None if v.tangible_ps is None else False,
                            "tangible book value missing or negative"))
    else:
        ratio_t = c.price / v.tangible_ps
        checks.append(Check("e_tangible", "Price ≤ 120% of net tangible assets",
                            ratio_t <= cfg.ent_max_price_to_tangible, f"price is {ratio_t:.0%} of tangible book"))
    return checks


def is_net_net(c: Company, v: Valuation) -> bool:
    """Graham's bargain: price below two thirds of net current asset value."""
    return bool(v.ncav_ps and v.ncav_ps > 0 and c.price and c.price < v.ncav_ps * 2 / 3)
