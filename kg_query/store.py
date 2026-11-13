"""Store abstraction — the SAME SPARQL runs on rdflib (in-process) and Stardog (HTTP).

This is the "evaluating, not committed" seam: swap backends via KG_BACKEND without
touching the query templates or the MCP tools. Both present a union view of all
named graphs (rdflib flattens quads; Stardog DB has query.all.graphs=true).
"""
from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from pathlib import Path

DEFAULT_TRIG = Path(__file__).resolve().parent.parent / "out" / "graph.trig"
DEFAULT_PARTS_DIR = Path(__file__).resolve().parent.parent / "out" / "parts"


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
            for var in row.labels:  # type: ignore[union-attr]  # rdflib types SELECT rows as ResultRow | None; SELECT always yields ResultRow
                val = row[var]  # type: ignore[index]  # ResultRow supports Variable-key indexing but typeshed stubs don't model it
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


class NtPartsStore(Store):
    """In-process store that loads N-Triples parts directly, skipping TriG serialization.

    Accepts a directory of *.nt files (e.g. out/parts/) and flattens them into a
    union Graph for SPARQL. Produces the same select() results as RdflibStore on
    the same triples, with no Dataset.serialize call.
    """

    def __init__(self, parts_dir: Path = DEFAULT_PARTS_DIR):
        from rdflib import Graph
        union = Graph()
        for nt_file in sorted(parts_dir.glob("*.nt")):
            union.parse(str(nt_file), format="nt")
        self._g = union

    def select(self, sparql: str) -> list[dict[str, str]]:
        rows = []
        for row in self._g.query(sparql):
            d = {}
            for var in row.labels:  # type: ignore[union-attr]  # rdflib types SELECT rows as ResultRow | None; SELECT always yields ResultRow
                val = row[var]  # type: ignore[index]  # ResultRow supports Variable-key indexing but typeshed stubs don't model it
                if val is not None:
                    d[str(var)] = str(val)
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


class StardogRunStore(Store):
    """Serverless Stardog Run store: exchanges a Run API token for a bearer, then POSTs SPARQL."""

    TOKEN_SECONDS = 50 * 60  # the console's Stardog token lasts one hour
    WAKE_SECONDS = 300
    REQUEST_SECONDS = 120  # above the 60 s an endpoint holds a request while it starts

    def __init__(self, api_token: str, db: str, console: str = "https://console.stardog.run",
                 endpoint: str | None = None, dataset: str | None = None):
        import requests
        self._session = requests.Session()
        self._console = console.rstrip("/")
        self._api_token = api_token
        self.db = db
        self.dataset = dataset
        self._bearer: str | None = None
        self._minted: float = 0.0
        self._url: str | None = None
        rows = self._console_call("GET", "/api/cli/endpoints")
        if endpoint:
            rows = [r for r in rows if endpoint in (r["name"], r["target"], r["address"])]
        if len(rows) != 1:
            raise RuntimeError(f"Stardog Run: expected one endpoint, found {[r['name'] for r in rows]}; "
                               "set KG_STARDOG_RUN_ENDPOINT")
        self._target = rows[0]["target"]

    def _console_call(self, method: str, path: str, timeout: int = 30, **kwargs):
        resp = self._session.request(method, f"{self._console}{path}", timeout=timeout,
                                     headers={"Authorization": f"Bearer {self._api_token}"}, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"Stardog Run console {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def _token(self, renew: bool = False) -> str:
        if renew or self._bearer is None or time.monotonic() - self._minted > self.TOKEN_SECONDS:
            ns, name = self._target.split("/", 1)
            deadline = time.monotonic() + self.WAKE_SECONDS
            while True:
                try:
                    body = self._console_call("POST", "/api/token", timeout=self.REQUEST_SECONDS + 30,
                                              json={"ns": ns, "name": name})
                    if "pass" in body:  # new console shape: the edge trades the pass for the token
                        body = self._edge_token(body["edgeUrl"], body["pass"])
                    break
                except RuntimeError as e:
                    if "HTTP 502" not in str(e) or time.monotonic() + 5 > deadline:
                        raise
                    time.sleep(5)
            self._bearer, self._url, self._minted = body["token"], body["endpoint"].rstrip("/"), time.monotonic()
        return self._bearer  # type: ignore[return-value]

    def _edge_token(self, edge_url: str, edge_pass: str) -> dict:
        resp = self._session.post(f"{edge_url.rstrip('/')}/edge/token", timeout=self.REQUEST_SECONDS + 30,
                                  headers={"Authorization": f"Bearer {edge_pass}"})
        if resp.status_code >= 400:
            raise RuntimeError(f"Stardog Run edge /edge/token: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def request(self, method: str, path: str, data=None, headers: dict | None = None, **kwargs):
        deadline = time.monotonic() + self.WAKE_SECONDS
        renewed = False
        while True:
            token = self._token()
            resp = self._session.request(
                method, f"{self._url}{path}", data=data,
                headers={**(headers or {}), "Authorization": f"Bearer {token}"},
                timeout=self.REQUEST_SECONDS, **kwargs)
            if resp.status_code == 401 and not renewed:
                self._token(renew=True)
                renewed = True
                continue
            retry = resp.headers.get("Retry-After")
            if resp.status_code == 503 and retry and time.monotonic() + int(retry) < deadline:
                time.sleep(int(retry))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Stardog Run {method} {path}: HTTP {resp.status_code} {resp.text[:200]}")
            return resp

    def select(self, sparql: str) -> list[dict[str, str]]:
        params = {"default-graph-uri": self.dataset} if self.dataset else {}
        resp = self.request("POST", f"/{self.db}/query", data={"query": sparql, **params},
                            headers={"Accept": "application/sparql-results+json"})
        return [{k: v["value"] for k, v in b.items()}
                for b in resp.json().get("results", {}).get("bindings", [])]


def _trig_path_from_env() -> Path | str:
    trig_env = os.environ.get("KG_TRIG")
    if trig_env is not None:
        return trig_env
    data_dir = os.environ.get("KG_DATA_DIR")
    return Path(data_dir) / "graph.trig" if data_dir else DEFAULT_TRIG


def get_store() -> Store:
    """Build the store from env. KG_BACKEND=rdflib (default) | stardog | stardog_run | oxigraph | nt_parts."""
    backend = os.environ.get("KG_BACKEND", "rdflib").lower()
    if backend == "stardog":
        return StardogStore(
            endpoint=os.environ.get("KG_STARDOG_ENDPOINT", "http://localhost:5820"),
            db=os.environ.get("KG_STARDOG_DB", "aiimplement_kg"),
            user=os.environ.get("KG_STARDOG_USER", "admin"),
            password=os.environ.get("KG_STARDOG_PASSWORD", "admin"),
        )
    if backend == "stardog_run":
        return StardogRunStore(
            api_token=os.environ["KG_STARDOG_RUN_TOKEN"],
            db=os.environ["KG_STARDOG_DB"],
            console=os.environ.get("KG_STARDOG_RUN_CONSOLE", "https://console.stardog.run"),
            endpoint=os.environ.get("KG_STARDOG_RUN_ENDPOINT"),
            dataset=os.environ.get("KG_STARDOG_DATASET"),
        )
    if backend == "oxigraph":
        return OxigraphStore(_trig_path_from_env())
    if backend == "nt_parts":
        parts_env = os.environ.get("KG_PARTS_DIR")
        parts_dir = Path(parts_env) if parts_env else DEFAULT_PARTS_DIR
        return NtPartsStore(parts_dir)
    return RdflibStore(_trig_path_from_env())
