"""Runs every screen and writes the Markdown/CSV reports."""
from __future__ import annotations

import csv
import datetime as dt
import statistics
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .data import FINANCIAL_OVERRIDES, Company
from .fisher import FisherView, fallen, fisher_view, is_candidate
from .graham import Check, Valuation, bn, defensive, enterprising, is_net_net, valuation
from .quality import FISHER_POINTS, operating_margin_5y, quality

DISCLAIMER = ("For learning and research, not financial advice. A screen only finds companies worth reading "
              "about; the decision, and checking the numbers against the annual reports, is yours.")


@dataclass
class Result:
    company: Company
    value: Valuation
    defensive: list[Check]
    enterprising: list[Check]
    quality: list[Check]
    net_net: bool
    fisher: FisherView

    @staticmethod
    def score(checks: list[Check]) -> int:
        return sum(ch.passed is True for ch in checks)

    @property
    def misses(self) -> str:
        notes = [ch.key for ch in self.defensive + self.quality if ch.passed is False]
        gap = self.company.share_count_gap
        if gap is not None and abs(gap) > 0.10:  # bonus issue or buyback since the last balance sheet
            notes.append(f"⚠ shares {gap:+.0%} vs last report")
        if self.company.cap_ignored is not None:  # a bad VNDirect market cap: the balance-sheet count was used
            notes.append(f"⚠ market cap implies shares {self.company.cap_ignored:+.0%}: ignored")
        return ", ".join(notes) or "—"


def analyse(companies: list[Company], cfg: Config, median_from: list[Company] | None = None) -> list[Result]:
    """`median_from`: the companies whose median operating margin q_margin compares with (default: `companies`). The
    deep dive of an illiquid ticker passes the screened universe, so the ticker's own margin doesn't move "the median of
    the screened companies" (2026-11-24: it did, and a margin just below the screen's median passed its own deep dive)."""
    industrial = [c for c in companies if c.kind == "industrial" and c.price and c.shares]
    base = industrial if median_from is None else [c for c in median_from
                                                   if c.kind == "industrial" and c.price and c.shares]
    margins = [m for c in base if (m := operating_margin_5y(c)) is not None]
    median = statistics.median(margins) if margins else None
    results = []
    for c in industrial:
        v = valuation(c)
        results.append(Result(c, v, defensive(c, cfg, v), enterprising(c, cfg, v), quality(c, cfg, median),
                              is_net_net(c, v), fisher_view(c)))
    return results


def _price(x: float | None) -> str:
    return "n/a" if x is None else f"{x:,.0f}"


def _num(x: float | None, fmt: str = "{:.1f}") -> str:
    return "n/a" if x is None else fmt.format(x)


def _table(results: list[Result]) -> list[str]:
    lines = ["| Ticker | Company | Exch | Price | P/E 3y | P/B | Graham # | Price vs Graham # | Graham /7 | Quality /8 | Misses |",
             "|---|---|---|--:|--:|--:|--:|--:|--:|--:|---|"]
    for r in results:
        c, v = r.company, r.value
        gap = "n/a" if v.vs_graham is None else f"{-v.vs_graham / (1 + v.vs_graham):+.0%}"
        lines.append(f"| {c.code} | {c.name[:28]} | {c.exchange} | {_price(c.price)} | {_num(v.pe3)} | {_num(v.pb, '{:.2f}')} "
                     f"| {_price(v.graham_number)} | {gap} | {Result.score(r.defensive)} | {Result.score(r.quality)} | {r.misses} |")
    return lines


def _bn(x: float | None) -> str:
    return _num(None if x is None else x / 1e9, "{:,.0f}")


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.0%}"


