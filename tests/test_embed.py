"""Build embeddings over a fixture graph; assert the sidecar files + meta.
Downloads the model on first run. Gated on fastembed.
Run: PYTHONPATH=. ./.venv/bin/python tests/test_embed.py
"""
import os, json, tempfile
from pathlib import Path

try:
    import fastembed  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

from kg_query.store import RdflibStore
from kg_ingest.embed import build_embeddings, MODEL, DIM

FIXTURE = """
@prefix kg: <https://example.org/kg/onto#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<https://example.org/kg/resource/graph/spine> {
  <https://example.org/kg/resource/comment/PROJ-259/0> a kg:Learning ;
    dcterms:title "dedup has no TTL" ; kg:fix "Add a TTL to dispatch dedup." ;
    kg:tagged <https://example.org/kg/resource/topic/dedup> .
  <https://example.org/kg/resource/topic/dedup> a kg:Topic ; skos:prefLabel "dedup" .
  <https://example.org/kg/resource/adr/0001> a kg:Decision ;
    dcterms:title "Use rdflib" ; kg:detail "Zero-infra local dev." .
}
"""

def main():
    if not HAVE:
        print("SKIP: fastembed not installed"); return
    d = tempfile.mkdtemp()
    trig = os.path.join(d, "g.trig")
    Path(trig).write_text(FIXTURE)
    meta = build_embeddings(RdflibStore(trig), out_dir=Path(d))
    assert (Path(d) / "embeddings.npz").exists()
    assert (Path(d) / "embeddings.meta.json").exists()
    assert meta["model"] == MODEL and meta["dim"] == DIM
    assert meta["count"] == 2, meta
    on_disk = json.loads((Path(d) / "embeddings.meta.json").read_text())
    assert on_disk == meta
    print(f"PASS: build_embeddings -> {meta['count']} vectors dim {meta['dim']}")

if __name__ == "__main__":
    main()
