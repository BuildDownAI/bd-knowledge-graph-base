"""Reconstitute the server-loadable graph (out/graph.trig [+ embeddings]) from the
committed, diffable snapshot (snapshot/parts/*.nt) — with NO access to the
original sources (tracker, repos).

Why this exists: out/graph.trig and out/embeddings.npz are gitignored build
outputs; only snapshot/parts/*.nt is committed (see snapshot.py — "cat
snapshot/parts/*.nt reconstitutes the graph"). A fresh `git clone` therefore has
the graph *data* but not the *loadable* form the MCP server reads at startup.
This command builds that loadable form from what's in git, so a deploy/image
build produces a working KG sidecar from a clone alone.

Usage:
    python -m kg_ingest.materialize            # graph.trig + embeddings
    python -m kg_ingest.materialize --no-embed # graph.trig only (lexical-only,
                                               #   server runs degraded=true)
"""
from __future__ import annotations

import argparse
from pathlib import Path

from rdflib import Dataset

ROOT = Path(__file__).resolve().parent.parent
SNAP_PARTS = ROOT / "snapshot" / "parts"
OUT_DIR = ROOT / "out"


def materialize_graph(parts_dir: Path = SNAP_PARTS, out_dir: Path = OUT_DIR) -> int:
    """Load every snapshot/parts/*.nt into one graph and write out/graph.trig.

    Returns the triple count. The store flattens named graphs into a union at
    query time, so the parts (flat N-Triples) round-trip through a single graph
    with no loss — verified triple-for-triple against a full ingest.
    """
    parts = sorted(parts_dir.glob("*.nt"))
    if not parts:
        raise SystemExit(f"[materialize] no snapshot parts found in {parts_dir}")
    ds = Dataset()
    g = ds.graph()
    for part in parts:
        g.parse(part, format="nt")
    out_dir.mkdir(parents=True, exist_ok=True)
    ds.serialize(out_dir / "graph.trig", format="trig")
    return len(g)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Reconstitute out/graph.trig from committed snapshot parts."
    )
    ap.add_argument(
        "--no-embed",
        action="store_true",
        help="skip rebuilding the semantic embeddings sidecar (server runs lexical-only)",
    )
    args = ap.parse_args(argv)

    n = materialize_graph()
    print(f"[materialize] out/graph.trig from snapshot/parts — {n} triples")

    if args.no_embed:
        print("[materialize] --no-embed: semantic search will run degraded (lexical-only)")
        return 0

    from . import embed

    embed.main()
    print("[materialize] embeddings rebuilt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
