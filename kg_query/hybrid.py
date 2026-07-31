"""Hybrid search — fuse the lexical `kg_search` and the semantic
`kg_semantic_search` with Reciprocal Rank Fusion (RRF), so one query is robust to
phrasing: it finds nodes by keyword AND by meaning in a single call.

Exact identifiers (issue keys, SCREAMING_SNAKE flags) get a dominant boost so
codes lead — vector similarity blurs exact tokens. Degrades to lexical-only
(`degraded=true`) when the embeddings sidecar is absent; never raises.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import queries, semantic

RRF_K = 60
_BOOST = 1.0  # dominant vs any RRF sum (~0.03) — exact identifiers lead
# issue key (PROJ-123) or SCREAMING_SNAKE flag (GITHUB_WEBHOOK_SECRET);
# deliberately not bare acronyms like TTL / API / PR.
_EXACT_RE = re.compile(r"\b[A-Z]{2,}-\d+\b|\b[A-Z0-9]+_[A-Z0-9_]+\b")


def _rrf(rank: int) -> float:
    """Reciprocal-rank contribution for a 0-based rank."""
    return 1.0 / (RRF_K + rank + 1)


def hybrid_search(store, query: str, limit: int = 10,
                  npz_path: Path = semantic.NPZ) -> dict:
    lex = queries.kg_search(store, query, limit)
    sem = semantic.semantic_search(query, limit, npz_path=npz_path)
    degraded = "error" in sem

    fused: dict[str, dict] = {}

    def _add(item: dict, source: str, rank: int) -> None:
        iri = item.get("iri")
        if not iri:
            return
        rec = fused.get(iri)
        if rec is None:
            rec = fused[iri] = {"iri": iri, "type": None,
                                "title": item.get("title", ""),
                                "snippet": "", "score": 0.0, "matched_by": []}
        rec["score"] += _rrf(rank)
        if source not in rec["matched_by"]:
            rec["matched_by"].append(source)
        if item.get("type") and not rec["type"]:
            rec["type"] = item["type"]
        # snippet: semantic's `snippet` preferred, lexical's `fix` as fallback
        snip = item.get("snippet") or item.get("fix") or ""
        if snip and not rec["snippet"]:
            rec["snippet"] = snip

    # Process semantic first so its type+snippet win the merge (lexical only fills
    # gaps — see _add); RRF ranks are per-list, so processing order never affects
    # scores.
    for i, item in enumerate(sem.get("results", [])):
        _add(item, "semantic", i)
    for i, item in enumerate(lex.get("results", [])):
        _add(item, "lexical", i)

    # exact-identifier boost: if the query names an ID/flag and a result's iri or
    # title contains it as a WHOLE token (word-boundary anchored — so "PROJ-12" does
    # not boost "PROJ-120"), lift it above RRF ties.
    tokens = [re.escape(t.upper()) for t in _EXACT_RE.findall(query)]
    if tokens:
        pat = re.compile(r"\b(?:" + "|".join(tokens) + r")\b")
        for rec in fused.values():
            if pat.search((rec["iri"] + " " + rec["title"]).upper()):
                rec["score"] += _BOOST

    out = sorted(fused.values(), key=lambda r: r["score"], reverse=True)[:limit]
    for r in out:
        r["matched_by"] = sorted(r["matched_by"])
        r["score"] = round(r["score"], 4)
    return {"query": query, "count": len(out), "degraded": degraded, "results": out}
