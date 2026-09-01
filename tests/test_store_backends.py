# Backend parity + benchmark: RdflibStore vs OxigraphStore (KGB-13 spike).
from __future__ import annotations

import statistics
import tempfile
import time
from pathlib import Path

from kg_query import queries
from kg_query.store import OxigraphStore, RdflibStore
from kg_ingest.iris import NAMESPACE as NS

# ── Fixture ──────────────────────────────────────────────────────────────────
# Mirrors test_query.py so both suites exercise the same graph shape.
FIXTURE_1X = f"""
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

# 10× corpus: duplicate with offset IRI segments so each copy is independent.
_TRIG_10X = "\n".join(
    [FIXTURE_1X]
    + [
        FIXTURE_1X.replace(f"{NS}resource/", f"{NS}resource/x{i}/")
        for i in range(1, 10)
    ]
)


def _write_fixture(content: str) -> Path:
    d = tempfile.mkdtemp()
    p = Path(d, "g.trig")
    p.write_text(content)
    return p


def _rss_kb() -> int:
    """RSS in KB via /proc/self/status; falls back to resource.getrusage."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except Exception:
        pass
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _measure_load(store_cls, path: Path):
    """Return (store, load_seconds, rss_delta_kb).

    Memory is measured as RSS delta via /proc/self/status so that native
    Rust/RocksDB allocations (invisible to tracemalloc) are included.
    """
    rss_before = _rss_kb()
    t0 = time.perf_counter()
    store = store_cls(path)
    elapsed = time.perf_counter() - t0
    rss_delta = max(0, _rss_kb() - rss_before)
    return store, elapsed, rss_delta


def _median_latency_ms(store, term: str = "webhook", runs: int = 10) -> float:
    lats = []
    for _ in range(runs):
        t0 = time.perf_counter()
        queries.kg_search(store, term, limit=10)
        lats.append((time.perf_counter() - t0) * 1000)
    return statistics.median(lats)


# ── Assertion harness ─────────────────────────────────────────────────────────
GAPS: list[str] = []


def _assert(condition: bool, msg: str) -> None:
    if not condition:
        GAPS.append(msg)
        print(f"  GAP: {msg}")


