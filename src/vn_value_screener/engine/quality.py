"""Quality signals: the parts of Philip Fisher's 15 points (Common Stocks and Uncommon Profits,
ch. 3) that a company's numbers can show. The rest — people, research, management, integrity —
needs scuttlebutt, so the company report turns those points into a worksheet instead.
"""
from __future__ import annotations

from .config import Config
from .data import Company
from .graham import Check, bn


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _margin(c: Company, year: int, item: str) -> float | None:
    sales, profit = c.get(year, "sales"), c.get(year, item)
    return profit / sales if sales and profit is not None else None


def operating_margins_5y(c: Company) -> list[float]:
    """Operating margins of the last 5 fiscal years that have one (sales and operating profit both known)."""
    year = c.latest_year
    return [m for y in range(year - 4, year + 1) if (m := _margin(c, y, "operating_profit")) is not None]


def operating_margin_5y(c: Company) -> float | None:
    margins = operating_margins_5y(c)
    return _mean(margins) if len(margins) >= 3 else None


def quality(c: Company, cfg: Config, median_operating_margin: float | None) -> list[Check]:
    year = c.latest_year
    checks: list[Check] = []

    now, five_ago = c.get(year, "sales"), c.get(year - 5, "sales")
    if now and five_ago and now > 0 and five_ago > 0:
        cagr = (now / five_ago) ** (1 / 5) - 1
        checks.append(Check("q_growth", "Sales growing ≥ 10% a year (5 years) · Fisher 1", cagr >= cfg.min_sales_cagr5,
                            f"sales {cagr:+.1%} a year, {year - 5}–{year}"))
    else:
        checks.append(Check("q_growth", "Sales growing ≥ 10% a year (5 years) · Fisher 1", None,
                            "not enough sales history"))

    ups = known = 0
    for y in range(year - 9, year + 1):
        before, after = c.get(y - 1, "sales"), c.get(y, "sales")
        if before and after:
            known += 1
            ups += after > before
    passed = ups >= cfg.min_sales_up_years if known == 10 or ups >= cfg.min_sales_up_years else None
    checks.append(Check("q_steady", f"Sales up in ≥ {cfg.min_sales_up_years} of the last 10 years · Fisher 1",
                        passed, f"up in {ups} of {known} years on record"))

    margin = operating_margin_5y(c)
    if margin is None or median_operating_margin is None:
        checks.append(Check("q_margin", "Operating margin above the median · Fisher 5", None, "not enough history"))
    else:  # the average runs on 3–5 years: say how many (2026-11-24: it always said "5-year")
        checks.append(Check("q_margin", "Operating margin above the median · Fisher 5",
                            margin >= median_operating_margin,
                            f"{len(operating_margins_5y(c))}-year operating margin {margin:.1%} vs "
                            f"{median_operating_margin:.1%} median of the screened companies"))

    # config.min_margin_retained: the last 3 years against the 3 before, so both windows need all 3 years
    # (2026-11-24: one earlier year was enough and the detail still said "3 years before"; HPA on FY2022 alone).
    recent_m = [m for y in range(year - 2, year + 1) if (m := _margin(c, y, "npat_parent")) is not None]
    earlier_m = [m for y in range(year - 5, year - 2) if (m := _margin(c, y, "npat_parent")) is not None]
    if len(recent_m) < 3 or len(earlier_m) < 3:
        checks.append(Check("q_margin_trend", "Margins holding up · Fisher 6", None,
                            f"not enough history (net margin for {len(recent_m)} of the last 3 years, "
                            f"{len(earlier_m)} of the 3 before)"))
    else:
        recent, earlier = _mean(recent_m), _mean(earlier_m)
        ok = recent >= cfg.min_margin_retained * earlier if earlier > 0 else recent > earlier
        checks.append(Check("q_margin_trend", "Margins holding up · Fisher 6", ok,
                            f"net margin {recent:.1%} (last 3 years) vs {earlier:.1%} (3 years before)"))

    roes = [p / e for y in range(year - 4, year + 1)
            if (p := c.get(y, "npat_parent")) is not None and (e := c.get(y, "parent_equity")) and e > 0]
    roe = _mean(roes) if len(roes) >= 3 else None
    checks.append(Check("q_roe", "Return on equity ≥ 15% (5-year average)", None if roe is None else roe >= cfg.min_roe5,
                        "not enough history" if roe is None else f"ROE {roe:.1%} on average over {len(roes)} years"))

    # Cash and profit of the same years only (2026-11-24: a year with profit but no cash-flow figure yet, as when
    # VNDirect loads the cash flow days after the income statement, went into the profit sum alone).
    pairs = [(cf, p) for y in range(year - 4, year + 1)
             if (cf := c.get(y, "cfo")) is not None and (p := c.get(y, "npat")) is not None]
    cfo, profit = [cf for cf, _ in pairs], [p for _, p in pairs]
    if len(pairs) < 3:
        checks.append(Check("q_cash", "Profits turn into cash", None, "not enough cash-flow history"))
    elif sum(profit) <= 0:
        checks.append(Check("q_cash", "Profits turn into cash", False, f"no profit over the last {len(pairs)} years"))
    else:
        conversion = sum(cfo) / sum(profit)
        checks.append(Check("q_cash", "Profits turn into cash", conversion >= cfg.min_cash_conversion,
                            f"operating cash flow = {conversion:.0%} of net profit over {len(pairs)} years "
                            f"(rule ≥ {cfg.min_cash_conversion:.0%})"))

    raised = sum(max(x, 0) for y in range(year - 9, year + 1) if (x := c.get(y, "share_issue")) is not None)
    cap = c.market_cap
    checks.append(Check(
        "q_dilution", "Grows without selling many new shares · Fisher 13",
        None if not cap else raised <= cfg.max_dilution_10y * cap,
        "market cap unknown" if not cap else
        f"raised {bn(raised)} VND from share issues in 10 years = {raised / cap:.0%} of today's market cap"))

    equity = c.get(year, "parent_equity")
    debt = (c.get(year, "st_debt") or 0) + (c.get(year, "lt_debt") or 0)
    checks.append(Check("q_debt", "Conservative borrowing", None if not equity or equity <= 0 else
                        debt / equity <= cfg.max_debt_to_equity,
                        "equity missing or negative" if not equity or equity <= 0 else
                        f"borrowings {bn(debt)} = {debt / equity:.0%} of equity (rule ≤ {cfg.max_debt_to_equity:.0%})"))
    return checks