def _fisher_table(results: list[Result], cfg: Config) -> list[str]:
    lines = [("| Ticker | Company | Exch | Price | Sales/yr 5y | Profit/yr 5y | Op. margin 5y | ROE 5y "
              "| Profit, last 4Q vs year before | P/E last 4Q | P/E 3y | vs 1-yr high | Quality /8 | Misses |"),
             "|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|"]
    for r in results:
        c, f = r.company, r.fisher
        ttm = "n/a" if f.ttm_growth is None else f"{f.ttm_growth:+.0%} (to {f.ttm_to})"
        high = _pct(f.vs_high_1y) + (" ⬇" if fallen(f, cfg) else "")
        lines.append(f"| {c.code} | {c.name[:28]} | {c.exchange} | {_price(c.price)} | {_pct(f.sales_cagr5)} "
                     f"| {_pct(f.profit_cagr5)} | {_num(f.op_margin5, '{:.0%}')} | {_num(f.roe5, '{:.0%}')} | {ttm} "
                     f"| {_num(f.pe_ttm)} | {_num(r.value.pe3)} | {high} | {Result.score(r.quality)} | {r.misses} |")
    return lines


# The screen's six lists, in report order (the backtest holds each one).
LISTS = {
    "defensive": "Graham defensive (≥ 6 of 7)",
    "qarp": "Quality at a reasonable price",
    "enterprising": "Graham enterprising (6 of 6)",
    "net_net": "Net-nets",
    "leaders": "Quality leaders (≥ 7 of 8, any price)",
    "fisher": "Fisher growth candidates",
}


def select_lists(results: list[Result], cfg: Config) -> dict[str, list[Result]]:
    """Which companies make each of the six lists, in each list's display order."""
    by_value = lambda r: -(r.value.vs_graham if r.value.vs_graham is not None else -9)
    return {
        "defensive": sorted([r for r in results if Result.score(r.defensive) >= 6],
                            key=lambda r: (-Result.score(r.defensive), by_value(r))),
        "qarp": sorted([r for r in results if Result.score(r.quality) >= 6
                        and ((r.value.pe3 is not None and r.value.pe3 <= cfg.max_pe3)
                             or (r.value.vs_graham or -1) >= 0)],
                       key=lambda r: (-Result.score(r.quality), r.value.pe3 or 99)),
        "enterprising": sorted([r for r in results if Result.score(r.enterprising) == 6], key=by_value),
        "net_net": sorted([r for r in results if r.net_net], key=by_value),
        "leaders": sorted([r for r in results if Result.score(r.quality) >= 7],
                          key=lambda r: (-Result.score(r.quality), r.value.pe3 or 99)),
        "fisher": sorted([r for r in results if is_candidate(r.quality, cfg)],
                         key=lambda r: (-Result.score(r.quality), r.fisher.pe_ttm or r.value.pe3 or 99)),
    }


