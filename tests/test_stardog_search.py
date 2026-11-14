"""Tests for kg_query.stardog_search (KGB-51).

Uses a fake Store that records SPARQL text and returns fixture rows.
No test calls the network.

Run: PYTHONPATH=. ./.venv/bin/python -m pytest tests/test_stardog_search.py -v
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from kg_query import stardog_search
from kg_query.queries import _lit


# ---------------------------------------------------------------------------
# Fake Store
# ---------------------------------------------------------------------------

class FakeStore:
    """Records all SPARQL sent to .select() and returns canned rows."""

    def __init__(self, rows: list[dict] | None = None):
        self._rows = rows or []
        self.queries: list[str] = []

    def select(self, sparql: str) -> list[dict]:
        self.queries.append(sparql)
        return self._rows

    def backend(self) -> str:
        return "FakeStore"


# ---------------------------------------------------------------------------
# _lit escaping
# ---------------------------------------------------------------------------

def test_lit_escapes_double_quote():
    result = _lit('say "hello"')
    assert '\\"' in result


def test_lit_escapes_backslash():
    result = _lit('path\\to\\file')
    assert '\\\\' in result


def test_lit_escapes_both():
    result = _lit('"\\')
    assert '\\"' in result
    assert '\\\\' in result


# ---------------------------------------------------------------------------
# semantic_search — result shape
# ---------------------------------------------------------------------------

SEMANTIC_ROWS = [
    {"iri": "http://kg/r/1", "s": "0.9500", "title": "Card one", "type": "Learning", "snippet": "Snip one"},
    {"iri": "http://kg/r/2", "s": "0.8000", "title": "Card two", "type": "Decision", "snippet": ""},
]


def test_semantic_search_top_level_keys():
    store = FakeStore(SEMANTIC_ROWS)
    result = stardog_search.semantic_search(store, "test query", limit=5)
    assert set(result.keys()) == {"query", "model", "count", "results"}


def test_semantic_search_query_and_model():
    store = FakeStore(SEMANTIC_ROWS)
    result = stardog_search.semantic_search(store, "test query", limit=5)
    assert result["query"] == "test query"
    assert result["model"] == stardog_search.MODEL


def test_semantic_search_count():
    store = FakeStore(SEMANTIC_ROWS)
    result = stardog_search.semantic_search(store, "q", limit=5)
    assert result["count"] == len(SEMANTIC_ROWS)


def test_semantic_search_result_item_keys():
    store = FakeStore(SEMANTIC_ROWS)
    result = stardog_search.semantic_search(store, "q", limit=5)
    for r in result["results"]:
        assert set(r.keys()) == {"iri", "type", "title", "snippet", "score"}


def test_semantic_search_score_rounded():
    store = FakeStore(SEMANTIC_ROWS)
    result = stardog_search.semantic_search(store, "q", limit=5)
    for r in result["results"]:
        assert r["score"] == round(r["score"], 4)


def test_semantic_search_includes_fts_service():
    store = FakeStore(SEMANTIC_ROWS)
    stardog_search.semantic_search(store, "my query", limit=3)
    q = store.queries[0]
    assert "fts:textMatch" in q
    assert "fts:semantic true" in q


def test_semantic_search_query_text_in_sparql():
    store = FakeStore([])
    stardog_search.semantic_search(store, "my query", limit=3)
    assert "my query" in store.queries[0]


def test_semantic_search_limit_in_sparql():
    store = FakeStore([])
    stardog_search.semantic_search(store, "q", limit=7)
    assert "7" in store.queries[0]


def test_semantic_search_empty_rows():
    store = FakeStore([])
    result = stardog_search.semantic_search(store, "q", limit=5)
    assert result["count"] == 0
    assert result["results"] == []


# ---------------------------------------------------------------------------
# hybrid_search — result shape
# ---------------------------------------------------------------------------

HYBRID_ROWS = [
    {
        "iri": "http://kg/r/1",
        "score": "0.0323",
        "title": "Card one",
        "type": "Learning",
        "snippet": "Snip one",
        "matched": "lexical,semantic",
    },
]


def test_hybrid_search_top_level_keys():
    store = FakeStore(HYBRID_ROWS)
    result = stardog_search.hybrid_search(store, "test query", limit=5)
    assert set(result.keys()) == {"query", "count", "degraded", "results"}


def test_hybrid_search_degraded_is_false():
    store = FakeStore(HYBRID_ROWS)
    result = stardog_search.hybrid_search(store, "q", limit=5)
    assert result["degraded"] is False


def test_hybrid_search_query():
    store = FakeStore(HYBRID_ROWS)
    result = stardog_search.hybrid_search(store, "test query", limit=5)
    assert result["query"] == "test query"


def test_hybrid_search_count():
    store = FakeStore(HYBRID_ROWS)
    result = stardog_search.hybrid_search(store, "q", limit=5)
    assert result["count"] == len(HYBRID_ROWS)


def test_hybrid_search_result_item_keys():
    store = FakeStore(HYBRID_ROWS)
    result = stardog_search.hybrid_search(store, "q", limit=5)
    for r in result["results"]:
        assert set(r.keys()) == {"iri", "type", "title", "snippet", "score", "matched_by"}


def test_hybrid_search_matched_by_sorted():
    store = FakeStore(HYBRID_ROWS)
    result = stardog_search.hybrid_search(store, "q", limit=5)
    r = result["results"][0]
    assert r["matched_by"] == sorted(r["matched_by"])


def test_hybrid_search_score_rounded():
    store = FakeStore(HYBRID_ROWS)
    result = stardog_search.hybrid_search(store, "q", limit=5)
    for r in result["results"]:
        assert r["score"] == round(r["score"], 4)


def test_hybrid_search_empty_rows():
    store = FakeStore([])
    result = stardog_search.hybrid_search(store, "q", limit=5)
    assert result["count"] == 0
    assert result["results"] == []


# ---------------------------------------------------------------------------
# boost / identifier token detection
# ---------------------------------------------------------------------------

def test_identifier_token_produces_regex_boost():
    store = FakeStore([])
    stardog_search.hybrid_search(store, "AII-256 search", limit=5)
    q = store.queries[0]
    assert "REGEX" in q
    assert "UCASE" in q


def test_no_identifier_token_no_regex():
    store = FakeStore([])
    stardog_search.hybrid_search(store, "plain prose query without codes", limit=5)
    q = store.queries[0]
    assert "REGEX" not in q
    assert "UCASE" not in q


def test_screaming_snake_produces_boost():
    store = FakeStore([])
    stardog_search.hybrid_search(store, "set GITHUB_WEBHOOK_SECRET to x", limit=5)
    q = store.queries[0]
    assert "REGEX" in q


def test_query_with_double_quote_escaped_in_sparql():
    store = FakeStore([])
    stardog_search.hybrid_search(store, 'query with "quotes"', limit=5)
    q = store.queries[0]
    # The literal in SPARQL must have escaped quotes; a bare " would break the string
    assert '\\"' in q


def test_query_with_backslash_escaped_in_sparql():
    store = FakeStore([])
    stardog_search.hybrid_search(store, "path\\to\\file", limit=5)
    q = store.queries[0]
    assert '\\\\' in q


# ---------------------------------------------------------------------------
# kg_neighbors excludes kg:card* predicates
# ---------------------------------------------------------------------------

def test_kg_neighbors_excludes_card_predicates():
    """kg_neighbors does not return edges with kg:card* predicates."""
    from kg_ingest.iris import NAMESPACE as NS
    from kg_query import queries
    from kg_query.store import RdflibStore

    trig = f"""
    @prefix kg: <{NS}onto#> .
    @prefix dcterms: <http://purl.org/dc/terms/> .
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
    <{NS}resource/graph/test> {{
      <{NS}resource/node/1> a kg:Learning ;
        dcterms:title "Node 1" ;
        kg:cardText "some card text" ;
        kg:cardTitle "Card title" ;
        kg:tagged <{NS}resource/topic/x> .
      <{NS}resource/topic/x> a kg:Topic ;
        skos:prefLabel "topic-x" .
    }}
    """
    d = tempfile.mkdtemp()
    p = Path(d, "g.trig")
    p.write_text(trig)
    store = RdflibStore(p)

    result = queries.kg_neighbors(store, f"{NS}resource/node/1", limit=20)
    pred_iris = [e["predicate_iri"] for e in result["edges"]]

    assert not any(p and "card" in p for p in pred_iris), \
        f"Found kg:card* predicates in neighbors: {pred_iris}"
    assert any(e["predicate"] == "tagged" for e in result["edges"]), \
        "Expected 'tagged' edge to still appear"


# ---------------------------------------------------------------------------
# server.py dispatch: StardogRunStore -> stardog_search, else original
# ---------------------------------------------------------------------------

def test_server_semantic_search_dispatches_for_stardog_run(monkeypatch):
    """kg_semantic_search calls stardog_search.semantic_search for StardogRunStore."""
    from kg_query import server
    from kg_query.store import StardogRunStore

    class _MockStore(StardogRunStore):
        def __init__(self):
            pass

        def select(self, sparql: str) -> list[dict]:
            return [{"iri": "http://kg/1", "s": "0.9", "title": "T",
                     "type": "Learning", "snippet": "S"}]

    monkeypatch.setattr(server, "_store", _MockStore())
    result = server.kg_semantic_search("test")
    assert result["count"] == 1
    assert "model" in result  # stardog_search.semantic_search adds "model"


def test_server_hybrid_search_dispatches_for_stardog_run(monkeypatch):
    """kg_hybrid_search calls stardog_search.hybrid_search for StardogRunStore."""
    from kg_query import server
    from kg_query.store import StardogRunStore

    class _MockStore(StardogRunStore):
        def __init__(self):
            pass

        def select(self, sparql: str) -> list[dict]:
            return [{"iri": "http://kg/1", "score": "0.03",
                     "title": "T", "type": "Learning",
                     "snippet": "S", "matched": "lexical"}]

    monkeypatch.setattr(server, "_store", _MockStore())
    result = server.kg_hybrid_search("test")
    assert result["count"] == 1
    assert result["degraded"] is False  # stardog_search.hybrid_search sets degraded=False


def test_server_semantic_search_uses_local_for_non_stardog(monkeypatch):
    """kg_semantic_search uses local semantic.semantic_search for non-Stardog stores."""
    from kg_query import server, semantic

    called = []

    def fake_sem(query, limit, npz_path=None):
        called.append(query)
        return {"query": query, "count": 0, "results": []}

    monkeypatch.setattr(semantic, "semantic_search", fake_sem)
    monkeypatch.setattr(server, "_store", FakeStore([]))
    server.kg_semantic_search("test")
    assert called == ["test"]


def test_server_hybrid_search_uses_local_for_non_stardog(monkeypatch):
    """kg_hybrid_search uses local hybrid.hybrid_search for non-Stardog stores."""
    from kg_query import server, hybrid

    called = []

    def fake_hyb(s, query, limit, npz_path=None):
        called.append(query)
        return {"query": query, "count": 0, "degraded": True, "results": []}

    monkeypatch.setattr(hybrid, "hybrid_search", fake_hyb)
    monkeypatch.setattr(server, "_store", FakeStore([]))
    result = server.kg_hybrid_search("test")
    assert called == ["test"]
    assert result["degraded"] is True  # local path sets degraded based on embeddings


# ---------------------------------------------------------------------------
# __main__ runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    tests = [
        test_lit_escapes_double_quote,
        test_lit_escapes_backslash,
        test_lit_escapes_both,
        test_semantic_search_top_level_keys,
        test_semantic_search_query_and_model,
        test_semantic_search_count,
        test_semantic_search_result_item_keys,
        test_semantic_search_score_rounded,
        test_semantic_search_includes_fts_service,
        test_semantic_search_query_text_in_sparql,
        test_semantic_search_limit_in_sparql,
        test_semantic_search_empty_rows,
        test_hybrid_search_top_level_keys,
        test_hybrid_search_degraded_is_false,
        test_hybrid_search_query,
        test_hybrid_search_count,
        test_hybrid_search_result_item_keys,
        test_hybrid_search_matched_by_sorted,
        test_hybrid_search_score_rounded,
        test_hybrid_search_empty_rows,
        test_identifier_token_produces_regex_boost,
        test_no_identifier_token_no_regex,
        test_screaming_snake_produces_boost,
        test_query_with_double_quote_escaped_in_sparql,
        test_query_with_backslash_escaped_in_sparql,
        test_kg_neighbors_excludes_card_predicates,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:
            import traceback
            print(f"FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    if failed:
        print(f"\n{failed} test(s) failed.")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")
