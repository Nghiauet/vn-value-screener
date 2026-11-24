"""The MCP layer: tool list and annotations, calls in-process, errors, progress, and a real stdio run."""
import sys

import pytest
from mcp import Client, StdioServerParameters

from vn_value_screener import server as server_module
from vn_value_screener.engine.store import Store
from vn_value_screener.service import Screener

from .world import World

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_market(tmp_path, monkeypatch):
    """The server's screener, on the synthetic VNDirect, opened lazily on the worker thread like the real one."""
    made = {}

    def get_screener():
        if "s" not in made:
            made["s"] = Screener(World(Store(tmp_path / "stock.db"), ["AAA", "BBB", "F88"]))
        return made["s"]

    monkeypatch.setattr(server_module, "_get_screener", get_screener)
    return made


async def test_tools_are_listed_read_only_with_titles():
    async with Client(server_module.server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"screen_stocks", "analyze_company", "explain_method"}
    for tool in tools.values():
        assert tool.title and tool.description
        assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    assert tools["explain_method"].annotations.open_world_hint is False
    assert tools["screen_stocks"].annotations.open_world_hint is True


async def test_screen_and_company_calls(fake_market):
    progress = []

    async def on_progress(value, total, message):
        progress.append(message)

    async with Client(server_module.server) as client:
        screen = await client.call_tool("screen_stocks", {"list_name": "defensive", "limit": 1},
                                        progress_callback=on_progress)
        company = await client.call_tool("analyze_company", {"ticker": "bbb"})
        lender = await client.call_tool("analyze_company", {"ticker": "F88"})
    assert not screen.is_error
    assert list(screen.structured_content["lists"]) == ["defensive"]
    assert any("listed stocks" in m for m in progress)  # the cold load reported what it was doing
    assert not company.is_error and company.structured_content["ticker"] == "BBB"
    assert lender.is_error and "lender" in lender.content[0].text


@pytest.mark.parametrize("name, arguments", [
    ("screen_stocks", {"list_name": "best"}),
    ("screen_stocks", {"limit": 500}),
    ("screen_stocks", {"limit": "ten"}),
    ("analyze_company", {}),
    ("analyze_company", {"ticker": ""}),
])
async def test_bad_arguments_are_tool_errors(fake_market, name, arguments):
    async with Client(server_module.server) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error


async def test_stdio_entry_point(tmp_path):
    """The installed command speaks MCP on stdout (nothing else may print there) and answers offline."""
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "vn_value_screener.server", "--offline", "--data-dir", str(tmp_path)])
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        method = await client.call_tool("explain_method", {})
        offline = await client.call_tool("screen_stocks", {})
    assert names == {"screen_stocks", "analyze_company", "explain_method"}
    assert not method.is_error and "graham_defensive_tests" in method.structured_content
    assert offline.is_error and "offline" in offline.content[0].text
