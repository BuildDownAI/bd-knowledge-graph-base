"""Exercise the read tools against a self-contained fixture graph.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_query.py
(Backend parity rdflib vs Stardog lives in tests/compare_backends.py — it needs a
loaded Stardog and real data, so it is not part of this template's default suite.)
"""
import json
import tempfile
from pathlib import Path

from kg_query import queries
from kg_query.store import RdflibStore

from kg_ingest.iris import NAMESPACE as NS

FIXTURE = f"""
@prefix kg: <{NS}onto#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<{NS}resource/graph/spine> {{
  <{NS}resource/doc/example/webhook-retries> a kg:Doc ;
    dcterms:title "Webhook retry playbook" .
  <{NS}resource/topic/webhook> a kg:Topic ; skos:prefLabel "webhook" .
  <{NS}resource/repo/test-org%2Fadmin> a kg:Repo ;
    kg:sourceId "test-org/admin" ;
    dcterms:title "test-org/admin" ;
    kg:historyBranch "master" ;
    kg:isDefaultBranch "true"^^xsd:boolean ;
    kg:snapshotRef "abc123" .
  <{NS}resource/repo/test-org%2Ffixture> a kg:Repo ;
    kg:sourceId "test-org/fixture" ;
    dcterms:title "test-org/fixture" ;
    kg:historyBranch "main" ;
    kg:isDefaultBranch "true"^^xsd:boolean ;
    kg:snapshotRef "def456" .
  <{NS}resource/commit/test-org%2Ffixture/abc123> a kg:Commit ;
    kg:sourceId "abc123" ;
    dcterms:title "fix login bug" ;
    dcterms:created "2026-01-15T10:00:00Z"^^xsd:dateTime ;
    kg:partOf <{NS}resource/repo/test-org%2Ffixture> .
  <{NS}resource/pr/test-org%2Ffixture/4257> a kg:PullRequest ;
    kg:number 4257 ;
    dcterms:title "fix login: prevent double auth on session resume" ;
    kg:state "merged" ;
    kg:mergedAt "2026-01-16T12:00:00Z"^^xsd:dateTime ;
    kg:baseRefName "main" ;
    kg:partOf <{NS}resource/repo/test-org%2Ffixture> .
}}
<{NS}resource/graph/run/testrun> {{
  <{NS}resource/run/testrun> a kg:ExtractionRun ;
    kg:pipelineVer "0.0-test" ; kg:model "deterministic-parser" ;
    kg:promptHash "n/a" .
  <{NS}resource/learning/webhook-dedup> a kg:Learning ;
    dcterms:title "Webhook deliveries can duplicate - consumers must dedup" ;
    kg:fix "Key handling on the delivery id; ignore repeats." ;
    kg:tagged <{NS}resource/topic/webhook> ;
    prov:wasDerivedFrom <{NS}resource/doc/example/webhook-retries> ;
    prov:wasGeneratedBy <{NS}resource/run/testrun> .
  <{NS}resource/comment/TEST-1/1> a kg:ImplementationNote ;
    dcterms:title "TEST-1 implementation summary" ;
    prov:wasDerivedFrom <{NS}resource/doc/example/webhook-retries> ;
    prov:wasGeneratedBy <{NS}resource/run/testrun> .
  <{NS}resource/comment/TEST-2/1> a kg:PlanningNote ;
    dcterms:title "TEST-2 planning" ;
    prov:wasDerivedFrom <{NS}resource/doc/example/webhook-retries> ;
    prov:wasGeneratedBy <{NS}resource/run/testrun> .
}}
"""


