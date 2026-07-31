"""Read-only KG query templates (backend-agnostic). MVP centerpiece: kg_search.

Each function takes a Store and returns plain dict/list structures ready for JSON
(the MCP tools are thin wrappers over these). No writes anywhere in this module.
"""
from __future__ import annotations

from collections import deque

from kg_ingest.iris import KG as _KG, NAMESPACE

from .store import Store

PREFIXES = f"""
PREFIX kg: <{_KG}>
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
"""


def _lit(s: str) -> str:
    """Escape a user string for safe embedding in a SPARQL string literal."""
    return (str(s).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", " ").replace("\r", " "))


def _iri(s: str) -> str:
    if not (s.startswith("http://") or s.startswith("https://")):
        raise ValueError(f"expected an absolute IRI, got: {s!r}")
    return f"<{s}>"


# --------------------------------------------------------------------------- #
# kg_search — topic-based (MVP)
# --------------------------------------------------------------------------- #
def kg_search(store: Store, term: str, limit: int = 10) -> dict:
    """Find learnings whose topic (tag) OR title matches `term` (substring, ci).

    Returns the transferable lesson: title, category, priority, fix snippet, and
    which topics matched. This is the "first 30 seconds" surface (recon Q1, opt.1).
    """
    t = _lit(term.lower())
    # Keep all variables at outer scope: a FILTER inside a UNION branch only sees
    # variables bound *within* that branch (SPARQL spec; Stardog enforces it,
    # rdflib does not). So we OPTIONAL the topic then decide the match with BIND.
    # Learnings AND Decisions (ADRs, plans, decision comments) are both surfaced —
    # they are all planning knowledge.
    q = PREFIXES + f"""
    SELECT DISTINCT ?learning ?title ?category ?priority ?fix ?matched WHERE {{
      VALUES ?stype {{ kg:Learning kg:Decision }}
      ?learning a ?stype ; dcterms:title ?title .
      OPTIONAL {{ ?learning kg:category ?category }}
      OPTIONAL {{ ?learning kg:priority ?priority }}
      OPTIONAL {{ ?learning kg:fix ?fix }}
      OPTIONAL {{ ?learning kg:tagged ?topic . ?topic skos:prefLabel ?tlabel }}
      BIND(
        IF(BOUND(?tlabel) && CONTAINS(LCASE(?tlabel), "{t}"), ?tlabel,
          IF(CONTAINS(LCASE(?title), "{t}"), "(title match)", "")) AS ?matched)
      FILTER(?matched != "")
    }} ORDER BY ?learning LIMIT {int(limit) * 6}
    """
    by_iri: dict[str, dict] = {}
    for r in store.select(q):
        iri = r["learning"]
        rec = by_iri.setdefault(iri, {
            "iri": iri, "title": r.get("title", ""),
            "category": r.get("category"), "priority": r.get("priority"),
            "fix": (r.get("fix") or "")[:400] or None,
            "matched_topics": [],
        })
        m = r.get("matched")
        if m and m not in rec["matched_topics"]:
            rec["matched_topics"].append(m)
    results = list(by_iri.values())[: int(limit)]
    return {"term": term, "backend": store.backend(),
            "count": len(results), "results": results}


# --------------------------------------------------------------------------- #
# kg_neighbors — 1-hop edges in/out of a node
# --------------------------------------------------------------------------- #
def kg_neighbors(store: Store, iri: str, limit: int = 30) -> dict:
    node = _iri(iri)
    q = PREFIXES + f"""
    SELECT ?dir ?pred ?other ?olabel WHERE {{
      {{ {node} ?pred ?other . BIND("out" AS ?dir) }}
      UNION
      {{ ?other ?pred {node} . BIND("in" AS ?dir) }}
      OPTIONAL {{ ?other dcterms:title ?t }}
      OPTIONAL {{ ?other skos:prefLabel ?pl }}
      OPTIONAL {{ ?other kg:path ?pa }}
      BIND(COALESCE(?t, ?pl, ?pa) AS ?olabel)
    }} ORDER BY ?dir ?pred ?other LIMIT {int(limit)}
    """
    edges = [{
        "direction": r.get("dir"),
        "predicate": r.get("pred", "").split("#")[-1].split("/")[-1],
        "predicate_iri": r.get("pred"),
        "neighbor": r.get("other"),
        "label": r.get("olabel"),
    } for r in store.select(q)]
    return {"node": iri, "backend": store.backend(),
            "count": len(edges), "edges": edges}


# --------------------------------------------------------------------------- #
# kg_provenance — why should I trust this node?
# --------------------------------------------------------------------------- #
def kg_provenance(store: Store, iri: str) -> dict:
    node = _iri(iri)
    q = PREFIXES + f"""
    SELECT ?type ?title ?derived ?dpath ?run ?pipelineVer ?model ?promptHash WHERE {{
      OPTIONAL {{ {node} rdf:type ?type }}
      OPTIONAL {{ {node} dcterms:title ?title }}
      OPTIONAL {{ {node} prov:wasDerivedFrom ?derived .
                 OPTIONAL {{ ?derived kg:path ?dpath }} }}
      OPTIONAL {{ {node} prov:wasGeneratedBy ?run .
                 OPTIONAL {{ ?run kg:pipelineVer ?pipelineVer }}
                 OPTIONAL {{ ?run kg:model ?model }}
                 OPTIONAL {{ ?run kg:promptHash ?promptHash }} }}
    }}
    """
    rows = store.select(q)
    derived, types, run = {}, set(), {}
    title = None
    for r in rows:
        if r.get("title"):
            title = r["title"]
        if r.get("type") and r["type"].startswith(NAMESPACE):
            types.add(r["type"].split("#")[-1])
        if r.get("derived"):
            derived[r["derived"]] = r.get("dpath")
        if r.get("run"):
            run = {"run": r["run"], "pipeline_ver": r.get("pipelineVer"),
                   "model": r.get("model"), "prompt_hash": r.get("promptHash")}
    return {
        "node": iri, "backend": store.backend(), "title": title,
        "types": sorted(types),
        "derived_from": [{"iri": k, "path": v} for k, v in derived.items()],
        "generated_by": run or None,
    }


# --------------------------------------------------------------------------- #
# kg_path — bounded BFS between two nodes over all edges (both directions)
# --------------------------------------------------------------------------- #
def kg_path(store: Store, from_iri: str, to_iri: str, max_len: int = 4) -> dict:
    start, goal = from_iri, to_iri
    seen = {start}
    # queue of (node, path) where path = [(predicate, node), ...]
    q: deque[tuple[str, list]] = deque([(start, [])])
    while q:
        node, path = q.popleft()
        if node == goal and path:
            return {"from": from_iri, "to": to_iri, "backend": store.backend(),
                    "reachable": True, "length": len(path), "path": path}
        if len(path) >= max_len:
            continue
        for e in kg_neighbors(store, node, limit=200)["edges"]:
            nb = e["neighbor"]
            if not nb.startswith("http") or nb in seen:
                continue
            seen.add(nb)
            q.append((nb, path + [{"predicate": e["predicate"],
                                   "direction": e["direction"], "node": nb}]))
    return {"from": from_iri, "to": to_iri, "backend": store.backend(),
            "reachable": False, "length": None, "path": []}
