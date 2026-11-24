"""The MCP server: three read-only tools over stdio.

All engine work runs on one worker thread (the SQLite store's connection belongs to the thread that opened it), so
calls are served one at a time. Progress messages go to the client while a cold first run downloads (~1 minute).
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import REPOSITORY, __version__
from .engine import SOURCE_REVISION
from .service import MAX_LIMIT, Screener, ScreenerError, default_data_dir, methodology

INSTRUCTIONS = """\
Screens companies listed in Vietnam (HOSE, HNX, UPCOM) with Benjamin Graham's tests and the measurable parts of
Philip Fisher's 15 points, from VNDirect's public data.

- `screen_stocks`: the six lists (Graham defensive, quality at a reasonable price, Graham enterprising, net-nets,
  quality leaders, Fisher growth candidates).
- `analyze_company`: one ticker's worksheet: every test with its numbers, ten years of figures, recent quarters.
- `explain_method`: what each test checks and its thresholds (no network).

The first call of the day downloads data (about a minute); later calls are fast. Prices are in VND; *_bn fields are
billions of VND; ratios are fractions (0.15 = 15%). A screen finds companies worth reading about. It is not
investment advice: never present a result as a recommendation to buy or sell, and say where the numbers can be wrong
(see each result's warnings, data_notes and disclaimer)."""

log_ = logging.getLogger(__name__)
READ_ONLY = {"read_only_hint": True, "destructive_hint": False, "idempotent_hint": True}

server = MCPServer("vn-value-screener", title="Vietnam value screener", instructions=INSTRUCTIONS,
                   version=__version__, website_url=REPOSITORY)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screener")
_screener: Screener | None = None
_open_options: dict[str, Any] = {}


def _get_screener() -> Screener:
    """Opened on the worker thread, on first use."""
    global _screener
    if _screener is None:
        _screener = Screener.open(**_open_options)
    return _screener


async def _run(ctx: Context | None, method: str, *args: Any) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    steps = itertools.count(1)

    def log(message: str) -> None:  # called on the worker thread
        if ctx is None:
            return
        sent = asyncio.run_coroutine_threadsafe(ctx.report_progress(next(steps), None, message[:300]), loop)
        try:
            sent.result(timeout=5)  # in order, before the result
        except Exception:  # a lost progress message (client gone, timeout) never fails the call
            log_.debug("progress message not sent", exc_info=True)

    def job() -> dict[str, Any]:
        return getattr(_get_screener(), method)(*args, log=log)

    try:
        return await loop.run_in_executor(_executor, job)
    except ScreenerError as e:
        raise ToolError(str(e)) from e


ListName = Literal["all", "defensive", "qarp", "enterprising", "net_net", "leaders", "fisher"]


@server.tool(
    title="Screen Vietnamese stocks",
    annotations=ToolAnnotations(title="Screen Vietnamese stocks", open_world_hint=True, **READ_ONLY),
)
async def screen_stocks(
    ctx: Context,
    list_name: Annotated[ListName, Field(description=(
        "Which list: 'all' (every list), 'defensive' (Graham defensive, ≥ 6 of 7 tests), 'qarp' (quality at a "
        "reasonable price), 'enterprising' (Graham enterprising, 6 of 6), 'net_net' (price < ⅔ of net current "
        "assets), 'leaders' (quality ≥ 7 of 8, any price), 'fisher' (Fisher growth candidates)."))] = "all",
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT, description="Companies shown per list, best first.")] = 10,
) -> dict[str, Any]:
    """Run Graham's and Fisher's screens over every liquid non-financial company listed in Vietnam.

    Returns each list's rule, how many companies pass, and the top `limit` with price, P/E, P/B, Graham number,
    scores and growth figures. A list is a reading list of companies worth studying, not a recommendation."""
    return await _run(ctx, "screen", list_name, limit)


@server.tool(
    title="Analyze one Vietnamese company",
    annotations=ToolAnnotations(title="Analyze one Vietnamese company", open_world_hint=True, **READ_ONLY),
)
async def analyze_company(
    ctx: Context,
    ticker: Annotated[str, Field(min_length=1, max_length=12,
                                 description="Ticker on HOSE, HNX or UPCOM, e.g. FPT, VNM, HPG.")],
) -> dict[str, Any]:
    """One company's worksheet: valuation (P/E, P/B, Graham number, net current assets), every Graham and quality
    test with the numbers behind it, ten years of figures, the last eight quarters, warnings about the data, and
    Fisher's questions that numbers can't answer. Works for illiquid tickers too; banks, insurers, brokers and
    lenders are not covered."""
    return await _run(ctx, "company", ticker)


@server.tool(
    title="Explain the screening method",
    annotations=ToolAnnotations(title="Explain the screening method", open_world_hint=False, **READ_ONLY),
)
def explain_method() -> dict[str, Any]:
    """Every test the screens apply, with its threshold and the reason for each adaptation to Vietnam, plus the
    method's known blind spots. No network."""
    return methodology()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vn-value-screener",
                                     description="MCP server (stdio): Graham/Fisher screens of Vietnamese stocks.")
    parser.add_argument("--data-dir", type=Path,
                        help=f"cache folder for the downloaded data (default: {default_data_dir()})")
    parser.add_argument("--offline", action="store_true", help="answer from stored data only; never call the API")
    parser.add_argument("--version", action="version",
                        version=f"vn-value-screener {__version__} (engine {SOURCE_REVISION[:7]})")
    args = parser.parse_args(argv)
    if args.data_dir:
        _open_options["data_dir"] = args.data_dir
    if args.offline:
        _open_options["offline"] = True
    try:
        server.run("stdio")
    except KeyboardInterrupt:
        pass
    finally:
        _executor.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