def main() -> None:
    try:
        import pyoxigraph  # noqa: F401
    except ImportError:
        print("pyoxigraph not installed — install with: pip install -r requirements-oxigraph.txt")
        print("Skipping OxigraphStore tests.")
        raise SystemExit(0)

    print("=== Backend parity + benchmark: RdflibStore vs OxigraphStore ===\n")

    # ── Load & benchmark at 1× ────────────────────────────────────────────────
    p1 = _write_fixture(FIXTURE_1X)
    print("--- 1× corpus ---")
    rd1, rd1_load, rd1_mem = _measure_load(RdflibStore, p1)
    ox1, ox1_load, ox1_mem = _measure_load(OxigraphStore, p1)

    rd1_lat = _median_latency_ms(rd1)
    ox1_lat = _median_latency_ms(ox1)

    print(f"  RdflibStore    load {rd1_load*1000:6.1f} ms  |  median kg_search {rd1_lat:6.2f} ms  |  RSS delta {rd1_mem:5} KB")
    print(f"  OxigraphStore  load {ox1_load*1000:6.1f} ms  |  median kg_search {ox1_lat:6.2f} ms  |  RSS delta {ox1_mem:5} KB")

    # ── Load & benchmark at 10× ───────────────────────────────────────────────
    p10 = _write_fixture(_TRIG_10X)
    print("\n--- 10× corpus ---")
    rd10, rd10_load, rd10_mem = _measure_load(RdflibStore, p10)
    ox10, ox10_load, ox10_mem = _measure_load(OxigraphStore, p10)

    rd10_lat = _median_latency_ms(rd10)
    ox10_lat = _median_latency_ms(ox10)

    print(f"  RdflibStore    load {rd10_load*1000:6.1f} ms  |  median kg_search {rd10_lat:6.2f} ms  |  RSS delta {rd10_mem:5} KB")
    print(f"  OxigraphStore  load {ox10_load*1000:6.1f} ms  |  median kg_search {ox10_lat:6.2f} ms  |  RSS delta {ox10_mem:5} KB")

    # ── Default-graph smoke check ─────────────────────────────────────────────
    print("\n--- Smoke check ---")
    smoke = ox1.select("SELECT * WHERE { ?s ?p ?o } LIMIT 1")
    _assert(len(smoke) == 1, "OxigraphStore default graph is empty — union-graph flattening failed")
    print(f"  OxigraphStore default graph populated: {'OK' if len(smoke) == 1 else 'EMPTY'}")

    # ── Parity assertions at 1× ───────────────────────────────────────────────
    print("\n--- Parity assertions ---")

    # kg_search
    rd_search = queries.kg_search(rd1, "webhook", limit=10)
    ox_search = queries.kg_search(ox1, "webhook", limit=10)
    rd_iris = {h["iri"] for h in rd_search["results"]}
    ox_iris = {h["iri"] for h in ox_search["results"]}
    _assert(rd_iris == ox_iris, f"kg_search IRI mismatch — rdflib={rd_iris} ox={ox_iris}")
    _assert(rd_search["count"] > 0, "kg_search returned no results on rdflib (fixture problem)")
    _assert(ox_search["count"] > 0, "kg_search returned no results on oxigraph (flattening or SPARQL issue)")
    print(f"  kg_search 'webhook': rdflib={len(rd_iris)} hits, oxigraph={len(ox_iris)} hits — {'MATCH' if rd_iris == ox_iris else 'MISMATCH'}")

    # kg_provenance (for each hit rdflib found)
    for hit in rd_search["results"]:
        iri = hit["iri"]
        rd_prov = queries.kg_provenance(rd1, iri)
        ox_prov = queries.kg_provenance(ox1, iri)
        types_match = set(rd_prov["types"]) == set(ox_prov["types"])
        _assert(types_match, f"kg_provenance types mismatch for {iri}: rdflib={rd_prov['types']} ox={ox_prov['types']}")
        rd_derived = {d["iri"] for d in rd_prov["derived_from"]}
        ox_derived = {d["iri"] for d in ox_prov["derived_from"]}
        derived_match = rd_derived == ox_derived
        _assert(derived_match, f"kg_provenance derived_from mismatch for {iri}: rdflib={rd_derived} ox={ox_derived}")
        print(f"  kg_provenance({iri.rsplit('/', 1)[-1]}): types={'MATCH' if types_match else 'MISMATCH'}  derived={'MATCH' if derived_match else 'MISMATCH'}")

    # kg_neighbors
    topic_iri = f"{NS}resource/topic/webhook"
    rd_nb = queries.kg_neighbors(rd1, topic_iri, limit=30)
    ox_nb = queries.kg_neighbors(ox1, topic_iri, limit=30)
    rd_edges = {(e["direction"], e["predicate"], e["neighbor"]) for e in rd_nb["edges"]}
    ox_edges = {(e["direction"], e["predicate"], e["neighbor"]) for e in ox_nb["edges"]}
    _assert(rd_edges == ox_edges, f"kg_neighbors edge-set mismatch: rdflib={rd_edges} ox={ox_edges}")
    print(f"  kg_neighbors(topic/webhook): rdflib={rd_nb['count']}, oxigraph={ox_nb['count']} — {'MATCH' if rd_edges == ox_edges else 'MISMATCH'}")

    # kg_path
    if rd_iris:
        hit_iri = next(iter(rd_iris))
        doc_iri = f"{NS}resource/doc/example/webhook-retries"
        rd_path = queries.kg_path(rd1, hit_iri, doc_iri, max_len=4)
        ox_path = queries.kg_path(ox1, hit_iri, doc_iri, max_len=4)
        reach_match = rd_path["reachable"] == ox_path["reachable"]
        _assert(reach_match, f"kg_path reachability mismatch: rdflib={rd_path['reachable']} ox={ox_path['reachable']}")
        print(f"  kg_path(learning→doc): rdflib reachable={rd_path['reachable']}, oxigraph reachable={ox_path['reachable']} — {'MATCH' if reach_match else 'MISMATCH'}")

    # ── Known gaps (per ADR 011 / issue acceptance criteria) ──────────────────
    print("\n--- Known gaps ---")
    print("  GAP [pyshacl]: Validation stays on rdflib at ingest time.")
    print("    pyshacl requires an rdflib.Graph; OxigraphStore is serving-side only")
    print("    and does not run SHACL. Acceptable per ADR 011: ingest is local,")
    print("    serving is what scales.")

    # ── Final summary ─────────────────────────────────────────────────────────
    print("\n=== Numbers summary ===")
    print("  (RSS delta via /proc/self/status — includes Rust/RocksDB native heap)")
    print(f"  Corpus  | Backend       |  Load (ms) | Query (ms) | RSS delta (KB)")
    print(f"  --------+---------------+------------+------------+---------------")
    print(f"  1×      | rdflib        | {rd1_load*1000:10.1f} | {rd1_lat:10.2f} | {rd1_mem:13}")
    print(f"  1×      | oxigraph      | {ox1_load*1000:10.1f} | {ox1_lat:10.2f} | {ox1_mem:13}")
    print(f"  10×     | rdflib        | {rd10_load*1000:10.1f} | {rd10_lat:10.2f} | {rd10_mem:13}")
    print(f"  10×     | oxigraph      | {ox10_load*1000:10.1f} | {ox10_lat:10.2f} | {ox10_mem:13}")

    if GAPS:
        print(f"\nGAPS ({len(GAPS)}):")
        for g in GAPS:
            print(f"  - {g}")
        raise SystemExit(1)
    else:
        print("\nALL BACKEND PARITY CHECKS PASSED")


if __name__ == "__main__":
    main()