def write_screen(results: list[Result], stats: dict, cfg: Config, out_dir: Path, top: int) -> Path:
    today = dt.date.today().isoformat()
    lists = select_lists(results, cfg)
    graham_list, qarp, enterprising_list = lists["defensive"], lists["qarp"], lists["enterprising"]
    net_nets, leaders, fisher_list = lists["net_net"], lists["leaders"], lists["fisher"]

    lines = [f"# Value screen — {today}", "",
             f"Prices of {stats.get('price_date')}. Data: VNDirect finfo (annual statements). {DISCLAIMER}", "",
             (f"- Universe: {stats['listed']} listed stocks → {stats['liquid']} that trade ≥ "
             f"{cfg.min_daily_value / 1e9:g} bn VND/day → **{len(results)} non-financial companies analysed** "
             f"(banks, insurers, brokers and lenders — by ICB sector, a few set by hand — need a different method "
             "and are left out)."),
             ("- **Price vs Graham #**: how far today's price is below (−) or above (+) Graham's maximum "
             "defensive price √(22.5 × EPS × book value)."), "- EPS uses today's share count, so stock dividends don't hide growth."]
    unclassified = sorted(r.company.code for r in results if r.company.sector is None)
    if unclassified:  # graded as industrials on their statements alone: a lender here needs FINANCIAL_OVERRIDES
        lines.append(f"- **No ICB sector** in VNDirect's list, so graded as industrials on their statements "
                     f"({len(unclassified)}): {', '.join(unclassified)}. Check none is a lender, broker or insurer; "
                     f"set by hand as financial: {', '.join(sorted(FINANCIAL_OVERRIDES))} (`data.FINANCIAL_OVERRIDES`).")
    if stats.get("vps_history"):  # only with --vps-fallback, and only when it was used
        vps = stats["vps_history"]
        lines.append(f"- **One-year high and change from VPS** (adjusted prices; VNDirect's history failed or was too "
                     f"short) for {len(vps)}: {', '.join(vps)}. Check with `stock-analyzer vps-check`.")
    lines += ["", f"## 1. Graham's defensive investor — passes ≥ 6 of 7 ({len(graham_list)})", ""]
    lines += _table(graham_list[:top]) if graham_list else ["None pass 6 of 7 today."]
    lines += ["", f"## 2. Quality at a reasonable price — quality ≥ 6 of 8 and cheap by Graham's measure ({len(qarp)})", "",
              "The closest thing to Fisher's outstanding company bought at a price Graham would accept.", ""]
    lines += _table(qarp[:top]) if qarp else ["None today."]
    lines += ["", f"## 3. Graham's enterprising investor — passes all 6 ({len(enterprising_list)})", ""]
    lines += _table(enterprising_list[:top]) if enterprising_list else ["None today."]
    lines += ["", f"## 4. Net-nets — price below ⅔ of net current assets ({len(net_nets)})", "",
              "Graham's deepest bargains. Often small or troubled: check why the market is this pessimistic.", ""]
    lines += _table(net_nets[:top]) if net_nets else ["None today."]
    lines += ["", f"## 5. Quality leaders at any price — quality ≥ 7 of 8 ({len(leaders)})", "",
              "A watchlist: Fisher would buy these at a fair price; Graham would wait for a cheaper one.", ""]
    lines += _table(leaders[:top]) if leaders else ["None today."]
    lines += ["", (f"## 6. Fisher growth candidates — sales growth, margin, ROE and no heavy dilution all pass, "
                   f"quality ≥ {cfg.fisher_min_quality} of 8 ({len(fisher_list)})"), "",
              ("Fisher's points 1, 5 and 13 in numbers. The last-four-quarters column shows whether profit is "
               f"still rising; ⬇ marks a price ≥ {cfg.fisher_fall_from_high:.0%} below its high of the year "
               "(adjusted for dividends) — a possible ch. 5 buying range *only if* the trouble behind it is "
               "temporary, which the numbers can't tell. Sorted by quality, then P/E on the last four quarters (the 3-year P/E "
               "where those quarters lost money)."), ""]
    lines += _fisher_table(fisher_list[:top], cfg) if fisher_list else ["None today."]
    lines += ["", "## Check keys", "",
              ("Graham: size, finance (current ratio ≥ 2, long-term debt ≤ working capital), stability (profit 10 years), "
              "dividends (10 years), growth (+⅓ in 10 years), pe (≤ 15 on 3-year earnings), pb (≤ 1.5 or P/E×P/B ≤ 22.5)."),
              ("Quality: q_growth, q_steady (sales), q_margin, q_margin_trend, q_roe, q_cash (cash conversion), "
              "q_dilution (shares sold for cash), q_debt."),
              ("Fisher list: Sales/yr and Profit/yr are 5-year compound growth (parent profit, which equals EPS "
               "growth on today's share count); last 4Q = the four newest quarterly reports VNDirect has."), "",
              "Deep dive on any ticker: `uv run stock-analyzer company <TICKER>`."]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"screen-{today}.md"
    path.write_text("\n".join(lines) + "\n")
    _write_csv(results, out_dir / f"screen-{today}.csv", cfg)
    return path


