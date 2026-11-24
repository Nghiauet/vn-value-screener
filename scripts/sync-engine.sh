#!/usr/bin/env bash
# Re-copy the screening engine from a stock-analyzer checkout, unchanged, at a committed revision.
#
#   bash scripts/sync-engine.sh <path to stock-analyzer checkout> [revision, default HEAD]
#
# Copies only committed files (git show), so uncommitted edits upstream never leak in. Then run the tests, and
# compare a live screen with upstream's report for the same day before releasing.
set -euo pipefail

src=${1:?usage: sync-engine.sh <stock-analyzer checkout> [revision]}
rev=$(git -C "$src" rev-parse "${2:-HEAD}")
dest=$(cd "$(dirname "$0")/.." && pwd)/src/vn_value_screener/engine
files=(config store vndirect vps data graham quality fisher report)

for f in "${files[@]}"; do
  git -C "$src" show "$rev:src/stock_analyzer/$f.py" > "$dest/$f.py"
done

# Imports between engine files must stay inside the copied set.
if grep -hoE '^from \.[a-z_]+' "$dest"/*.py | sort -u | grep -vE "^from \.($(IFS='|'; echo "${files[*]}"))$"; then
  echo "error: the engine now imports a module that isn't copied; add it to files=(...)" >&2
  exit 1
fi

sed -i -E "s/^Source revision: [0-9a-f]+$/Source revision: $rev/; s/^SOURCE_REVISION = \"[0-9a-f]+\"$/SOURCE_REVISION = \"$rev\"/" \
  "$dest/__init__.py"
echo "engine copied from $rev"
git -C "$dest" status --short -- . || true
