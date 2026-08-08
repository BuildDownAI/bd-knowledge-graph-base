"""Verify materialize reconstitutes the graph losslessly from snapshot/parts.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_materialize.py

The property that matters for the KG sidecar: a fresh clone (which has
snapshot/parts/*.nt but NOT the gitignored out/graph.trig) can rebuild a graph
that is triple-for-triple identical to the original — so `kg_ingest.materialize`
must round-trip write_parts() with no loss.
"""
import tempfile
from pathlib import Path

from rdflib import Dataset, Graph, URIRef, Literal

from kg_ingest import snapshot, materialize
from kg_ingest.iris import NAMESPACE as NS


def _fixture_union() -> Graph:
    g = Graph()
    for i in range(1, 6):
        s = URIRef(f"{NS}resource/issue/EX-{i}")
        g.add((s, URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), URIRef(f"{NS}onto#Issue")))
        g.add((s, URIRef("http://purl.org/dc/terms/title"), Literal(f"Example issue {i}")))
    return g


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


if __name__ == "__main__":
    test_materialize_roundtrip()