def _write_csv(results: list[Result], path: Path, cfg: Config) -> None:
    keys = [ch.key for ch in results[0].defensive + results[0].enterprising + results[0].quality] if results else []
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["code", "name", "exchange", "price", "market_cap_bn", "avg_value_20d_bn", "latest_year", "pe3", "pb",
                    "graham_number", "ncav_ps", "graham_score", "enterprising_score", "quality_score", "net_net",
                    "fisher_candidate", "sales_cagr5", "profit_cagr5", "op_margin5", "roe5", "ttm_profit_bn",
                    "ttm_growth", "ttm_to", "pe_ttm", "vs_high_1y", "change_1y", *keys])
        for r in sorted(results, key=lambda r: r.company.code):
            c, v = r.company, r.value
            w.writerow([c.code, c.name, c.exchange, round(c.price or 0), round((c.market_cap or 0) / 1e9),
                        round((c.avg_value_20d or 0) / 1e9, 1), c.latest_year, _num(v.pe3), _num(v.pb, "{:.2f}"),
                        _num(v.graham_number, "{:.0f}"), _num(v.ncav_ps, "{:.0f}"), Result.score(r.defensive),
                        Result.score(r.enterprising), Result.score(r.quality), r.net_net,
                        is_candidate(r.quality, cfg), *[_num(x, "{:.3f}") for x in (
                            r.fisher.sales_cagr5, r.fisher.profit_cagr5, r.fisher.op_margin5, r.fisher.roe5)],
                        _num(r.fisher.ttm_profit and r.fisher.ttm_profit / 1e9, "{:.0f}"),
                        _num(r.fisher.ttm_growth, "{:.3f}"), r.fisher.ttm_to or "", _num(r.fisher.pe_ttm),
                        _num(r.fisher.vs_high_1y, "{:.3f}"), _num(r.fisher.change_1y, "{:.3f}"),
                        *[{True: "pass", False: "fail", None: "?"}[ch.passed]
                          for ch in r.defensive + r.enterprising + r.quality]])


def _mark(passed: bool | None) -> str:
    return {True: "✅", False: "❌", None: "❔"}[passed]


def year_end_pe(c: Company, year: int) -> float | None:
    """Market value on the year's last trading day ÷ that year's parent profit.

    Shares = that year-end's paid-in capital ÷ par, so bonus issues don't distort the history
    (treasury shares and shares held by subsidiaries are still counted)."""
    close, capital, profit = c.year_end_close.get(year), c.get(year, "paid_in_capital"), c.get(year, "npat_parent")
    if not close or not capital or not profit or profit <= 0:
        return None
    return close * capital / c.face_value / profit


