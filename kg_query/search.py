"""Terminal search over the KG — the same read tools the MCP server exposes,
usable straight from a shell without an MCP client.

Usage:
  kg-query search "webhook"            # or: python -m kg_query.search "webhook"
  kg-query search --limit 15 gap-fill
  kg-query search --semantic "flaky preview deploys"   # vector (embeddings sidecar)
  kg-query search --hybrid "AII-256 retries"           # lexical + vector, RRF-fused
  kg-query search --neighbors <iri>
  kg-query search --provenance <iri>

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


def _print_scored(res: dict, kind: str) -> None:
    extra = " (degraded: lexical only — no embeddings sidecar)" if res.get("degraded") else ""
    print(f"# kg_{kind}_search({res['query']!r}) — {res['count']} hit(s){extra}\n")
    if res.get("error"):
        print(f"  {res['error']}")
        return
    for r in res["results"]:
        via = f"  via {'+'.join(r['matched_by'])}" if r.get("matched_by") else ""
        typ = f"  ({r['type']})" if r.get("type") else ""
        print(f"- [{r['score']:.4f}] {r['title']}{typ}{via}")
        if r.get("snippet"):
            print(f"    {r['snippet'][:200].strip()}")
        print(f"    iri: {r['iri']}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kg-query search")
    ap.add_argument("term", nargs="*", help="search term(s)")
    ap.add_argument("--limit", type=int, default=10)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--semantic", action="store_true",
                      help="vector search via the embeddings sidecar (paraphrase-tolerant)")
    mode.add_argument("--hybrid", action="store_true",
                      help="lexical + vector fused with RRF (best default for prose queries)")
    mode.add_argument("--neighbors", metavar="IRI", help="1-hop neighbors of a node")
    mode.add_argument("--provenance", metavar="IRI", help="provenance of a node")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    args = ap.parse_args(argv)

    store = get_store()
    kind = None
    if args.neighbors:
        out = queries.kg_neighbors(store, args.neighbors, limit=args.limit)
    elif args.provenance:
        out = queries.kg_provenance(store, args.provenance)
    else:
        term = " ".join(args.term).strip()
        if not term:
            ap.error("provide a search term (or --neighbors / --provenance IRI)")
        if args.semantic:
            from . import semantic
            kind = "semantic"
            out = semantic.semantic_search(term, limit=args.limit)
        elif args.hybrid:
            from . import hybrid
            kind = "hybrid"
            out = hybrid.hybrid_search(store, term, limit=args.limit)
        else:
            out = queries.kg_search(store, term, limit=args.limit)

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    elif kind:
        _print_scored(out, kind)
    elif "results" in out:
        _print_search(out)
    else:
        print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
