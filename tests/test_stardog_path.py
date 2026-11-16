"""Tests for _stardog_path / kg_path on Stardog stores (KGB-52).

FakeStardogRunStore subclasses StardogRunStore but skips the network call in
__init__ and records every SPARQL string passed to select().

Run: PYTHONPATH=. ./.venv/bin/python tests/test_stardog_path.py
"""
from __future__ import annotations

from kg_query import queries
from kg_query.store import StardogRunStore

FROM_IRI = "http://example.org/node/A"
TO_IRI = "http://example.org/node/B"


class FakeStardogRunStore(StardogRunStore):
    def __init__(self, rows: list[dict[str, str]] | None = None):
        self.db = "fakedb"
        self.dataset = None
        self._rows: list[dict[str, str]] = rows or []
        self._queries: list[str] = []

    def select(self, sparql: str) -> list[dict[str, str]]:
        self._queries.append(sparql)
        return self._rows


def test_stardog_path_sends_one_paths_query():
    """kg_path on a Stardog store sends exactly one PATHS START query."""
    fake = FakeStardogRunStore([])
    queries.kg_path(fake, FROM_IRI, TO_IRI)
    assert len(fake._queries) == 1, f"expected 1 query, got {len(fake._queries)}"
    assert fake._queries[0].lstrip().startswith("PATHS START"), (
        f"query must start with 'PATHS START', got: {fake._queries[0][:60]!r}"
    )


def test_stardog_path_result_shape_with_rows():
    """Rows from the fake store give the expected result keys and path step keys."""
    rows = [
        {"p": "http://example.org/pred#rel", "dir": "out", "y": TO_IRI},
    ]
    fake = FakeStardogRunStore(rows)
    result = queries.kg_path(fake, FROM_IRI, TO_IRI)

    assert set(result.keys()) == {"from", "to", "backend", "reachable", "length", "path"}
    assert result["from"] == FROM_IRI
    assert result["to"] == TO_IRI
    assert result["reachable"] is True
    assert result["length"] == 1
    assert len(result["path"]) == 1
    step = result["path"][0]
    assert set(step.keys()) == {"predicate", "direction", "node"}
    assert step["predicate"] == "rel"
    assert step["direction"] == "out"
    assert step["node"] == TO_IRI


def test_stardog_path_no_rows_gives_unreachable():
    """No rows gives reachable=False, length=None, path=[]."""
    fake = FakeStardogRunStore([])
    result = queries.kg_path(fake, FROM_IRI, TO_IRI)

    assert result["reachable"] is False
    assert result["length"] is None
    assert result["path"] == []


def test_same_iri_uses_bfs_not_paths():
    """With from_iri == to_iri, the BFS runs and no PATHS query is sent."""
    fake = FakeStardogRunStore([])
    same_iri = "http://example.org/node/A"
    queries.kg_path(fake, same_iri, same_iri)
    assert not any(q.lstrip().startswith("PATHS START") for q in fake._queries), (
        "PATHS START query must not be sent when from_iri == to_iri"
    )


def test_max_len_embedded_as_integer():
    """max_len goes into the PATHS query as an integer."""
    fake = FakeStardogRunStore([])
    queries.kg_path(fake, FROM_IRI, TO_IRI, max_len=3)
    assert len(fake._queries) == 1
    assert "MAX LENGTH 3" in fake._queries[0]


def test_backend_key_in_result():
    """The result dict contains 'backend' matching fake_store.backend()."""
    fake = FakeStardogRunStore([])
    result = queries.kg_path(fake, FROM_IRI, TO_IRI)
    assert "backend" in result
    assert result["backend"] == fake.backend()


if __name__ == "__main__":
    import sys

    tests = [
        test_stardog_path_sends_one_paths_query,
        test_stardog_path_result_shape_with_rows,
        test_stardog_path_no_rows_gives_unreachable,
        test_same_iri_uses_bfs_not_paths,
        test_max_len_embedded_as_integer,
        test_backend_key_in_result,
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
