"""The tools' logic on the synthetic VNDirect (tests/world.py): shapes, input checks, caching, errors."""
import dataclasses
import json

import pytest

from vn_value_screener.engine import vndirect
from vn_value_screener.engine.config import Config
from vn_value_screener.engine.report import DISCLAIMER, LISTS
from vn_value_screener.engine.store import Store
from vn_value_screener.service import UNIVERSE_TTL, USER_AGENT, Screener, ScreenerError, methodology

from .world import World

CODES = ["AAA", "BBB", "CCC", "F88", "ILQ"]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "stock.db")
    yield World(store, CODES, value={"ILQ": 0.1e9})  # ILQ trades 0.1 bn a day: outside the universe
    store.close()


@pytest.fixture
def screener(world):
    return Screener(world, clock=Clock())


def test_screen_returns_every_list_with_its_rule_and_the_disclaimer(screener):
    out = screener.screen()
    assert set(out["lists"]) == set(LISTS)
    assert out["universe"]["liquid"] == 4  # ILQ is too illiquid
    assert out["universe"]["analysed"] == 3  # F88 is a lender: left out
    assert out["disclaimer"] == DISCLAIMER and "not financial advice" in out["disclaimer"]
    assert out["price_date"] == "2026-11-04"
    for key, block in out["lists"].items():
        assert block["title"] == LISTS[key] and block["rule"]
        assert block["shown"] == len(block["companies"]) <= block["count"]
    row = out["lists"]["defensive"]["companies"][0]
    assert row["ticker"] in {"AAA", "BBB", "CCC"} and row["price_vnd"] == 20_000
    assert row["pe_3y"] == pytest.approx(8.3, abs=0.1)  # 1,000 bn cap ÷ 120 bn profit
    json.dumps(out)  # plain JSON, nothing the MCP layer can't send


def test_screen_one_list_and_limit(screener):
    out = screener.screen("defensive", limit=1)
    assert list(out["lists"]) == ["defensive"]
    assert out["lists"]["defensive"]["shown"] == 1 and out["lists"]["defensive"]["count"] == 3


@pytest.mark.parametrize("args", [("nope", 10), ("all", 0), ("all", 51), ("all", True), ("all", 2.5)])
def test_screen_rejects_bad_input(screener, args):
    with pytest.raises(ScreenerError):
        screener.screen(*args)


def test_universe_is_reused_then_rebuilt_from_the_store(screener, world):
    screener.screen()
    made, lookups = world.requests_made, world.lookups
    screener.screen("qarp")
    assert (world.requests_made, world.lookups) == (made, lookups)  # same analysed market, nothing asked
    screener.clock.now += UNIVERSE_TTL + 1
    screener.screen()
    assert world.lookups > lookups and world.requests_made == made  # rebuilt, but from young stored responses


def test_company_worksheet(screener):
    out = screener.company(" aaa ")
    assert out["ticker"] == "AAA" and out["name"] == "AAA JSC"
    assert [len(out["checks"][k]) for k in ("graham_defensive", "graham_enterprising", "quality")] == [7, 6, 8]
    assert out["scores"]["graham_defensive"]["of"] == 7
    assert [y["year"] for y in out["ten_years"]] == list(range(2016, 2026))
    assert out["ten_years"][-1]["sales_bn"] == 1_200.0
    assert [q["quarter_end"] for q in out["recent_quarters"]] == ["2026-03-31", "2026-06-30"]
    assert len(out["fisher_questions_numbers_cannot_answer"]) == 15
    assert "defensive" in out["on_lists"]
    assert out["disclaimer"] == DISCLAIMER
    json.dumps(out)


def test_company_outside_the_universe_is_fetched_on_its_own(screener):
    out = screener.company("ILQ")
    assert out["ticker"] == "ILQ"
    assert any("outside the screen's universe" in w for w in out["warnings"])


@pytest.mark.parametrize("ticker, message", [
    ("F88", "bank, insurer, broker or lender"),
    ("ZZZ", "not found"),
    ("F!8", "letters or digits"),
    ("", "letters or digits"),
    (None, "letters or digits"),
    ("A" * 11, "letters or digits"),
])
def test_company_errors(screener, ticker, message):
    with pytest.raises(ScreenerError, match=message):
        screener.company(ticker)


def test_offline_without_stored_data_says_so(tmp_path):
    store = Store(tmp_path / "empty.db")
    with pytest.raises(ScreenerError, match="offline"):
        Screener(World(store, CODES, offline=True)).screen()
    store.close()


class Down(World):
    def _fetch(self, url):
        raise OSError("connection refused")


class Empty(World):
    def answer(self, path, params):
        return []  # VNDirect answering with no rows at all


@pytest.mark.parametrize("world_class", [Down, Empty])
def test_a_failing_data_source_becomes_a_readable_error(tmp_path, world_class):
    store = Store(tmp_path / "s.db")
    with pytest.raises(ScreenerError, match="didn't answer"):
        Screener(world_class(store, CODES)).screen()
    store.close()


def test_requests_carry_this_package_user_agent(tmp_path, monkeypatch):
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"data": [], "totalElements": 0}'

    def urlopen(request, timeout):
        seen.append(request.get_header("User-agent"))
        return Response()

    monkeypatch.setattr(vndirect.urllib.request, "urlopen", urlopen)
    store = Store(tmp_path / "s.db")
    vndirect.Client(store).get("stocks", {"q": "code:FPT"}, 1)
    store.close()
    assert seen == [USER_AGENT] and USER_AGENT.startswith("vn-value-screener/")


def test_methodology_reads_the_engine_config():
    out = methodology()
    assert out["thresholds"] == dataclasses.asdict(Config())
    assert set(out["lists"]) == set(LISTS)
    assert len(out["graham_defensive_tests"]) == 7 and len(out["graham_enterprising_tests"]) == 6
    assert len(out["quality_signals"]) == 8
    json.dumps(out)
