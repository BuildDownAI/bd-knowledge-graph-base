"""Terminal search over the KG — the same read tools the MCP server exposes,
usable straight from a shell without an MCP client.

Usage:
  PYTHONPATH=. ./.venv/bin/python -m kg_query.search "webhook"
  PYTHONPATH=. ./.venv/bin/python -m kg_query.search --limit 15 gap-fill
  PYTHONPATH=. ./.venv/bin/python -m kg_query.search --neighbors <iri>
  PYTHONPATH=. ./.venv/bin/python -m kg_query.search --provenance <iri>

Backend follows KG_BACKEND (rdflib default). Read-only.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import queries
from .store import get_store


def _print_search(res: dict) -> None:
    print(f"# kg_search({res['term']!r}) — {res['count']} hit(s) via {res['backend']}\n")
    if not res["results"]:
        print("  (no matches — try a broader term)")
        return
    for r in res["results"]:
        pri = f"[{r['priority']}] " if r.get("priority") else ""
        cat = f"  ({r['category']})" if r.get("category") else ""
        print(f"- {pri}{r['title']}{cat}")
        if r.get("matched_topics"):
            print(f"    matched: {', '.join(r['matched_topics'])}")
        if r.get("fix"):
            print(f"    fix/detail: {r['fix'][:200].strip()}")
        print(f"    iri: {r['iri']}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kg_query.search")
    ap.add_argument("term", nargs="*", help="search term(s)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--neighbors", metavar="IRI", help="1-hop neighbors of a node")
    ap.add_argument("--provenance", metavar="IRI", help="provenance of a node")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    args = ap.parse_args(argv)

    store = get_store()
    if args.neighbors:
        out = queries.kg_neighbors(store, args.neighbors, limit=args.limit)
    elif args.provenance:
        out = queries.kg_provenance(store, args.provenance)
    else:
        term = " ".join(args.term).strip()
        if not term:
            ap.error("provide a search term (or --neighbors / --provenance IRI)")
        out = queries.kg_search(store, term, limit=args.limit)

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    elif "results" in out:
        _print_search(out)
    else:
        print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
