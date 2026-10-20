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
    """Find learnings, decisions, notes, PRs, and repos matching `term` (substring, ci).

    Matches topic/tag labels and titles across semantic nodes (Learning, Decision,
    ImplementationNote, PlanningNote), PullRequest titles, and Repo names.
    """
    t = _lit(term.lower())
    # Keep all variables at outer scope: a FILTER inside a UNION branch only sees
    # variables bound *within* that branch (SPARQL spec; Stardog enforces it,
    # rdflib does not). So we OPTIONAL the topic then decide the match with BIND.
    q = PREFIXES + f"""
    SELECT DISTINCT ?learning ?type ?title ?category ?priority ?fix ?matched WHERE {{
      VALUES ?type {{ kg:Learning kg:Decision kg:ImplementationNote kg:PlanningNote kg:PullRequest kg:Repo }}
      ?learning a ?type ; dcterms:title ?title .
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
        type_iri = r.get("type") or ""
        rec = by_iri.setdefault(iri, {
            "iri": iri, "title": r.get("title", ""),
            "type": type_iri.split("#")[-1] if type_iri else None,
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
def _dt(s: str) -> str:
    """Normalize a date/datetime string to xsd:dateTime format for SPARQL FILTERs."""
    if "T" not in s:
        return s + "T00:00:00Z"
    return s


def kg_repo_history(store: Store, repo: str, branch: str | None = None,
                    since: str | None = None, until: str | None = None,
                    pr: int | None = None, kind: str | None = None,
                    limit: int = 100) -> dict:
    """Return commits and/or merged PRs for a repo with optional filters.

    repo:   owner/name slug (required)
    branch: filter commits by repo historyBranch; filter PRs by baseRefName
    since:  ISO date/datetime lower bound (inclusive) on commit date / PR mergedAt
    until:  ISO date/datetime upper bound (inclusive) on commit date / PR mergedAt
    pr:     filter to a specific PR number
    kind:   "commit", "pr", or None for both
    limit:  max results returned (default 100)
    """
    r = _lit(repo)
    results: list[dict] = []

    if kind in (None, "commit"):
        branch_f = f'\n      ?repo kg:historyBranch "{_lit(branch)}" .' if branch else ""
        date_f = ""
        if since:
            date_f += f'\n      FILTER(?date >= "{_lit(_dt(since))}"^^xsd:dateTime)'
        if until:
            date_f += f'\n      FILTER(?date <= "{_lit(_dt(until))}"^^xsd:dateTime)'
        q = PREFIXES + f"""
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?node ?title ?sha ?date WHERE {{
          ?node a kg:Commit ; kg:partOf ?repo ; dcterms:created ?date .
          ?repo kg:sourceId "{r}" .{branch_f}
          OPTIONAL {{ ?node dcterms:title ?title }}
          OPTIONAL {{ ?node kg:sourceId ?sha }}{date_f}
        }} ORDER BY DESC(?date) LIMIT {int(limit)}
        """
        for row in store.select(q):
            results.append({
                "type": "Commit", "iri": row.get("node"),
                "title": row.get("title", ""), "sha": row.get("sha"),
                "date": row.get("date"),
            })

    if kind in (None, "pr"):
        pr_f = f'\n      FILTER(?number = {int(pr)})' if pr is not None else ""
        branch_f = f'\n      FILTER(?baseRef = "{_lit(branch)}")' if branch else ""
        date_f = ""
        if since:
            date_f += f'\n      FILTER(BOUND(?date) && ?date >= "{_lit(_dt(since))}"^^xsd:dateTime)'
        if until:
            date_f += f'\n      FILTER(BOUND(?date) && ?date <= "{_lit(_dt(until))}"^^xsd:dateTime)'
        q = PREFIXES + f"""
        PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
        SELECT ?node ?title ?number ?state ?date ?baseRef WHERE {{
          ?node a kg:PullRequest ; kg:partOf ?repo .
          ?repo kg:sourceId "{r}" .
          OPTIONAL {{ ?node dcterms:title ?title }}
          OPTIONAL {{ ?node kg:number ?number }}
          OPTIONAL {{ ?node kg:state ?state }}
          OPTIONAL {{ ?node kg:mergedAt ?date }}
          OPTIONAL {{ ?node kg:baseRefName ?baseRef }}{pr_f}{branch_f}{date_f}
        }} ORDER BY DESC(?number) LIMIT {int(limit)}
        """
        for row in store.select(q):
            num = row.get("number")
            results.append({
                "type": "PullRequest", "iri": row.get("node"),
                "title": row.get("title", ""),
                "number": int(num) if num is not None else None,
                "state": row.get("state"), "date": row.get("date"),
                "base_ref": row.get("baseRef"),
            })

    results.sort(key=lambda x: x.get("date") or "", reverse=True)
    results = results[:int(limit)]
    return {"repo": repo, "backend": store.backend(),
            "count": len(results), "results": results}


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
