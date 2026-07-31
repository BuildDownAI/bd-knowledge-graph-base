"""Exercise the read tools on BOTH backends and check parity.

Run: ./.venv/bin/python tests/test_query.py
(rdflib always; Stardog only if reachable on localhost:5820).
"""
import json
import os

from kg_query import queries
from kg_query.store import RdflibStore, StardogStore


def _stardog_or_none():
    try:
        s = StardogStore("http://localhost:5820", "aiimplement_kg", "admin", "admin")
        s.select("SELECT * WHERE { ?s ?p ?o } LIMIT 1")
        return s
    except Exception as e:
        print(f"  (Stardog not reachable, skipping parity: {e.__class__.__name__})")
        return None


def _iris(res):
    return {r["iri"] for r in res["results"]}


def main():
    rd = RdflibStore()
    sd = _stardog_or_none()

    print("== kg_search('webhook') [rdflib] ==")
    r = queries.kg_search(rd, "webhook", limit=5)
    assert r["count"] > 0, "expected webhook lessons"
    for x in r["results"]:
        print(f"  - [{x.get('priority')}] {x['title'][:70]}  via {x['matched_topics']}")

    print("== kg_search('feature-branch') [rdflib] ==")
    r2 = queries.kg_search(rd, "feature-branch", limit=6)
    assert r2["count"] > 0

    # ---- backend parity: same result set on rdflib and Stardog ----
    if sd:
        for term in ("webhook", "feature-branch", "envelope", "comment-trigger"):
            a = _iris(queries.kg_search(rd, term, limit=25))
            b = _iris(queries.kg_search(sd, term, limit=25))
            assert a == b, f"backend mismatch for {term!r}: only-rdflib={a-b} only-stardog={b-a}"
        print("== backend parity: rdflib == stardog for all probe terms  OK ==")

    # ---- provenance of a search hit ----
    sample = queries.kg_search(rd, "webhook", limit=1)["results"][0]["iri"]
    prov = queries.kg_provenance(rd, sample)
    print("== kg_provenance(sample) ==")
    print("  " + json.dumps({k: prov[k] for k in ("types", "generated_by")}, default=str))
    assert prov["generated_by"] and prov["derived_from"], "hit must carry provenance"

    # ---- neighbors of a topic ----
    topic_iri = "https://example.org/kg/resource/topic/webhook"
    nb = queries.kg_neighbors(rd, topic_iri, limit=10)
    print(f"== kg_neighbors(topic/webhook): {nb['count']} edges ==")
    assert nb["count"] > 0

    print("\nALL QUERY CHECKS PASSED")


if __name__ == "__main__":
    main()
