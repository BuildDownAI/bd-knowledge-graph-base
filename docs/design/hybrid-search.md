# Design spec: hybrid search (`kg_hybrid_search`)

**Status:** approved design · **Date:** 2026-07-29 · builds on `semantic-search` (a tracker issue)

One search tool that is right regardless of phrasing: it runs both the lexical
`kg_search` and the semantic `kg_semantic_search`, fuses their rankings, and
boosts exact identifiers so codes still land first. Removes the "which tool do I
call?" decision — and its silent-miss failure mode (picking lexical for a
paraphrase and getting nothing).

## Tool shape

A **new, non-breaking** 5th→6th read-only MCP tool `kg_hybrid_search(query,
limit=10)`, positioned as the **preferred default**. `kg_search` (pure exact-term)
and `kg_semantic_search` (pure vector) stay as specialized escape hatches; their
behavior is unchanged.

## Fusion: Reciprocal Rank Fusion (RRF)

Run both searches (each capped at `limit`), then score every returned node by its
rank in each list:

```
score(node) = Σ_lists  1 / (RRF_K + rank_in_list)      # rank 0-based, RRF_K = 60
```

- Dedup by `iri`; a node found by **both** lists sums both contributions (so
  agreement floats to the top). RRF is rank-based, so it needs no score
  normalization between lexical (no scores) and semantic (cosine) — the standard
  reason RRF is used for heterogeneous retrievers.
- `RRF_K = 60` is the well-established default.

## Exact-identifier boost

Vector search blurs exact tokens (`a tracker issue` ≈ `a tracker issue` ≈ `a tracker issue`). So: if the
query contains an identifier — an **issue key** (`[A-Z]{2,}-\d+`) or a
**SCREAMING_SNAKE flag** (`[A-Z0-9]+_[A-Z0-9_]+`) — and a result's `iri` or
`title` contains that token (case-insensitive), add a **dominant** boost (`+1.0`,
far above any RRF sum ≈ 0.03) so exact matches lead. Common acronyms (`TTL`,
`API`, `PR`) deliberately don't match the pattern, so they don't trigger it.

## Graceful degradation

If the embeddings sidecar is absent, `semantic_search` returns its friendly error
(no exception). Hybrid detects that, returns **lexical-only** results, and sets
`degraded: true` so the caller knows the vector half was unavailable (fix: run
`python -m kg_ingest.embed`). Never raises.

## Result shape

```json
{ "query": "...", "count": N, "degraded": false,
  "results": [ { "iri": "...", "type": "Learning|Decision|Issue|null",
                 "title": "...", "snippet": "...", "score": 0.0312,
                 "matched_by": ["lexical","semantic"] } ] }
```

Fields are merged across sources: `type`/`snippet` come from the semantic hit when
present; for a lexical-only hit, `snippet` falls back to the lexical `fix` and
`type` may be `null` (lexical doesn't distinguish). `matched_by` is the sorted set
of sources that surfaced the node — useful signal for the caller.

## Components

- `kg_query/hybrid.py` — `hybrid_search(store, query, limit=10) -> dict`. Pure
  fusion over the two existing functions; imports `queries` + `semantic` only
  (fastembed stays lazy inside `semantic`). No new heavy dependency.
- `kg_query/server.py` — register `kg_hybrid_search`, delegating to
  `hybrid.hybrid_search(store(), query, limit)`. Docstring marks it the default.
- `tests/test_hybrid.py` — standalone asserts (gated on fastembed for the vector
  half): (1) a paraphrase surfaces the dedup learning via the semantic arm;
  (2) an exact issue-key query ranks that issue first (boost); (3) missing
  sidecar → `degraded: true`, lexical-only, no exception.
- README: add the tool to the Query-surface list and note it's the default.

## Non-goals

- No learned/weighted fusion or re-ranker model — RRF + a flat exact boost only.
- No change to `kg_search` / `kg_semantic_search`.
- No new dependency (numpy/fastembed already arrive via the semantic layer).
