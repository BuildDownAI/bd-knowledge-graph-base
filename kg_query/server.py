"""Read-only MCP server for a project knowledge graph built from this template.

Exposes four read tools over the store abstraction (rdflib in-process by default,
or Stardog via KG_BACKEND=stardog). NO write tools — Phase 1 is read-only.

Run (stdio):  python -m kg_query.server
Backend:      KG_BACKEND=rdflib|stardog  (+ KG_STARDOG_* / KG_TRIG)
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import queries, semantic, hybrid
from .store import Store, get_store

mcp = FastMCP(os.environ.get("KG_SERVER_NAME", "kg"))

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = get_store()
    return _store


@mcp.tool()
def kg_search(term: str, limit: int = 10) -> dict:
    """Surface prior LEARNINGS and DECISIONS relevant to a term — the first thing
    to call when starting work in an unfamiliar area or before writing code that
    touches a known-tricky surface of the source project. Matches the
    term against topics/tags and titles across solutions docs, ADRs, plans, and
    Linear-issue learnings. Returns each item's title, category, priority, a fix
    snippet, and which topics matched. Read-only.
    """
    return queries.kg_search(store(), term, limit)


@mcp.tool()
def kg_semantic_search(query: str, limit: int = 10) -> dict:
    """Semantic (vector) search over prior LEARNINGS, DECISIONS, and ISSUES — use
    for paraphrased or conceptual queries that the exact-term `kg_search` misses
    (e.g. "how do we stop re-dispatching a torn-down issue"). Returns ranked index
    cards {iri, type, title, snippet, score}; follow up with kg_neighbors /
    kg_provenance for full content. Requires the embeddings sidecar (build with
    `python -m kg_ingest.embed`); returns a friendly error if absent. Read-only.
    """
    return semantic.semantic_search(query, limit)


@mcp.tool()
def kg_hybrid_search(query: str, limit: int = 10) -> dict:
    """PREFERRED default search over prior LEARNINGS, DECISIONS, and ISSUES. Fuses
    the exact-term `kg_search` and the semantic `kg_semantic_search` with
    reciprocal rank fusion, so it is robust to phrasing — finds nodes by keyword
    AND by meaning in one call. Exact identifiers (issue keys like PROJ-123,
    SCREAMING_SNAKE flags) are boosted so codes lead. Returns {iri, type, title,
    snippet, score, matched_by}; sets `degraded: true` and falls back to
    lexical-only if the vector index isn't built (`python -m kg_ingest.embed`).
    Prefer this over `kg_search`/`kg_semantic_search` unless you specifically need
    exact-only or vector-only. Read-only.
    """
    return hybrid.hybrid_search(store(), query, limit)


@mcp.tool()
def kg_neighbors(iri: str, limit: int = 30) -> dict:
    """List the 1-hop graph neighbors (incoming + outgoing edges) of a node IRI —
    e.g. the issues/PRs/files/topics a learning links to, or the learnings under
    a topic. Use after kg_search to expand context around a hit. Read-only.
    """
    return queries.kg_neighbors(store(), iri, limit)


@mcp.tool()
def kg_provenance(iri: str) -> dict:
    """Show WHY a node should be trusted: what spine artifacts it was derived from
    (source docs/PRs) and which extraction run generated it (pipeline version,
    model, prompt hash). Use to audit or cite a learning before acting on it.
    Read-only.
    """
    return queries.kg_provenance(store(), iri)


@mcp.tool()
def kg_path(from_iri: str, to_iri: str, max_len: int = 4) -> dict:
    """Find whether/how two nodes are connected (bounded BFS over all edges, both
    directions) — e.g. does this learning trace to that PR or topic. Returns the
    connecting path if one exists within max_len hops. Read-only.
    """
    return queries.kg_path(store(), from_iri, to_iri, max_len)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