def main():
    d = tempfile.mkdtemp()
    p = Path(d, "g.trig")
    p.write_text(FIXTURE)
    rd = RdflibStore(p)

    print("== kg_search('webhook') ==")
    r = queries.kg_search(rd, "webhook", limit=5)
    assert r["count"] > 0, "expected the webhook learning"
    hit = r["results"][0]
    print(f"  - {hit['title'][:70]}  via {hit['matched_topics']}")

    print("== kg_search('implementation summary') — ImplementationNote ==")
    r_impl = queries.kg_search(rd, "implementation summary", limit=5)
    impl_hits = [x for x in r_impl["results"] if x["type"] == "ImplementationNote"]
    assert impl_hits, f"expected an ImplementationNote hit, got: {r_impl['results']}"
    print(f"  - {impl_hits[0]['title'][:70]}  type={impl_hits[0]['type']}")

    print("== kg_search('planning') — PlanningNote ==")
    r_plan = queries.kg_search(rd, "planning", limit=5)
    plan_hits = [x for x in r_plan["results"] if x["type"] == "PlanningNote"]
    assert plan_hits, f"expected a PlanningNote hit, got: {r_plan['results']}"
    print(f"  - {plan_hits[0]['title'][:70]}  type={plan_hits[0]['type']}")

    print("== kg_search('admin') — Repo ==")
    r_repo = queries.kg_search(rd, "admin", limit=10)
    repo_hits = [x for x in r_repo["results"] if x["type"] == "Repo"]
    assert repo_hits, f"expected a Repo hit for 'admin', got: {r_repo['results']}"
    print(f"  - {repo_hits[0]['title'][:70]}  type={repo_hits[0]['type']}")

    print("== kg_search('fix login') — PullRequest ==")
    r_pr = queries.kg_search(rd, "fix login", limit=10)
    pr_hits = [x for x in r_pr["results"] if x["type"] == "PullRequest"]
    assert pr_hits, f"expected a PullRequest hit for 'fix login', got: {r_pr['results']}"
    print(f"  - {pr_hits[0]['title'][:70]}  type={pr_hits[0]['type']}")

    print("== kg_provenance(hit) ==")
    prov = queries.kg_provenance(rd, hit["iri"])
    print("  " + json.dumps({k: prov[k] for k in ("types", "generated_by")}, default=str))
    assert prov["generated_by"] and prov["derived_from"], "hit must carry provenance"

    topic_iri = f"{NS}resource/topic/webhook"
    nb = queries.kg_neighbors(rd, topic_iri, limit=10)
    print(f"== kg_neighbors(topic/webhook): {nb['count']} edges ==")
    assert nb["count"] > 0

    path = queries.kg_path(rd, hit["iri"], f"{NS}resource/doc/example/webhook-retries", max_len=3)
    assert path["reachable"], path
    print(f"== kg_path(learning -> doc): reachable in {path['length']} hop(s) ==")

    print("== kg_repo_history('test-org/fixture') ==")
    hist = queries.kg_repo_history(rd, "test-org/fixture")
    assert "results" in hist, f"expected 'results' key, got: {hist}"
    assert hist["count"] > 0, f"expected history entries for test-org/fixture, got: {hist}"
    print(f"  - {hist['count']} entries: {[x['type'] for x in hist['results']]}")

    print("== kg_repo_history('test-org/fixture', pr=4257) ==")
    hist_pr = queries.kg_repo_history(rd, "test-org/fixture", pr=4257)
    pr_results = [x for x in hist_pr["results"] if x["type"] == "PullRequest"]
    assert len(pr_results) == 1, f"expected exactly 1 PullRequest result for pr=4257, got: {pr_results}"
    assert pr_results[0]["number"] == 4257, f"expected number=4257, got: {pr_results[0]}"
    print(f"  - PR #{pr_results[0]['number']}: {pr_results[0]['title'][:60]}")

    print("== kg_repo_history('test-org/fixture', since='2099-01-01') ==")
    hist_empty = queries.kg_repo_history(rd, "test-org/fixture", since="2099-01-01")
    assert hist_empty["count"] == 0, (
        f"expected 0 results for since=2099-01-01, got: {hist_empty['results']}"
    )
    print(f"  - {hist_empty['count']} results (expected 0)")

    print("\nALL QUERY CHECKS PASSED")


if __name__ == "__main__":
    main()
