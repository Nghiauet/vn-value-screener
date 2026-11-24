"""The screening engine, copied unchanged from the author's research tool (stock-analyzer).

Copied files: config, store, vndirect, vps, data, graham, quality, fisher, report.
Source revision: 1a8a9a82d9264f357ed9614f65fd25aaadd54782

Do not edit these files here: change them upstream, then re-copy with scripts/sync-engine.sh, so the two copies
stay identical. The MCP layer (../service.py, ../server.py) adapts them: its own User-Agent, its own cache folder,
JSON output instead of report files.
"""

SOURCE_REVISION = "1a8a9a82d9264f357ed9614f65fd25aaadd54782"
