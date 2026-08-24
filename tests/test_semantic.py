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
}
"""
FIXTURE = FIXTURE_RAW.replace(_DNS, _NS)

def test_resolve_npz_no_vars():
    saved = {k: os.environ.pop(k, None) for k in ("KG_NPZ", "KG_DATA_DIR")}
    try:
        assert semantic.resolve_npz() == semantic.NPZ, semantic.resolve_npz()
        print("PASS: no env vars -> default NPZ path")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_resolve_npz_data_dir():
    d = tempfile.mkdtemp()
    saved = {k: os.environ.pop(k, None) for k in ("KG_NPZ", "KG_DATA_DIR")}
    os.environ["KG_DATA_DIR"] = d
    try:
        assert semantic.resolve_npz() == Path(d) / "embeddings.npz", semantic.resolve_npz()
        print(f"PASS: KG_DATA_DIR -> {d}/embeddings.npz")
    finally:
        os.environ.pop("KG_DATA_DIR", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_resolve_npz_kg_npz_wins():
    d = tempfile.mkdtemp()
    alt = "/tmp/alt.npz"
    saved = {k: os.environ.pop(k, None) for k in ("KG_NPZ", "KG_DATA_DIR")}
    os.environ["KG_DATA_DIR"] = d
    os.environ["KG_NPZ"] = alt
    try:
        assert semantic.resolve_npz() == Path(alt), semantic.resolve_npz()
        print("PASS: KG_NPZ beats KG_DATA_DIR")
    finally:
        os.environ.pop("KG_DATA_DIR", None)
        os.environ.pop("KG_NPZ", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


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
    test_resolve_npz_no_vars()
    test_resolve_npz_data_dir()
    test_resolve_npz_kg_npz_wins()
    test_missing_sidecar()
    if not HAVE:
        print("SKIP: fastembed not installed — build/query path not exercised"); return
    test_ranks_dedup_first()
    print("\nall semantic tests passed")

if __name__ == "__main__":
    main()
