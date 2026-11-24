"""VPS's TradingView history feed (histdatafeed.vps.com.vn): the backtest's VN-Index, and a backup for stocks'
adjusted prices.

VNDirect's `vnmarket_prices` VN-Index starts 2017-08-24; VPS serves it from 2000-07-28, free and without a login
(notes/2026-11-01-data-sources.md). The 2026-11-01 cross-check found VPS and SSI equal for 2014–2017 and VPS,
SSI and VNDirect within 0.1 pp a year for 2018–2025 (notes/2026-11-01-backtest-crosscheck.md).

For stocks the feed has **adjusted prices only** (dividends and bonus shares, scaled so the newest bar is the
last raw close), in thousands of VND like VNDirect. So it can stand in for VNDirect's `adClose` (the one-year
high and change: `--vps-fallback`, data.py) but never for the raw `close` history. `FIELD_MAP` lists every
field; `compare` / `stock-analyzer vps-check` measure the agreement with VNDirect before VPS data is trusted.

Same politeness as the VNDirect client (shared code): honest User-Agent, ≥ 1 s between requests, retries with
backoff, every response logged append-only in the store under source 'vps'. VPS stays raw-only in the store:
its bars are index points or adjusted-only stock prices, which don't fit `daily_prices`' raw `close` column.

The feed takes `from`/`to` as Unix seconds. The stored request leaves `to` out — it is the fetch time, added
to the URL only — so the request means "the whole daily history as of the fetch", like VNDirect's
`vnmarket_prices?q=code:VNINDEX`, and a stored response is found again on any later day (`--offline`). Stocks
use the same request shape (one GET per ticker, the whole history since 2000; ~5,000 bars for FPT).
"""
from __future__ import annotations

import datetime as dt
import itertools
import time
from dataclasses import dataclass, field

from .vndirect import Client, url_of

SOURCE = "vps"
BASE_URL = "https://histdatafeed.vps.com.vn/tradingview"
FROM = 946684800  # 2000-01-01 00:00 UTC, before the feed's first VN-Index bar (2000-07-28)
VNINDEX = "VNINDEX"


class VpsError(Exception):
    """The feed answered, but not with a usable daily series."""


class VpsClient(Client):
    source = SOURCE

    def url(self, path: str, params: dict) -> str:
        return url_of(path, {**params, "to": int(time.time())}, BASE_URL)


def history_params(symbol: str) -> dict:
    return {"symbol": symbol, "resolution": "D", "from": FROM}


def parse_history(body) -> dict[str, float]:
    """A TradingView-style response ({"s": "ok", "t": [...], "c": [...]}) → {date: close}.

    VPS stamps each daily bar at 00:00 UTC of its trading day, so the UTC date is the trading date.
    Bars without a positive close are dropped."""
    if not isinstance(body, dict) or body.get("s") != "ok":
        raise VpsError(f"VPS history: status {body.get('s') if isinstance(body, dict) else type(body).__name__!r}")
    stamps, closes = body.get("t") or [], body.get("c") or []
    if len(stamps) != len(closes):
        raise VpsError(f"VPS history: {len(stamps)} timestamps but {len(closes)} closes")
    return {dt.datetime.fromtimestamp(int(t), dt.UTC).date().isoformat(): float(c)
            for t, c in zip(stamps, closes) if c is not None and float(c) > 0}


def vnindex_levels(client: Client, max_age_hours: float) -> dict[str, float]:
    """VN-Index daily closes (price index, no dividends) since 2000-07-28: one request."""
    return parse_history(client.get("history", history_params(VNINDEX), max_age_hours))


# ------------------------------------------------------------------ stocks: adjusted prices only

# VNDirect `stock_prices` field → VPS history field (None: VPS has nothing that may stand in for it). Prices in both
# are thousands of VND (VPS c 52.7 = VNDirect adClose 52.7 = 52,700 VND); `t` is 00:00 UTC of the trading day.
FIELD_MAP: dict[str, str | None] = {
    "date": "t",
    "adClose": "c", "adOpen": "o", "adHigh": "h", "adLow": "l",
    # Raw prices: VPS has none. Its newest bar equals that day's raw close, every older bar is adjusted.
    "close": None, "open": None, "high": None, "low": None, "average": None, "basicPrice": None,
    # Volume `v` is shares, but whether it is matched only (VNDirect nmVolume) or includes put-through deals
    # (ptVolume), and whether it is rescaled for stock dividends, is what `compare` measures; not mapped until then.
    "nmVolume": None, "ptVolume": None,
    # Traded value: VPS has none. close × volume from VPS would be on adjusted prices (too low before an ex-date).
    "nmValue": None, "ptValue": None,
}
# What the stock bars may be used for (CLAUDE.md "Second source"): adClose-based figures only.
SAFE_USES = ("one-year high", "one-year change", "adjusted returns within one response (after a vps-check)")
UNSAFE_USES = ("raw close history (year-end P/E, close × shares)", "the share count from market cap ÷ close",
               "bonus-share factors from adClose/close", "liquidity (traded value)", "the screen's universe")


