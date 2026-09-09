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
    python -m kg_ingest.materialize --direct   # copy parts directly (low RSS)
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import sys
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


def copy_parts(parts_dir: Path = SNAP_PARTS, out_dir: Path = OUT_DIR) -> int:
    """Copy snapshot/parts/*.nt to out/parts/ with no rdflib parsing.

    Returns the number of files copied. Writes to a temporary sibling directory
    first, then renames it into place so a concurrent server startup never reads
    a half-written parts tree.
    """
    parts = sorted(parts_dir.glob("*.nt"))
    if not parts:
        raise SystemExit(f"[materialize] no snapshot parts found in {parts_dir}")

    out_parts = out_dir / "parts"
    tmp_parts = out_dir / "parts.tmp"

    out_dir.mkdir(parents=True, exist_ok=True)
    if tmp_parts.exists():
        shutil.rmtree(tmp_parts)
    tmp_parts.mkdir(parents=True)

    for src in parts:
        shutil.copy2(src, tmp_parts / src.name)

    if out_parts.exists():
        shutil.rmtree(out_parts)
    os.rename(str(tmp_parts), str(out_parts))

    return len(parts)


def peak_rss_mb() -> int:
    """Peak resident set size of this process in MB (Linux reports KB, macOS bytes)."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024)


def _graph_age_stamp(parts_dir: Path) -> str:
    """Return the dcterms:modified stamp on the spine IRI via a line scan (no rdflib parse).

    Scans N-Triples files for the triple whose subject is G_SPINE and whose predicate is
    dcterms:modified, then extracts the literal value without loading rdflib.Dataset.
    """
    from . import iris
    spine_subject = f"<{iris.G_SPINE}>"
    modified_pred = "<http://purl.org/dc/terms/modified>"
    for nt_file in sorted(parts_dir.glob("*.nt")):
        for line in nt_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith(spine_subject):
                continue
            rest = line[len(spine_subject):].strip()
            if not rest.startswith(modified_pred):
                continue
            obj = rest[len(modified_pred):].strip()
            # NT literal form: "value"^^<datatype> .  or  "value" .
            if obj.startswith('"'):
                end = obj.index('"', 1)
                return obj[1:end]
    return ""


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
        description="Reconstitute out/graph.trig (or out/parts/) from committed snapshot parts."
    )
    ap.add_argument(
        "--no-embed",
        action="store_true",
        help="skip rebuilding the semantic embeddings sidecar (server runs lexical-only)",
    )
    ap.add_argument(
        "--direct",
        action="store_true",
        help=(
            "copy snapshot/parts/*.nt to out/parts/ without rdflib re-serialization "
            "(low-RSS path for in-place refresh; use KG_BACKEND=nt_parts to serve)"
        ),
    )
    args = ap.parse_args(argv)

    if args.direct:
        n = copy_parts()
        print(f"[materialize] out/parts/ from snapshot/parts — {n} files (peak RSS {peak_rss_mb()} MB)")
        if args.no_embed:
            print("[materialize] --no-embed: semantic search will run degraded (lexical-only)")
            return 0
        copy_embeddings()
        print("[materialize] embeddings copied from snapshot/")
        return 0

    n = materialize_graph()
    # Peak RSS is printed so an OOM kill on the next run has a number beside it:
    # materialize holds the whole graph in rdflib (~270 MB at ~31k quads) and, beside a
    # serving sidecar, exceeds a 512 MB host. See README "Host sizing".
    print(f"[materialize] out/graph.trig from snapshot/parts — {n} triples (peak RSS {peak_rss_mb()} MB)")

    if args.no_embed:
        print("[materialize] --no-embed: semantic search will run degraded (lexical-only)")
        return 0

    copy_embeddings()
    print("[materialize] embeddings copied from snapshot/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
