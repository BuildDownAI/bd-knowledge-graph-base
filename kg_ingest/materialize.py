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
import json
import shutil
from pathlib import Path

from rdflib import Dataset
from rdflib.namespace import DCTERMS

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "snapshot"
SNAP_PARTS = SNAP_DIR / "parts"
OUT_DIR = ROOT / "out"  # KGB-10 will make this configurable


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


def _graph_age_stamp(parts_dir: Path) -> str:
    """Return the dcterms:modified stamp on the spine IRI from the committed parts."""
    from . import iris
    ds = Dataset()
    g = ds.graph()
    for part in sorted(parts_dir.glob("*.nt")):
        g.parse(part, format="nt")
    val = g.value(iris.G_SPINE, DCTERMS.modified)
    return str(val) if val is not None else ""


def copy_embeddings(
    snap_dir: Path = SNAP_DIR,
    out_dir: Path = OUT_DIR,
    parts_dir: Path = SNAP_PARTS,
) -> None:
    """Copy committed snapshot/embeddings.* to out/, verifying the age stamp.

    Exits non-zero (naming the relevant file and stamps) if the committed meta
    does not match the materialized graph's dcterms:modified, or if committed
    files are missing. Does not import fastembed.
    """
    snap_npz = snap_dir / "embeddings.npz"
    snap_meta = snap_dir / "embeddings.meta.json"

    if not snap_npz.exists():
        raise SystemExit(
            f"[materialize] missing committed embeddings: {snap_npz}\n"
            f"Run a full ingest (python -m kg_ingest.cli --repo ...) to generate them."
        )
    if not snap_meta.exists():
        raise SystemExit(
            f"[materialize] missing committed embeddings meta: {snap_meta}\n"
            f"Run a full ingest (python -m kg_ingest.cli --repo ...) to generate them."
        )

    committed_stamp = json.loads(snap_meta.read_text()).get("age_stamp", "")
    graph_stamp = _graph_age_stamp(parts_dir)

    if graph_stamp != committed_stamp:
        raise SystemExit(
            f"[materialize] embedding stamp mismatch in {snap_meta}\n"
            f"  expected (graph dcterms:modified): {graph_stamp!r}\n"
            f"  found    (file age_stamp):         {committed_stamp!r}\n"
            f"Run a full ingest to regenerate."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snap_npz, out_dir / "embeddings.npz")
    shutil.copy2(snap_meta, out_dir / "embeddings.meta.json")


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

    copy_embeddings()
    print("[materialize] embeddings copied from snapshot/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
