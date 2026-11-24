# vn-value-screener

An [MCP](https://modelcontextprotocol.io) server that screens every company listed in Vietnam (HOSE, HNX, UPCOM)
with Benjamin Graham's tests and the measurable parts of Philip Fisher's 15 points. Ask your AI assistant
"which Vietnamese companies pass Graham's defensive test?" or "walk me through FPT's numbers", and it calls these
tools instead of guessing.

<!-- mcp-name: io.github.Nghiauet/vn-value-screener -->

> **Not investment advice.** A screen finds companies worth reading about. It cannot tell you what to buy or sell,
> and the figures can be wrong. Check them against the audited annual report.

## Tools

| Tool | What it returns |
| --- | --- |
| `screen_stocks(list_name="all", limit=10)` | Six lists over every liquid non-financial company. **defensive**: Graham's defensive investor, ≥ 6 of 7 tests. **qarp**: quality at a reasonable price. **enterprising**: Graham's enterprising investor, 6 of 6. **net_net**: price below ⅔ of net current assets. **leaders**: quality ≥ 7 of 8, at any price. **fisher**: Fisher growth candidates. Each list gives its rule, how many companies pass, and the top `limit` with price, P/E, P/B, Graham number, scores and growth. |
| `analyze_company(ticker)` | One company's worksheet: valuation, every Graham and quality test with the numbers behind it, ten years of figures, the last eight quarters, warnings about the data, and Fisher's questions that numbers can't answer. Works for illiquid tickers too. |
| `explain_method()` | Every test and threshold, how each was adapted to Vietnam, and the method's known blind spots. Needs no network. |

All three are read-only. Prices are in VND, `*_bn` fields in billions of VND, and ratios are fractions (0.15 = 15%).
Banks, insurers, brokers and lenders are left out: Graham's tests don't fit their balance sheets.

## Install

You need [uv](https://docs.astral.sh/uv/). `uvx` downloads and runs the package; nothing else to set up.

**Claude Code**

```bash
claude mcp add vn-value-screener -- uvx vn-value-screener
```

**Claude Desktop, Cursor, and other clients** (`claude_desktop_config.json`, `.cursor/mcp.json`, …):

```json
{
  "mcpServers": {
    "vn-value-screener": {
      "command": "uvx",
      "args": ["vn-value-screener"]
    }
  }
}
```

If the client can't find `uvx`, put its full path in `command`. To find it, run `which uvx` on macOS/Linux or
`where uvx` on Windows.

### First call of the day: about a minute

The first call downloads what the screens need (about 55 requests to VNDirect, one per second). Later calls take
seconds. Every response is stored as it arrives, so if your client gives up waiting, just ask again: the next call
picks up where the last one stopped.

Options, as arguments or environment variables:

| Argument | Environment variable | Default |
| --- | --- | --- |
| `--data-dir PATH` | `VN_VALUE_SCREENER_DATA_DIR` | `$XDG_CACHE_HOME/vn-value-screener`, else `~/.cache/vn-value-screener` |
| `--offline` | `VN_VALUE_SCREENER_OFFLINE=1` | off: answer only from stored data, never call the API |

The store is a SQLite file. It holds a few tens of MB after a day's screen and grows as you look at more companies.

## Data source

Everything comes from VNDirect's finfo API (`api-finfo.vndirect.com.vn`): the public, undocumented API behind
VNDirect's website. This project is **not affiliated with VNDirect**, and the API may change or stop without notice.

The client stays polite on purpose:
- It names itself in its User-Agent (`vn-value-screener/<version> (+repository URL)`).
- It waits at least one second between requests and backs off on errors.
- It stores every response, so the same question isn't asked twice in a day.

If VNDirect objects, this project will slow down or stop. It will never disguise its traffic.

## Method, briefly

`explain_method` has the full list. In short:

- **Graham defensive** (*The Intelligent Investor*, ch. 14), adapted to Vietnam:
  - sales ≥ 1,000 bn VND;
  - current ratio ≥ 2;
  - profit in each of 10 years;
  - cash dividends in each of 10 years (Graham asked 20, but HOSE opened in 2000);
  - earnings up a third;
  - P/E ≤ 15 on 3-year earnings;
  - P/B ≤ 1.5, or P/E × P/B ≤ 22.5.
- **Graham enterprising** (ch. 15) and **net-nets** (price below ⅔ of net current assets).
- **Eight quality signals** for the parts of Fisher's points that numbers can show:
  - sales growth and steadiness;
  - operating margin above the median, and margins holding;
  - ROE ≥ 15%;
  - cash conversion;
  - little dilution;
  - conservative debt.
- EPS for every year uses **today's share count**. Vietnamese companies pay many stock dividends, and reported EPS
  hides that growth.

Known blind spots:
- Shares held by subsidiaries and preferred shares count as ordinary shares.
- Profit is struck before the bonus-and-welfare fund and staff share schemes.
- One-off gains stay inside profit.
- Cyclical companies pass the growth tests at the top of their cycle.

Read the company's filings before trusting any result.

## Development

```bash
uv sync
uv run pytest -q          # synthetic data, no network
uv run ruff check .
uv run vn-value-screener --help
```

`src/vn_value_screener/engine/` is the screening engine. It is copied unchanged from the author's research tool, and
`scripts/sync-engine.sh` refreshes it. The MCP layer is `service.py` (tool logic, JSON shapes) and `server.py` (the MCP
server).

Releases: push a tag `vX.Y.Z` that matches the version in `pyproject.toml`, `src/vn_value_screener/__init__.py` and
`server.json`. The release workflow then tests the code, publishes it to PyPI (trusted publishing), and publishes
`server.json` to the [MCP Registry](https://registry.modelcontextprotocol.io).

## License

MIT. See [LICENSE](LICENSE).
