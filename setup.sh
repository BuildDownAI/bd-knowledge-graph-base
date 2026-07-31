#!/usr/bin/env bash
# One-shot local setup for this knowledge graph.
#   ./setup.sh                 # venv + deps + build graph (docs+code only)
#   ./setup.sh --tracker       # also ingest Linear issues (needs LINEAR_API_KEY)
#   ./setup.sh --tracker --secondary   # also fold in secondary_repos
#
# Requires Python 3.10+ (the `mcp` package won't install on 3.9).
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3.10}"
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python3
fi
"$PY" - <<'PYCHK'
import sys
assert sys.version_info >= (3, 10), f"Python 3.10+ required, found {sys.version.split()[0]}"
print(f"using Python {sys.version.split()[0]}")
PYCHK

[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

# code repo path comes from sources.yml (code_repo.path); override with KG_REPO
REPO="${KG_REPO:-$(./.venv/bin/python -c "from kg_ingest import sources; print((sources.load().get('code_repo') or {}).get('path') or '.')" 2>/dev/null || echo .)}"
echo "== building graph from $REPO $* =="
./.venv/bin/python -m kg_ingest.cli --repo "$REPO" "$@"

echo
echo "Done. Next:"
echo "  - search:   PYTHONPATH=. ./.venv/bin/python -m kg_query.search \"webhook\""
echo "  - register: cp .mcp.json.example .mcp.json  (then set the absolute paths for YOUR clone)"
