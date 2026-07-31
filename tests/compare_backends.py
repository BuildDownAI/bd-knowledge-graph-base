"""1:1 backend comparison — lexical kg_search (+ read tools) on rdflib vs Stardog.

Runs identical queries on both stores and reports: result-set parity (do they
return the SAME learnings?) and per-backend latency. This is the open-source vs
Stardog head-to-head for the topic-based MVP.

Run: PYTHONPATH=. ./.venv/bin/python tests/compare_backends.py
"""
import statistics
import time

from kg_query import queries
from kg_query.store import RdflibStore, StardogStore

TERMS = ["webhook", "feature-branch", "envelope", "comment-trigger",
         "gap-fill", "dispatch", "runner", "bedrock",
         "sync", "url-encoding"]


def _time(fn, n=5):
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        out = fn()
        ts.append((time.perf_counter() - t) * 1000)
    return out, statistics.median(ts)


def _iris(res):
    return {r["iri"] for r in res["results"]}


def main():
    rd = RdflibStore()
    try:
        sd = StardogStore("http://localhost:5820", "aiimplement_kg", "admin", "admin")
        sd.select("SELECT * WHERE {?s ?p ?o} LIMIT 1")
    except Exception as e:
        print(f"Stardog unreachable ({e.__class__.__name__}); cannot compare.")
        return

    print(f"{'term':<16} {'n(rdflib)':>9} {'n(stardog)':>10} {'set 1:1':>8} "
          f"{'rdflib ms':>10} {'stardog ms':>11}")
    print("-" * 72)
    all_match = True
    rd_tot, sd_tot = [], []
    for term in TERMS:
        r_res, r_ms = _time(lambda: queries.kg_search(rd, term, limit=25))
        s_res, s_ms = _time(lambda: queries.kg_search(sd, term, limit=25))
        match = _iris(r_res) == _iris(s_res)
        all_match &= match
        rd_tot.append(r_ms); sd_tot.append(s_ms)
        print(f"{term:<16} {r_res['count']:>9} {s_res['count']:>10} "
              f"{'YES' if match else 'NO!':>8} {r_ms:>10.1f} {s_ms:>11.1f}")

    print("-" * 72)
    print(f"{'MEDIAN':<16} {'':>9} {'':>10} {'':>8} "
          f"{statistics.median(rd_tot):>10.1f} {statistics.median(sd_tot):>11.1f}")
    print()

    # spot-check kg_neighbors + kg_provenance parity on one sample node
    sample = queries.kg_search(rd, "webhook", limit=1)["results"][0]["iri"]
    n_rd = {(e["predicate"], e["neighbor"]) for e in queries.kg_neighbors(rd, sample)["edges"]}
    n_sd = {(e["predicate"], e["neighbor"]) for e in queries.kg_neighbors(sd, sample)["edges"]}
    prov_rd = queries.kg_provenance(rd, sample)
    prov_sd = queries.kg_provenance(sd, sample)
    prov_match = (prov_rd["generated_by"] == prov_sd["generated_by"] and
                  {d["iri"] for d in prov_rd["derived_from"]} ==
                  {d["iri"] for d in prov_sd["derived_from"]})
    print(f"kg_neighbors 1:1 on sample node:  {'YES' if n_rd == n_sd else 'NO'}")
    print(f"kg_provenance 1:1 on sample node: {'YES' if prov_match else 'NO'}")

    print()
    print("RESULT:", "kg_search is 1:1 identical across backends"
          if all_match else "MISMATCH — investigate")

    # show one side-by-side so the match is visible, not just asserted
    print("\n--- side-by-side: kg_search('rate limit') ---")
    for label, res in (("rdflib ", queries.kg_search(rd, "rate limit", limit=5)),
                       ("stardog", queries.kg_search(sd, "rate limit", limit=5))):
        print(f"  [{label}]")
        for x in sorted(res["results"], key=lambda r: r["iri"]):
            print(f"     {x['title'][:66]}")


if __name__ == "__main__":
    main()
