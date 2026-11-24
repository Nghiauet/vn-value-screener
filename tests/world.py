"""A tiny synthetic VNDirect for tests: no network. Adapted from the engine's own test world."""
from __future__ import annotations

import datetime as dt
import urllib.parse

from vn_value_screener.engine.data import ITEMS
from vn_value_screener.engine.store import Store
from vn_value_screener.engine.vndirect import Client

DAY = dt.date(2026, 11, 4)  # a Wednesday: the newest trading day in this world
BN = 1e9


def weekdays(a: dt.date, b: dt.date) -> list[dt.date]:
    return [a + dt.timedelta(days=n) for n in range((b - a).days + 1) if (a + dt.timedelta(days=n)).weekday() < 5]


class World(Client):
    """`codes` listed on HOSE, each trading `value[code]` VND a day (2 bn by default) at 20,000 VND, twelve years of
    plain annual statements, two quarters, and a 1,000 bn VND market cap. Counts the requests it answers."""

    def __init__(self, store: Store, codes: list[str], offline: bool = False, value: dict[str, float] | None = None):
        super().__init__(store, offline)
        self.codes = list(codes)
        self.value = value or {}
        self.urls: list[str] = []

    def row(self, code: str, day: dt.date) -> dict:
        return {"code": code, "date": day.isoformat(), "close": 20.0, "adClose": 20.0,
                "nmValue": self.value.get(code, 2e9), "nmVolume": 1e5, "floor": "HOSE", "type": "STOCK"}

    def stock(self, code: str) -> dict:
        return {"code": code, "floor": "HOSE", "type": "STOCK", "shortName": f"{code} JSC", "faceValue": 10_000}

    def answer(self, path: str, params: dict) -> list[dict]:
        q = params.get("q", "")
        codes = q.split("~")[0].removeprefix("code:").split(",") if q.startswith("code:") else self.codes
        if path == "stocks":
            if "status:LISTED" in q:
                return [self.stock(c) for c in self.codes]
            return [self.stock(c) for c in codes if c in self.codes]
        if path == "stock_prices" and params.get("sort") == "date:desc":
            return [self.row(codes[0], DAY)]
        if path == "stock_prices":
            start = dt.date.fromisoformat(q.split("date:gte:")[1][:10])
            end = dt.date.fromisoformat(q.split("date:lte:")[1][:10]) if "date:lte:" in q else DAY
            return [self.row(c, d) for d in weekdays(start, min(end, DAY)) for c in codes]
        if path == "financial_statements":
            if "QUARTER" in q:
                return [{"code": c, "itemCode": float(ITEMS[item]), "fiscalDate": f"2026-{md}", "numericValue": v}
                        for c in codes for md in ("03-31", "06-30")
                        for item, v in (("sales", 300 * BN), ("npat_parent", 30 * BN))]
            return [{"code": c, "itemCode": float(ITEMS[item]), "fiscalDate": f"{y}-12-31", "numericValue": v}
                    for c in codes for y in range(2014, 2026) for item, v in (
                        ("sales", 1_200 * BN), ("npat", 120 * BN), ("npat_parent", 120 * BN),
                        ("current_assets", 900 * BN), ("current_liabilities", 300 * BN), ("liabilities", 300 * BN),
                        ("equity", 1_000 * BN), ("paid_in_capital", 500 * BN), ("dividends_paid", -50 * BN))]
        if path == "ratios/latest":
            codes = params["where"].removeprefix("code:").split(",")
            return [{"code": c, "ratioCode": "MARKETCAP", "value": 1_000 * BN,
                     "reportDate": (DAY - dt.timedelta(days=1)).isoformat()} for c in codes]
        return []

    def _fetch(self, url: str) -> dict:
        self.urls.append(url)
        self.requests_made += 1
        path = url.split("/v4/")[1].split("?")[0]
        data = self.answer(path, dict(urllib.parse.parse_qsl(url.split("?", 1)[1])))
        return {"data": data, "totalElements": len(data)}