def stock_bars(client: Client, ticker: str, max_age_hours: float) -> list[dict]:
    """One ticker's whole daily history from VPS, as VNDirect-shaped rows holding only the adjusted fields
    (`code`, `date`, `adOpen`, `adHigh`, `adLow`, `adClose`, in thousands of VND; `vpsVolume` kept under its own
    name). Oldest first. Never a `close` key: VPS has no raw prices. One request."""
    body = client.get("history", history_params(ticker), max_age_hours)
    if not isinstance(body, dict) or body.get("s") != "ok":
        raise VpsError(f"VPS {ticker}: status {body.get('s') if isinstance(body, dict) else type(body).__name__!r}")
    stamps = body.get("t") or []
    series = {k: body.get(k) or [] for k in ("o", "h", "l", "c", "v")}
    if any(len(v) != len(stamps) for k, v in series.items() if k != "v" or v):
        raise VpsError(f"VPS {ticker}: series lengths differ from {len(stamps)} timestamps")
    rows = []
    for i, t in enumerate(stamps):
        c = series["c"][i]
        if c is None or float(c) <= 0:
            continue
        row = {"code": ticker, "date": dt.datetime.fromtimestamp(int(t), dt.UTC).date().isoformat(),
               "adClose": float(c)}
        for vnd_key, key in (("adOpen", "o"), ("adHigh", "h"), ("adLow", "l")):
            if series[key][i] is not None:
                row[vnd_key] = float(series[key][i])
        if series["v"]:
            row["vpsVolume"] = series["v"][i]
        rows.append(row)
    return sorted(rows, key=lambda r: r["date"])


@dataclass
class Comparison:
    """VNDirect vs VPS adjusted closes for one ticker over one window (both fetched about the same time)."""

    ticker: str
    start: str
    end: str  # VNDirect's newest day in the window
    common: int = 0
    only_vndirect: list[str] = field(default_factory=list)  # trading days VPS lacks
    only_vps: list[str] = field(default_factory=list)  # VPS bars VNDirect lacks (often zero-volume days)
    max_level_diff: float = 0.0  # max |VPS / VNDirect adClose − 1| on common days
    level_mismatches: list[tuple[str, float, float]] = field(default_factory=list)  # (date, VNDirect, VPS) > tol
    max_return_diff: float = 0.0  # max |daily return difference|, as a fraction
    return_mismatches: list[tuple[str, float, float]] = field(default_factory=list)  # (date, VNDirect, VPS) > tol
    latest_close: float | None = None  # VNDirect raw close on `end`, thousands of VND
    latest_vps: float | None = None  # VPS close on `end`
    high: tuple[float | None, float | None] = (None, None)  # one-year high (VNDirect, VPS), thousands of VND
    change: tuple[float | None, float | None] = (None, None)  # change over the window (VNDirect, VPS)
    volume_matched: int = 0  # common days where VPS volume == nmVolume
    volume_with_pt: int = 0  # ... == nmVolume + ptVolume
    volume_days: int = 0  # common days with both volumes

    @property
    def ok(self) -> bool:
        return bool(self.common) and not self.level_mismatches and not self.return_mismatches

    @property
    def latest_equal(self) -> bool | None:
        if self.latest_close is None or self.latest_vps is None:
            return None
        return abs(self.latest_vps - self.latest_close) < 1e-6


