"""Every threshold the screens use, in one place. Values are in VND unless noted."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    exchanges: tuple[str, ...] = ("HOSE", "HNX", "UPCOM")
    # Skip stocks you couldn't buy or sell without moving the price: 20-day average traded value.
    min_daily_value: float = 1e9
    history_years: int = 14

    # Graham, defensive investor (The Intelligent Investor, ch. 14), adapted to Vietnam.
    min_sales: float = 1_000e9  # Graham: $100M of sales in 1972. ~1,000 bn VND keeps mid-caps in.
    min_current_ratio: float = 2.0  # current assets ≥ 2 × current liabilities
    earnings_years: int = 10  # a profit in each of the last N years
    dividend_years: int = 10  # Graham asks 20; HOSE only opened in 2000, so 10 is the VN version
    # A year counts as a dividend year (defensive and enterprising tests) only if the cash paid (cash-flow item
    # 33600) is ≥ 1% of that year's parent profit: less is leftovers of earlier declarations or minorities' payouts,
    # not a dividend policy (VGS FY2025: 3 M VND on 217 bn; VCS FY2013: 0.12 bn on 68 bn). Set 2026-11-23 before
    # measuring; the filings rank uses the same 1% (RankConfig.min_dividend_share) on consolidated net profit.
    min_dividend_share: float = 0.01
    min_growth: float = 1 / 3  # earnings up a third: last 3-year average vs first 3-year average
    max_pe3: float = 15.0  # price ≤ 15 × average earnings of the last 3 years
    max_pb: float = 1.5  # price ≤ 1.5 × book value …
    max_pe_x_pb: float = 22.5  # … or P/E × P/B ≤ 22.5

    # Graham, enterprising investor (ch. 15).
    ent_current_ratio: float = 1.5
    ent_debt_to_wc: float = 1.1  # long-term debt ≤ 110% of working capital
    ent_earnings_years: int = 5
    ent_growth_years: int = 5  # last year's profit above the profit 5 years earlier
    ent_max_price_to_tangible: float = 1.2

    # Quality signals — the parts of Fisher's 15 points that numbers can show.
    min_sales_cagr5: float = 0.10
    min_sales_up_years: int = 7  # sales up in ≥ 7 of the last 10 years
    min_margin_retained: float = 0.95  # net margin, last 3 years ≥ 95% of the 3 years before
    min_roe5: float = 0.15
    min_cash_conversion: float = 0.8  # operating cash flow ≥ 80% of profit over 5 years
    max_dilution_10y: float = 0.10  # cash raised from share issues in 10 years ≤ 10% of market cap
    max_debt_to_equity: float = 0.5

    # Fisher growth list: quality ≥ this many of 8, with growth, margin, ROE and dilution all passing
    # (points 1, 5 and 13, plus a return that shows the growth is worth having).
    fisher_min_quality: int = 6
    # Fisher ch. 5: a good company whose price has fallen a lot may be in a "buying range" — if the
    # trouble behind the fall is temporary. The screen only flags the fall; the reason needs reading.
    fisher_fall_from_high: float = 0.20
    price_history_days: int = 365


@dataclass(frozen=True)
class RankConfig:
    """`filings rank`, the quality filter on the audited reports in the store (filings/rank.py). Its own class, so the
    screen's `Config` (and `paper`'s hash of it) doesn't change. Where a test is one of the screen's, rank.py takes
    the screen's threshold from `Config` (dividend_years, min_growth, min_current_ratio, min_roe5, min_margin_retained,
    min_cash_conversion); these are the filter's own. Set before the first run (the trend and related-party values just
    after it, see below); never tuned to let a company through."""
    window_years: int = 10  # the record the tests read: Graham's defensive record is 10 years (Config.earnings_years)
    long_years: int = 20  # Graham's original span: losses are also counted over 20 years (shown, not a drop)
    min_years: int = 10  # fewer audited years can't show a 10-year record (`--min-years`)
    opinion_years: int = 5  # a qualified, adverse or disclaimer opinion this recent: the accounts can't carry a record
    going_concern_years: int = 10  # a going-concern emphasis or section within the 10-year record drops the company
    min_test_years: int = 7  # a 10-year average (ROE, cash conversion, capex) needs at least 7 of the 10 years
    # Data not verified (tier D, not dropped):
    min_identity_pass_rate: float = 0.90  # more than 1 failing identity in 10: the machine reading is too often wrong
    max_unknown_opinions: float = 1 / 3  # over a third of the opinions unread (whole record, or the 5-year window)
    max_report_lag: int = 1  # the newest audited report more than this many years before the last full fiscal year:
    #   the record stops, and the tests would grade an old company (added 2026-11-12 at the owner's request: HDC graded
    #   B on FY2016, GIL and HQC on FY2018, DXP on FY2008, all "done" in the bulk state). 1 year lets in a March
    #   year-end (CMG) or a report signed late (DGC FY2025: 20 June 2026)
    min_dividend_share: float = Config.min_dividend_share  # the screen's floor (cash paid ≥ 1% of that year's
    #   profit counts as a dividend year), kept here because rank.py reads it from RankConfig
    # Tests the screen doesn't have:
    max_issued_growth: float = 0.10  # shares issued for money or assets ≤ 10% in 10 years (Fisher 13; the screen
    #                                  allows cash raised ≤ 10% of market cap, Config.max_dilution_10y)
    shares_mismatch: float = 0.10  # audited share count vs VNDirect's today: flag above 10% (the screen's ⚠ shares)
    # Added 2026-11-08, after the first run, because the owner pointed at VCS: tier A 7/8 despite profit falling four
    # years running (1,772 bn in 2021 → 694 bn in 2025) and 1,687 bn lent to its controller's companies. Both make the
    # filter stricter. The values are set once from Graham's average earning power and plain materiality, not fitted to
    # VCS: it sits far past each line (trend 0.71; loans 29% of assets and 33% of equity; related sales 39%), so any
    # nearby choice gives the same answer, and the same lines also catch VNM (trend) and would catch any other company.
    min_trend_ratio: float = 1.0  # trend: the last 3 years' average parent profit ≥ the average of the 10-year window.
    #   Graham judges a company on its average earning power; a business now earning below its own decade's average is
    #   in decline, however much it grew from a low base (VCS's decade test read +241%). 1.0 is the no-decline line.
    rp_loans_assets: float = 0.10  # red flag: loans, advances or deposits placed with the controller's or parent
    #   group's companies ≥ 10% of total assets: a tenth of everything the company owns depends on its owner's credit
    rp_loans_equity: float = 0.25  # … or ≥ 25% of parent equity: a quarter of the shareholders' money lent to the owner
    rp_sales_revenue: float = 0.30  # sales to related parties ≥ 30% of revenue: the controller sets the price of a
    #   third of the business, so margins and receivables can't be read as arm's length
    rp_receivables_current: float = 0.30  # related-party receivables ≥ 30% of current assets: the current ratio and
    #   net current assets (Graham's tests) would rest on what the controller's group owes
    red_flag_years: int = 5  # a red flag dated in the last 5 audited years, or later, drops the company (the window of
    #   the opinion rule); an older one is shown as history
    # Tiers among the companies not dropped:
    a_min_passed: int = 8  # A: at least 8 of the 9 quality tests pass (at most one fails, as with 7 of 8 before
    #                        the trend test) …
    a_required: tuple[str, ...] = ("cash_conversion", "trend")  # … including: profit turns into cash (the owner's
    #   rule: profit is trusted only where it becomes cash) and no decline against its own decade (a company that
    #   fails it is at most B)
