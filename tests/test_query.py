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

NS = "https://example.org/kg/"

FIXTURE = f"""
@prefix kg: <{NS}onto#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<{NS}resource/graph/spine> {{
  <{NS}resource/doc/example/webhook-retries> a kg:Doc ;
    dcterms:title "Webhook retry playbook" .
  <{NS}resource/topic/webhook> a kg:Topic ; skos:prefLabel "webhook" .
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

    print("\nALL QUERY CHECKS PASSED")


if __name__ == "__main__":
    main()
