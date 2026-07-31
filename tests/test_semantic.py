"""End-to-end: build embeddings over a fixture, then assert a PARAPHRASED query
ranks the dedup learning first. Also the missing-sidecar path.
Gated on fastembed. Run: PYTHONPATH=. ./.venv/bin/python tests/test_semantic.py
"""
import os, tempfile
from pathlib import Path

try:
    import fastembed  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

from kg_query import semantic
from kg_query.store import RdflibStore
from kg_ingest.embed import build_embeddings

FIXTURE = """
@prefix kg: <https://example.org/kg/onto#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<https://example.org/kg/resource/graph/spine> {
  <https://example.org/kg/resource/comment/PROJ-259/0> a kg:Learning ;
    dcterms:title "Dispatch dedup has no TTL - a torn-down issue can never be re-dispatched" ;
    kg:fix "Add a TTL so a torn-down issue can be re-dispatched." ;
    kg:tagged <https://example.org/kg/resource/topic/dedup> .
  <https://example.org/kg/resource/topic/dedup> a kg:Topic ; skos:prefLabel "dedup" .
  <https://example.org/kg/resource/comment/PROJ-222/0> a kg:Learning ;
    dcterms:title "Auto-merge child PRs into feature branches" ;
    kg:fix "Recursively merge completed child PRs up the feature tree." ;
    kg:tagged <https://example.org/kg/resource/topic/branching> .
  <https://example.org/kg/resource/topic/branching> a kg:Topic ; skos:prefLabel "branching" .
}
"""

def test_missing_sidecar():
    r = semantic.semantic_search("anything", npz_path=Path("/nonexistent/x.npz"))
    assert r["results"] == [] and "error" in r, r
    print("PASS: missing sidecar -> friendly error")

def test_ranks_dedup_first():
    d = tempfile.mkdtemp()
    Path(d, "g.trig").write_text(FIXTURE)
    build_embeddings(RdflibStore(os.path.join(d, "g.trig")), out_dir=Path(d))
    semantic._index = None  # reset singleton for the fixture path
    r = semantic.semantic_search("how do we stop re-dispatching a torn-down issue",
                                 limit=2, npz_path=Path(d) / "embeddings.npz")
    assert r["results"], r
    top = r["results"][0]
    assert "dedup" in (top["title"] + top["snippet"]).lower(), top
    assert r["results"][0]["score"] >= r["results"][-1]["score"]
    print(f"PASS: paraphrase ranked dedup first (score {top['score']})")

def main():
    test_missing_sidecar()
    if not HAVE:
        print("SKIP: fastembed not installed — build/query path not exercised"); return
    test_ranks_dedup_first()
    print("\nall semantic tests passed")

if __name__ == "__main__":
    main()