def write_company(r: Result, out_dir: Path) -> Path:
    c, v = r.company, r.value
    year = c.latest_year
    years = [y for y in range(year - 9, year + 1) if y in c.years]
    lines = [f"# {c.code} — {c.name} ({c.exchange})", "",
             (f"Price {_price(c.price)} VND ({c.price_date}) · market cap {bn(c.market_cap)} VND · "
             f"shares {c.shares / 1e6:,.1f} M (as of {c.shares_as_of}) · trades {bn(c.avg_value_20d)} VND/day"), "",
             (f"P/E on 3-year earnings **{_num(v.pe3)}** · P/E on the last four quarters **{_num(r.fisher.pe_ttm)}** · "
             f"P/B **{_num(v.pb, '{:.2f}')}** · Graham number **{_price(v.graham_number)}** · "
             f"net current assets/share {_price(v.ncav_ps)}"), "",
             (f"Price over the last year (adjusted for dividends): {_pct(r.fisher.change_1y)}; "
             f"{_pct(r.fisher.vs_high_1y)} vs the year's high of {_price(c.high_1y)}"
             + (" (from VPS's adjusted prices: VNDirect's one-year history failed or was too short)."
                if c.history_source == "vps" else ".")), ""]
    gap = c.share_count_gap
    if gap is not None and abs(gap) > 0.10:
        lines += [(f"⚠ Today's share count is {gap:+.0%} vs the last balance sheet — a bonus issue, rights issue or "
                   "buyback since then. Per-share numbers use today's count; check the company's recent events."), ""]
    lines += ["## Ten years in numbers (bn VND; EPS on today's share count)", "",
             ("| Year | Sales | Op. margin | Net profit (parent) | EPS | ROE | Current ratio | LT debt | Dividends paid "
              "| Op. cash flow | Shares issued for cash | Year-end P/E |"),
             "|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for y in years:
        sales, op, profit = c.get(y, "sales"), c.get(y, "operating_profit"), c.get(y, "npat_parent")
        equity, ca, cl = c.get(y, "parent_equity"), c.get(y, "current_assets"), c.get(y, "current_liabilities")
        cells = [
            _num(sales and sales / 1e9, "{:,.0f}"), _num(op / sales if sales and op is not None else None, "{:.1%}"),
            _num(profit and profit / 1e9, "{:,.0f}"), _num(profit / c.shares if profit is not None else None, "{:,.0f}"),
            _num(profit / equity if profit is not None and equity else None, "{:.1%}"),
            _num(ca / cl if ca and cl else None, "{:.2f}"), _bn(c.get(y, "lt_debt")),
            _bn(None if (d := c.get(y, "dividends_paid")) is None else -d), _bn(c.get(y, "cfo")),
            _bn(c.get(y, "share_issue")), _num(year_end_pe(c, y))]  # a missing figure is n/a, not 0 (2026-11-24)
        lines.append(f"| {y} | " + " | ".join(cells) + " |")
    quarters = sorted(d for d, q in c.quarters.items() if "sales" in q or "npat_parent" in q)[-8:]
    if quarters:
        lines += ["", "## Recent quarters (bn VND)", "",
                  "| Quarter end | Sales | Net profit (parent) | Profit vs same quarter a year before |", "|---|--:|--:|--:|"]
        for d in quarters:
            q = c.quarters[d]
            sales, profit = q.get("sales"), q.get("npat_parent", q.get("npat"))
            prior = c.quarters.get(f"{int(d[:4]) - 1}{d[4:]}", {})
            before = prior.get("npat_parent", prior.get("npat"))
            yoy = profit / before - 1 if profit is not None and before and before > 0 else None
            lines.append(f"| {d} | {_num(sales and sales / 1e9, '{:,.0f}')} | {_num(profit and profit / 1e9, '{:,.0f}')} "
                         f"| {_pct(yoy)} |")
    for title, checks in (("Graham — defensive investor", r.defensive), ("Graham — enterprising investor", r.enterprising),
                          ("Quality signals (measurable parts of Fisher's points)", r.quality)):
        lines += ["", f"## {title}: {Result.score(checks)} of {len(checks)}", ""]
        lines += [f"- {_mark(ch.passed)} **{ch.label}** — {ch.detail}" for ch in checks]
    if r.net_net:
        lines += ["", "**Net-net:** price is below two thirds of net current asset value."]
    lines += ["", "## Fisher worksheet — the points numbers can't answer", "",
              "Write your own answer first; then talk it through in `books/` with `/apply`.", ""]
    for number, question, where in FISHER_POINTS:
        lines += [f"**{number}. {question}**  ", f"_Where to look:_ {where}  ", "My answer:", ""]
    lines += ["## Data notes", "",
              "- Source: VNDirect finfo, annual consolidated statements; check key numbers against the audited report.",
              ("- Shares = VNDirect market cap ÷ that day's close, carried forward over bonus shares since (else, or "
               "when the cap implies over 30% fewer shares than the balance sheet, paid-in capital ÷ par value); both "
               "include shares a subsidiary holds, and preferred shares."),
              "- Tangible book removes goodwill only: in Vietnam most intangible assets are land-use rights.",
              "", DISCLAIMER]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{c.code}.md"
    path.write_text("\n".join(lines) + "\n")
    return path
