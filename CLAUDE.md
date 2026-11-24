# vn-value-screener

A public MCP server (stdio, PyPI package) that runs Graham/Fisher screens over Vietnamese listed companies. Users
install it with `uvx vn-value-screener`. Machine-specific notes, if any, are in `CLAUDE.local.md` (untracked).

## Commands

```bash
uv sync
uv run pytest -q                      # synthetic VNDirect (tests/world.py), no network; includes a real stdio run
uv run ruff check .
uv run python scripts/check_versions.py [vX.Y.Z]   # pyproject, __init__, server.json (+ tag) agree; README has mcp-name
uv build                              # wheel + sdist in dist/
uv run vn-value-screener --data-dir /tmp/x [--offline]   # the server on stdio (an MCP client starts it)
bash scripts/sync-engine.sh <stock-analyzer checkout> [rev]   # re-copy the engine, committed files only
```

## Architecture

```
src/vn_value_screener/
  engine/      the screening engine, copied UNCHANGED from the author's stock-analyzer (revision in engine/__init__.py)
  service.py   tool logic: Screener (store + client, analysed market reused for an hour), JSON shapes, input checks,
               methodology() read from engine Config; sets our own User-Agent on the engine's client
  server.py    MCPServer (mcp SDK 2.x): screen_stocks, analyze_company, explain_method; all engine work on ONE worker
               thread (sqlite connection is thread-bound); engine log lines → MCP progress notifications
tests/         world.py (synthetic VNDirect), test_service.py, test_server.py (in-process + stdio)
server.json    MCP Registry metadata (schema 2025-12-11); name io.github.Nghiauet/vn-value-screener
.github/workflows/ci.yml, release.yml   tag vX.Y.Z → tests → PyPI (trusted publishing) → MCP Registry (GitHub OIDC)
```

## Rules

- **Never edit `engine/` here.** Change stock-analyzer, commit there, then `sync-engine.sh` and re-test. After a sync,
  run one live screen and compare its universe line and list counts with stock-analyzer's report of the same day
  (2026-11-24: 1519 → 277 → 222; lists 34/30/26/6/18/19, identical).
- **Nothing may print to stdout**: it is the MCP channel. The engine logs through a callback; keep it that way.
- **Politeness is not negotiable:** honest User-Agent (`service.USER_AGENT`), ≥ 1 s between requests (engine), every
  response stored. No proxies, no User-Agent rotation, no evasion. If VNDirect refuses, slow down or stop.
- **Not advice:** every tool result carries the disclaimer. Tool descriptions and the server instructions tell the
  calling model not to present results as buy/sell calls. Keep it so.
- Tools stay read-only (`read_only_hint=True`, `destructive_hint=False`). A tool that writes anything beyond the
  local cache needs a design review first.
- New tool inputs: validate type, range and empty values (pydantic `Field` in server.py and checks in service.py),
  and add a bad-input test.
- Errors users can act on → `ScreenerError` (becomes an MCP tool error with that text). Data-source failures are
  caught as `service.SOURCE_ERRORS`.

## Release

1. Bump the version in `pyproject.toml`, `src/vn_value_screener/__init__.py` and `server.json` (both places), then
   run `check_versions.py`.
2. Commit, then tag `vX.Y.Z` and push the tag. `release.yml` does the rest.
3. Actions are pinned to full commit hashes, with the version in a comment. setup-uv publishes no floating major
   tag (`@v10` failed on 2026-11-24). Bump by resolving the new tag: `gh api repos/<owner>/<action>/git/ref/tags/<tag>`.
4. To bump `mcp-publisher`: change `MCP_PUBLISHER_VERSION` and `MCP_PUBLISHER_SHA256` in `release.yml`. The sha256
   is the `mcp-publisher_linux_amd64.tar.gz` line in that release's `registry_<v>_checksums.txt`.

The MCP Registry is still in preview (data resets possible). It stores only metadata; the code is the PyPI package.
To be listed it needs the `mcp-name: io.github.Nghiauet/vn-value-screener` line in README.md, which becomes the PyPI
description. Glama picks the package up from the registry. Claude's Connectors Directory needs a remote HTTP server,
so it is not in scope.
