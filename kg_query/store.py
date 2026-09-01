"""Store abstraction — the SAME SPARQL runs on rdflib (in-process) and Stardog (HTTP).

This is the "evaluating, not committed" seam: swap backends via KG_BACKEND without
touching the query templates or the MCP tools. Both present a union view of all
named graphs (rdflib flattens quads; Stardog DB has query.all.graphs=true).
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

DEFAULT_TRIG = Path(__file__).resolve().parent.parent / "out" / "graph.trig"


class Store(ABC):
    @abstractmethod
    def select(self, sparql: str) -> list[dict[str, str]]:
        """Run a SELECT; return rows as {var: str-value}. Unbound vars omitted."""

    def backend(self) -> str:
        return type(self).__name__


class RdflibStore(Store):
    """In-process store: parse the TriG into a flattened union Graph."""

    def __init__(self, trig_path: str | Path = DEFAULT_TRIG):
        from rdflib import Dataset, Graph
        ds = Dataset()
        ds.parse(str(trig_path), format="trig")
        union = Graph()
        for s, p, o, _ in ds.quads((None, None, None, None)):
            union.add((s, p, o))
        self._g = union

    def select(self, sparql: str) -> list[dict[str, str]]:
        rows = []
        for row in self._g.query(sparql):
            d = {}
            for var in row.labels:
                val = row[var]
                if val is not None:
                    d[str(var)] = str(val)
            rows.append(d)
        return rows


class OxigraphStore(Store):
    """In-process store backed by Oxigraph; flattens quads into default graph for SPARQL parity."""

    def __init__(self, trig_path: str | Path = DEFAULT_TRIG):
        import pyoxigraph

        # Load the TriG into a staging store (populates named graphs).
        staging = pyoxigraph.Store()
        staging.load(path=str(trig_path), format=pyoxigraph.RdfFormat.TRIG)

        # Flatten all quads into the default graph so SPARQL sees a union view.
        self._store = pyoxigraph.Store()
        default = pyoxigraph.DefaultGraph()
        for quad in staging:
            self._store.add(pyoxigraph.Quad(quad.subject, quad.predicate, quad.object, default))

    def select(self, sparql: str) -> list[dict[str, str]]:
        import pyoxigraph

        result = self._store.query(sparql)
        if not isinstance(result, pyoxigraph.QuerySolutions):
            return []
        # Capture variable list before iteration; the iterator is single-pass.
        variables = result.variables
        rows = []
        for solution in result:
            d = {}
            for var in variables:
                val = solution[var]
                if val is not None:
                    d[var.value] = val.value
            rows.append(d)
        return rows


class StardogStore(Store):
    """HTTP store: POST SPARQL to Stardog. Stateless -> reconnect-tolerant."""

    def __init__(self, endpoint: str, db: str, user: str, password: str):
        import requests
        self._url = f"{endpoint.rstrip('/')}/{db}/query"
        self._auth = (user, password)
        self._session = requests.Session()

    def select(self, sparql: str) -> list[dict[str, str]]:
        resp = self._session.post(
            self._url,
            data={"query": sparql},
            auth=self._auth,
            headers={"Accept": "application/sparql-results+json"},
            timeout=30,
        )
        resp.raise_for_status()
        out = []
        for b in resp.json().get("results", {}).get("bindings", []):
            out.append({k: v["value"] for k, v in b.items()})
        return out


def _trig_path_from_env() -> Path | str:
    trig = os.environ.get("KG_TRIG")
    if trig is None:
        data_dir = os.environ.get("KG_DATA_DIR")
        trig = Path(data_dir) / "graph.trig" if data_dir else DEFAULT_TRIG
    return trig


def get_store() -> Store:
    """Build the store from env. KG_BACKEND=rdflib (default) | stardog | oxigraph."""
    backend = os.environ.get("KG_BACKEND", "rdflib").lower()
    if backend == "stardog":
        return StardogStore(
            endpoint=os.environ.get("KG_STARDOG_ENDPOINT", "http://localhost:5820"),
            db=os.environ.get("KG_STARDOG_DB", "aiimplement_kg"),
            user=os.environ.get("KG_STARDOG_USER", "admin"),
            password=os.environ.get("KG_STARDOG_PASSWORD", "admin"),
        )
    if backend == "oxigraph":
        return OxigraphStore(_trig_path_from_env())
    return RdflibStore(_trig_path_from_env())
