"""Search inside Stardog Run (KG_BACKEND=stardog_run): Stardog's semantic index replaces
the embeddings file, and hybrid search's RRF merge runs as one SPARQL query. Same result
shapes as semantic.py and hybrid.py."""
from __future__ import annotations

import re

from . import hybrid
from .queries import PREFIXES, _lit, _search_match

MODEL = "all-MiniLM-L6-v2"  # Stardog's default search.semantic.model

_FTS = "PREFIX fts: <tag:stardog:api:search:>\n"


def _semantic(query: str, limit: int, iri: str, score: str) -> str:
    """Pattern binding `iri` and `score` for the top `limit` cards by meaning."""
    lit = f"?{iri}_l"
    return (f'SERVICE fts:textMatch {{ [] fts:query "{_lit(query)}" ; fts:semantic true ; '
            f"fts:limit {int(limit)} ; fts:result {lit} ; fts:score ?{score} }} ?{iri} kg:cardText {lit} .")


def _lexical(query: str, limit: int, iri: str) -> str:
    """kg_search's match as a subquery: the first `limit` matching nodes by IRI."""
    return f"""{{ SELECT DISTINCT ?{iri} WHERE {{
      {_search_match(iri, _lit(query.lower()), f"{iri}_")}
    }} ORDER BY ?{iri} LIMIT {int(limit)} }}"""


def _card_fields(iri: str) -> str:
    return f"""OPTIONAL {{ ?{iri} kg:cardTitle ?ctitle }} OPTIONAL {{ ?{iri} dcterms:title ?dtitle }}
      OPTIONAL {{ ?{iri} kg:cardType ?ctype }}
      OPTIONAL {{ ?{iri} a ?rtype . FILTER(STRSTARTS(STR(?rtype), STR(kg:))) }}
      OPTIONAL {{ ?{iri} kg:cardSnippet ?csnip }} OPTIONAL {{ ?{iri} kg:fix ?fix }}"""


def semantic_search(store, query: str, limit: int = 10) -> dict:
    rows = store.select(PREFIXES + _FTS + f"""
    SELECT ?iri (MAX(?score) AS ?s) (SAMPLE(?ctitle) AS ?title) (SAMPLE(?ctype) AS ?type)
           (SAMPLE(?csnip) AS ?snippet) WHERE {{
      {_semantic(query, limit, "iri", "score")}
      OPTIONAL {{ ?iri kg:cardTitle ?ctitle }} OPTIONAL {{ ?iri kg:cardType ?ctype }}
      OPTIONAL {{ ?iri kg:cardSnippet ?csnip }}
    }} GROUP BY ?iri ORDER BY DESC(?s) ?iri""")
    results = [{"iri": r["iri"], "type": r.get("type", ""), "title": r.get("title", ""),
                "snippet": r.get("snippet", ""), "score": round(float(r["s"]), 4)} for r in rows]
    return {"query": query, "model": MODEL, "count": len(results), "results": results}


def hybrid_search(store, query: str, limit: int = 10) -> dict:
    """hybrid.py's fusion in one query: RRF (k=60) over each leg's 0-based rank, where a
    rank is a self-join count, plus the exact-identifier boost."""
    k = hybrid.RRF_K
    legs = f"""
      {{ SELECT ?iri ("semantic" AS ?leg) (1.0 / ({k} + COUNT(?o)) AS ?c) WHERE {{
          {_semantic(query, limit, "iri", "s")}
          {_semantic(query, limit, "o", "os")}
          FILTER(?os >= ?s) }} GROUP BY ?iri }}
      UNION
      {{ SELECT ?iri ("lexical" AS ?leg) (1.0 / ({k} + COUNT(DISTINCT ?o)) AS ?c) WHERE {{
          {_lexical(query, limit, "iri")}
          {_lexical(query, limit, "o")}
          FILTER(STR(?o) <= STR(?iri)) }} GROUP BY ?iri }}"""
    tokens = [re.escape(t.upper()) for t in hybrid._EXACT_RE.findall(query)]
    boost = ""
    if tokens:
        pattern = _lit(r"\b(?:" + "|".join(tokens) + r")\b")
        boost = f"""BIND(IF(REGEX(UCASE(CONCAT(STR(?iri), " ", COALESCE(?title, ""))), "{pattern}"),
                            {hybrid._BOOST}, 0.0) AS ?boost)"""
    rows = store.select(PREFIXES + _FTS + f"""
    SELECT ?iri ?score ?title ?type ?snippet ?matched WHERE {{
      {{ SELECT ?iri ?rrf ?matched (SAMPLE(COALESCE(?ctitle, ?dtitle)) AS ?title)
                (SAMPLE(COALESCE(?ctype, STRAFTER(STR(?rtype), "#"))) AS ?type)
                (SAMPLE(COALESCE(?csnip, SUBSTR(?fix, 1, 400), "")) AS ?snippet) WHERE {{
          {{ SELECT ?iri (SUM(?c) AS ?rrf) (GROUP_CONCAT(DISTINCT ?leg; separator=",") AS ?matched)
             WHERE {{ {legs} }} GROUP BY ?iri }}
          {_card_fields("iri")}
      }} GROUP BY ?iri ?rrf ?matched }}
      {boost}
      BIND(?rrf + {"?boost" if tokens else "0.0"} AS ?score)
    }} ORDER BY DESC(?score) ?iri LIMIT {int(limit)}""")
    results = [{"iri": r["iri"], "type": r.get("type") or None, "title": r.get("title", ""),
                "snippet": r.get("snippet", ""), "score": round(float(r["score"]), 4),
                "matched_by": sorted(r["matched"].split(","))} for r in rows]
    return {"query": query, "count": len(results), "degraded": False, "results": results}
