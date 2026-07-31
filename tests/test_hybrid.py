"""Hybrid search fuses lexical + semantic (RRF), boosts exact IDs, and degrades
to lexical-only when the vector index is missing. Vector arm gated on fastembed.
Run: PYTHONPATH=. ./.venv/bin/python tests/test_hybrid.py
"""
import os, tempfile
from pathlib import Path

try:
    import fastembed  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

from kg_query import hybrid, semantic
from kg_query.store import RdflibStore
from kg_ingest.iris import DEFAULT_NAMESPACE as _DNS, NAMESPACE as _NS
from kg_ingest.embed import build_embeddings

FIXTURE_RAW = """
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
  <https://example.org/kg/resource/issue/PROJ-260> a kg:Issue ;
    dcterms:title "Widget rendering glitch in the admin dashboard" ;
    kg:label "widget" ;
    kg:description "Widgets flicker on load." .
}
"""
FIXTURE = FIXTURE_RAW.replace(_DNS, _NS)


def _fixture_store():
    d = tempfile.mkdtemp()
    Path(d, "g.trig").write_text(FIXTURE)
    return RdflibStore(os.path.join(d, "g.trig")), Path(d)


def test_degraded_lexical_only():
    store, _ = _fixture_store()
    semantic._index = None
    r = hybrid.hybrid_search(store, "dedup", npz_path=Path("/nonexistent/x.npz"))
    assert r["degraded"] is True, r
    assert r["results"], r
    assert any("PROJ-259" in x["iri"] for x in r["results"]), r
    assert all(x["matched_by"] == ["lexical"] for x in r["results"]), r
    print("PASS: missing sidecar -> degraded lexical-only")


def test_paraphrase_via_semantic():
    store, d = _fixture_store()
    build_embeddings(store, out_dir=d)
    semantic._index = None
    r = hybrid.hybrid_search(store, "how do we stop re-dispatching a torn-down issue",
                             limit=5, npz_path=d / "embeddings.npz")
    assert r["degraded"] is False, r
    top = r["results"][0]
    assert "dedup" in (top["title"] + (top.get("snippet") or "")).lower(), top
    assert "semantic" in top["matched_by"], top
    # scores descending
    assert all(r["results"][i]["score"] >= r["results"][i + 1]["score"]
               for i in range(len(r["results"]) - 1)), r
    print(f"PASS: paraphrase surfaces dedup via {top['matched_by']}")


def test_exact_id_boost():
    store, d = _fixture_store()
    build_embeddings(store, out_dir=d)
    semantic._index = None
    r = hybrid.hybrid_search(store, "PROJ-260", limit=5, npz_path=d / "embeddings.npz")
    assert r["results"], r
    assert "PROJ-260" in r["results"][0]["iri"], r["results"][0]
    print("PASS: exact issue-key ranked first")


def test_partial_id_no_false_boost():
    # "PROJ-26" is a valid-looking key token but NOT a whole-token match for
    # "PROJ-260" — the boost must not fire (no result gets the dominant +1.0).
    store, d = _fixture_store()
    build_embeddings(store, out_dir=d)
    semantic._index = None
    r = hybrid.hybrid_search(store, "PROJ-26", limit=5, npz_path=d / "embeddings.npz")
    assert all(x["score"] < 1.0 for x in r["results"]), r
    print("PASS: partial issue-key does not false-boost PROJ-260")


def main():
    test_degraded_lexical_only()
    if not HAVE:
        print("SKIP: fastembed not installed — vector arm not exercised"); return
    test_paraphrase_via_semantic()
    test_exact_id_boost()
    test_partial_id_no_false_boost()
    print("\nall hybrid tests passed")


if __name__ == "__main__":
    main()
