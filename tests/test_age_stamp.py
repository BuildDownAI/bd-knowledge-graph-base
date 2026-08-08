"""Verify the graph age stamp survives the snapshot -> materialize round-trip.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_age_stamp.py

The property that matters: the ingest stamps dcterms:modified on the spine IRI
(when was this data current?), and a fresh clone that rebuilds the graph via
`kg_ingest.materialize` can still answer that date through an ordinary SPARQL
query — so a deployed sidecar's graph is self-describing about its own age.
"""
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS, XSD

from kg_ingest import iris, materialize, snapshot


def test_age_stamp_roundtrip():
    src = Graph()
    stamp = Literal(datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    datatype=XSD.dateTime)
    src.set((iris.G_SPINE, DCTERMS.modified, stamp))
    # a bystander triple so the graph isn't stamp-only
    src.add((URIRef(f"{iris.NAMESPACE}resource/issue/EX-1"),
             DCTERMS.title, Literal("Example issue")))

    with tempfile.TemporaryDirectory() as d:
        parts = Path(d) / "parts"
        out = Path(d) / "out"
        snapshot.write_parts(src, parts)
        materialize.materialize_graph(parts_dir=parts, out_dir=out)

        ds = Dataset()
        ds.parse(str(out / "graph.trig"), format="trig")
        rebuilt = Graph()
        for quad in ds:
            rebuilt.add(quad[:3])

        rows = list(rebuilt.query(
            "SELECT ?d WHERE { ?s <http://purl.org/dc/terms/modified> ?d }",
        ))
        assert len(rows) == 1, f"expected exactly one age stamp, got {len(rows)}"
        assert str(rows[0][0]) == str(stamp), "stamp changed across round-trip"
    print("test_age_stamp_roundtrip: OK")


if __name__ == "__main__":
    test_age_stamp_roundtrip()