def compare(ticker: str, vndirect_rows: list[dict], vps_rows: list[dict], start: str,
            level_tol: float = 0.01, return_tol: float = 0.005) -> Comparison:
    """Compare VNDirect `stock_prices` rows (raw `close`, `adClose`, `nmVolume`, `ptVolume`) with `stock_bars`
    rows for one ticker, from `start` to VNDirect's newest day. Levels: both series are scaled to their own
    newest adjustment, so fetch both the same day. A return mismatch marks an ex-date one source adjusts and the
    other doesn't (or adjusts differently: VNDirect follows the exchange's tick-rounded reference price)."""
    vnd = {r["date"]: r for r in vndirect_rows
           if r.get("code", ticker) == ticker and r.get("adClose") and r["date"] >= start}
    if not vnd:
        return Comparison(ticker, start, start)
    end = max(vnd)
    vps = {r["date"]: r for r in vps_rows if start <= r["date"] <= end}
    out = Comparison(ticker, start, end)
    common = sorted(set(vnd) & set(vps))
    out.common = len(common)
    out.only_vndirect = sorted(set(vnd) - set(vps))
    out.only_vps = sorted(set(vps) - set(vnd))
    for d in common:
        a, b = vnd[d]["adClose"], vps[d]["adClose"]
        diff = abs(b / a - 1)
        out.max_level_diff = max(out.max_level_diff, diff)
        if diff > level_tol:
            out.level_mismatches.append((d, a, b))
        volume = vps[d].get("vpsVolume")
        if volume is not None and vnd[d].get("nmVolume") is not None:
            out.volume_days += 1
            out.volume_matched += volume == vnd[d]["nmVolume"]
            out.volume_with_pt += volume == vnd[d]["nmVolume"] + (vnd[d].get("ptVolume") or 0)
    for prev, d in itertools.pairwise(common):
        ra = vnd[d]["adClose"] / vnd[prev]["adClose"] - 1
        rb = vps[d]["adClose"] / vps[prev]["adClose"] - 1
        out.max_return_diff = max(out.max_return_diff, abs(ra - rb))
        if abs(ra - rb) > return_tol:
            out.return_mismatches.append((d, ra, rb))
    out.latest_close = vnd[end].get("close")
    out.latest_vps = vps[end]["adClose"] if end in vps else None
    out.high = (max(r["adClose"] for r in vnd.values()), max((r["adClose"] for r in vps.values()), default=None))
    first_vnd, first_vps = vnd[min(vnd)]["adClose"], (vps[min(vps)]["adClose"] if vps else None)
    out.change = (vnd[end]["adClose"] / first_vnd - 1,
                  vps[max(vps)]["adClose"] / first_vps - 1 if vps and first_vps else None)
    return out


def format_comparison(c: Comparison, level_tol: float, return_tol: float, show: int = 5) -> list[str]:
    def pct(x):
        return "n/a" if x is None else f"{x * 100:+.2f}%"

    def price(x):
        return "n/a" if x is None else f"{x * 1000:,.0f}"

    if not c.common:
        return [(f"{c.ticker}: no common days from {c.start} (VNDirect {len(c.only_vndirect)} days, "
                f"VPS {len(c.only_vps)} bars) — MISMATCH")]
    latest = {True: "=", False: "≠", None: "?"}[c.latest_equal]
    lines = [f"{c.ticker}: {c.start} → {c.end}, {c.common} common days — {'OK' if c.ok else 'MISMATCH'}",
             (f"  adClose level: max diff {c.max_level_diff * 100:.2f}% ({len(c.level_mismatches)} days > "
             f"{level_tol * 100:g}%); daily returns: max diff {c.max_return_diff * 100:.2f} pp "
             f"({len(c.return_mismatches)} days > {return_tol * 100:g} pp)"),
             (f"  newest day: VNDirect close {price(c.latest_close)} {latest} VPS {price(c.latest_vps)}; "
             f"1-year high {price(c.high[0])} vs {price(c.high[1])}; change {pct(c.change[0])} vs {pct(c.change[1])}"),
             (f"  missing: {len(c.only_vndirect)} VNDirect days not in VPS, {len(c.only_vps)} VPS bars not in "
             f"VNDirect; volume = nmVolume on {c.volume_matched}/{c.volume_days} days, "
             f"= nmVolume + ptVolume on {c.volume_with_pt}/{c.volume_days}")]
    for label, days in (("VNDirect-only days", c.only_vndirect), ("VPS-only bars", c.only_vps)):
        if days:
            lines.append(f"  {label}: {', '.join(days[:show])}{' …' if len(days) > show else ''}")
    for d, a, b in c.level_mismatches[:show]:
        lines.append(f"  level {d}: VNDirect {a:g}, VPS {b:g} ({(b / a - 1) * 100:+.2f}%)")
    for d, a, b in c.return_mismatches[:show]:
        lines.append(f"  return {d}: VNDirect {a * 100:+.2f}%, VPS {b * 100:+.2f}% (an ex-date one source misses?)")
    return lines
