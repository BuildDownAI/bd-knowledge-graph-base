"""Verify materialize reconstitutes the graph losslessly from snapshot/parts,
copies committed embeddings without importing fastembed, and enforces the
age-stamp contract between snapshot/embeddings.meta.json and the graph.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_materialize.py

The property that matters for the KG sidecar: a fresh clone (which has
snapshot/parts/*.nt but NOT the gitignored out/graph.trig) can rebuild a graph
that is triple-for-triple identical to the original — so `kg_ingest.materialize`
must round-trip write_parts() with no loss.
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, XSD

from kg_ingest import materialize, snapshot
from kg_ingest.iris import G_SPINE, NAMESPACE as NS

STAMP = "2026-08-01T00:00:00+00:00"


def _fixture_union() -> Graph:
    g = Graph()
    for i in range(1, 6):
        s = URIRef(f"{NS}resource/issue/EX-{i}")
        g.add((s, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), URIRef(f"{NS}onto#Issue")))
        g.add((s, URIRef("http://purl.org/dc/terms/title"), Literal(f"Example issue {i}")))
    # Age stamp on the spine IRI — materialize reads this from parts to verify embeddings.
    g.add((G_SPINE, DCTERMS.modified, Literal(STAMP, datatype=XSD.dateTime)))
    return g


def test_fastembed_not_imported():
    """Importing kg_ingest.materialize must not pull in fastembed."""
    assert "fastembed" not in sys.modules, (
        "fastembed was imported as a side-effect of importing kg_ingest.materialize"
    )
    print("test_fastembed_not_imported: OK")


def test_materialize_roundtrip():
    src = _fixture_union()
    with tempfile.TemporaryDirectory() as d:
        parts = Path(d) / "parts"
        out = Path(d) / "out"
        snapshot.write_parts(src, parts)
        n = materialize.materialize_graph(parts_dir=parts, out_dir=out)

        # Reload what materialize wrote and flatten to a union, exactly like the store.
        ds = Dataset()
        ds.parse(str(out / "graph.trig"), format="trig")
        rebuilt = Graph()
        for quad in ds:
            rebuilt.add(quad[:3])

        assert n == len(src), f"materialize triple count {n} != source {len(src)}"
        assert set(rebuilt) == set(src), "materialized graph is not triple-identical to source"
    print("test_materialize_roundtrip: OK")


def test_copy_embeddings_byte_identical():
    """out/embeddings.npz is byte-identical to snapshot/embeddings.npz after copy."""
    import numpy as np

    src = _fixture_union()
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        parts = d / "parts"
        snap = d / "snapshot"
        out = d / "out"

        snapshot.write_parts(src, parts)

        snap.mkdir(parents=True)
        vecs = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
        np.savez_compressed(snap / "embeddings.npz", vectors=vecs)
        meta = {"model": "test", "dim": 3, "count": 1, "age_stamp": STAMP}
        (snap / "embeddings.meta.json").write_text(json.dumps(meta) + "\n")

        materialize.copy_embeddings(snap_dir=snap, out_dir=out, parts_dir=parts)

        snap_digest = hashlib.sha256((snap / "embeddings.npz").read_bytes()).hexdigest()
        out_digest = hashlib.sha256((out / "embeddings.npz").read_bytes()).hexdigest()
        assert snap_digest == out_digest, (
            "out/embeddings.npz is not byte-identical to snapshot/embeddings.npz"
        )
    print("test_copy_embeddings_byte_identical: OK")


def test_stamp_mismatch_exits_nonzero():
    """Stamp mismatch in committed meta → SystemExit naming the file and both stamps."""
    import numpy as np

    src = _fixture_union()
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        parts = d / "parts"
        snap = d / "snapshot"
        out = d / "out"

        snapshot.write_parts(src, parts)

        snap.mkdir(parents=True)
        wrong_stamp = "2020-01-01T00:00:00+00:00"
        vecs = np.array([[1.0, 2.0, 3.0]], dtype=np.float32)
        np.savez_compressed(snap / "embeddings.npz", vectors=vecs)
        meta = {"model": "test", "dim": 3, "count": 1, "age_stamp": wrong_stamp}
        (snap / "embeddings.meta.json").write_text(json.dumps(meta) + "\n")

        try:
            materialize.copy_embeddings(snap_dir=snap, out_dir=out, parts_dir=parts)
            assert False, "copy_embeddings should have raised SystemExit on stamp mismatch"
        except SystemExit as exc:
            msg = str(exc)
            assert "embeddings.meta.json" in msg, (
                f"exit message must name the meta file; got: {msg!r}"
            )
            assert wrong_stamp in msg, (
                f"exit message must name the found stamp {wrong_stamp!r}; got: {msg!r}"
            )
            assert STAMP in msg, (
                f"exit message must name the expected stamp {STAMP!r}; got: {msg!r}"
            )
    print("test_stamp_mismatch_exits_nonzero: OK")


def test_missing_npz_exits_nonzero():
    """Missing snapshot/embeddings.npz → SystemExit naming the file."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        snap = d / "snapshot"
        out = d / "out"
        parts = d / "parts"
        snap.mkdir(parents=True)
        parts.mkdir(parents=True)

        try:
            materialize.copy_embeddings(snap_dir=snap, out_dir=out, parts_dir=parts)
            assert False, "copy_embeddings should have raised SystemExit for missing npz"
        except SystemExit as exc:
            msg = str(exc)
            assert "embeddings.npz" in msg, (
                f"exit message must name embeddings.npz; got: {msg!r}"
            )
    print("test_missing_npz_exits_nonzero: OK")


if __name__ == "__main__":
    test_fastembed_not_imported()
    test_materialize_roundtrip()
    test_copy_embeddings_byte_identical()
    test_stamp_mismatch_exits_nonzero()
    test_missing_npz_exits_nonzero()