# Fisher's 15 points, in short paraphrase, with where to look in Vietnam. The numbered checks
# above cover the measurable part of points 1, 5, 6 and 13; everything here is for the reader.
FISHER_POINTS = [
    (1, "Do its products have room to grow sales a lot for several years?",
     "Industry size vs company sales; annual report (Báo cáo thường niên) strategy section."),
    (2, "Is management building the next products for when today's growth slows?",
     "AGM documents (tài liệu ĐHĐCĐ): 5-year plan, new business lines, what they invest in."),
    (3, "How effective is its research and development for its size?",
     "Rarely disclosed in VN — look for new products launched, patents, tech hires."),
    (4, "Does it have an above-average sales organisation?",
     "Distribution network, market share trends, how customers talk about it."),
    (5, "Does it earn a worthwhile profit margin?", "See the margin check; compare with direct competitors."),
    (6, "What is it doing to keep or raise margins?", "Cost programmes, pricing power, raw-material exposure."),
    (7, "Are labour and people relations outstanding?", "Employee reviews (TopCV, ITviec, Glassdoor), turnover, strikes."),
    (8, "Are relations among the executives good?", "Leadership changes, board disputes in the news, AGM minutes."),
    (9, "Does it have depth of management (not a one-person company)?", "Board and officer list, succession, who runs units."),
    (10, "Are its cost analysis and accounting controls good?", "Auditor (Big 4?), qualified opinions, restatements."),
    (11, "What industry-specific edge does it have over competitors?", "Licences, land bank, brand, network, scale."),
    (12, "Does it take a long-range view of profits?", "Reinvestment vs payouts; does it sacrifice next year for later?"),
    (13, "Will growth need so many new shares that owners gain little?", "See the dilution check; rights issues in events."),
    (14, "Does management talk openly in bad times as well as good?", "Compare their AGM / results commentary in bad years."),
    (15, "Is management's integrity beyond question?",
     "Related-party transactions in the notes, insider selling, SSC/HOSE penalties, state/major-holder conflicts."),
]
