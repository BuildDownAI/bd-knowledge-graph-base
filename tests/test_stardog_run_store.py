"""Tests for StardogRunStore (KGB-50).

All tests use a local fake HTTP server; no test touches the network.

Run: PYTHONPATH=. ./.venv/bin/python -m pytest tests/test_stardog_run_store.py -v
"""
from __future__ import annotations

import collections
import json as _json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs

import pytest

from kg_query.store import StardogRunStore, get_store


# ---------------------------------------------------------------------------
# FakeTime — replaces kg_query.store.time so urllib3 keeps real time
# ---------------------------------------------------------------------------

class FakeTime:
    """Replacement for the `time` module in kg_query.store during tests.

    Patching kg_query.store.time (not time.monotonic globally) means urllib3
    keeps the real time module and its timeout calculations are unaffected.
    """

    def __init__(self, mono_values: list[float] | None = None, default: float = 0.0):
        self._q: list[float] = list(mono_values or [])
        self._default = default
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self._q.pop(0) if self._q else self._default

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)


# ---------------------------------------------------------------------------
# Fake HTTP server
# ---------------------------------------------------------------------------

class _FakeServer:
    """Single-process fake for the Stardog console, edge, and endpoint."""

    def __init__(self):
        self._routes: dict[str, collections.deque] = {}
        self._calls: list[tuple[str, str, bytes]] = []
        self._lock = threading.Lock()

        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def _serve(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b""
                key = f"{self.command} {self.path}"
                with srv._lock:
                    srv._calls.append((self.command, self.path, body))
                    q = srv._routes.get(key)
                    entry = q.popleft() if q else None

                if entry is None:
                    status, resp_body, extra = 500, f"no route for {key}".encode(), {}
                else:
                    status, resp_body, extra = entry

                if isinstance(resp_body, (dict, list)):
                    resp_bytes = _json.dumps(resp_body).encode()
                elif isinstance(resp_body, str):
                    resp_bytes = resp_body.encode()
                else:
                    resp_bytes = resp_body

                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in extra.items():
                    self.send_header(k, str(v))
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)

            def do_GET(self): self._serve()
            def do_POST(self): self._serve()

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        self._server = HTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{port}"
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()

    def add(self, method: str, path: str, status: int, body, headers: dict | None = None) -> "_FakeServer":
        key = f"{method} {path}"
        if key not in self._routes:
            self._routes[key] = collections.deque()
        self._routes[key].append((status, body, headers or {}))
        return self

    def call_count(self, method: str, path: str) -> int:
        with self._lock:
            return sum(1 for m, p, _ in self._calls if m == method and p == path)

    def last_body(self, method: str, path: str) -> bytes:
        with self._lock:
            bodies = [b for m, p, b in self._calls if m == method and p == path]
        return bodies[-1] if bodies else b""

    def stop(self):
        self._server.shutdown()

    def __enter__(self): return self
    def __exit__(self, *a): self.stop()


def _endpoints(url: str, name: str = "ep", target: str = "ns/ep") -> list:
    return [{"name": name, "target": target, "address": url}]


def _token_body(url: str, token: str = "tok1") -> dict:
    return {"token": token, "endpoint": url}


def _empty_sparql_result() -> dict:
    return {"results": {"bindings": []}}


# ---------------------------------------------------------------------------
# Tests: get_store() factory
# ---------------------------------------------------------------------------

def test_get_store_returns_stardog_run_store():
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        env = {
            "KG_BACKEND": "stardog_run",
            "KG_STARDOG_RUN_TOKEN": "mytoken",
            "KG_STARDOG_DB": "mydb",
            "KG_STARDOG_RUN_CONSOLE": srv.url,
        }
        with patch.dict(os.environ, env):
            store = get_store()
        assert isinstance(store, StardogRunStore)
        assert store.db == "mydb"


# ---------------------------------------------------------------------------
# Tests: token exchange
# ---------------------------------------------------------------------------

def test_token_exchange_once():
    """Two consecutive select() calls share one token exchange."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, _token_body(srv.url, "tok1"))
        srv.add("POST", "/api/token", 200, _token_body(srv.url, "tok2"))
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())

        # Calls: request-deadline, _token-deadline, _minted,
        #        request-deadline2, expiry-check (→0.0−0.0=0≤TOKEN_SECONDS→no renewal)
        ft = FakeTime([0.0, 0.0, 0.0, 0.0, 0.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            store.select("SELECT * WHERE { ?s ?p ?o }")
            store.select("SELECT * WHERE { ?s ?p ?o }")

        assert srv.call_count("POST", "/api/token") == 1


def test_token_renewal_after_expiry():
    """After TOKEN_SECONDS, the next select() exchanges a new token."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, _token_body(srv.url, "tok1"))
        srv.add("POST", "/api/token", 200, _token_body(srv.url, "tok2"))
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())

        # 1st select: deadline(0), token-deadline(0), minted(1.0)
        # 2nd select: deadline(0), expiry-check(4000→4000−1>TOKEN_SECONDS→renew),
        #             token-deadline(0), minted(2.0)
        ft = FakeTime([0.0, 0.0, 1.0, 0.0, 4000.0, 0.0, 2.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            store.select("SELECT * WHERE { ?s ?p ?o }")
            store.select("SELECT * WHERE { ?s ?p ?o }")

        assert srv.call_count("POST", "/api/token") == 2


# ---------------------------------------------------------------------------
# Tests: 401 handling
# ---------------------------------------------------------------------------

def test_401_causes_one_renewal_then_success():
    """A 401 from the endpoint causes one token renewal; the retry succeeds."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, _token_body(srv.url, "tok1"))
        srv.add("POST", "/api/token", 200, _token_body(srv.url, "tok2"))
        srv.add("POST", "/ns/ep/query", 401, b"unauthorized")
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())

        # deadline, token-deadline, minted1, renewal-deadline, minted2, expiry-check
        ft = FakeTime([0.0, 0.0, 1.0, 0.0, 2.0, 0.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            result = store.select("SELECT * WHERE { ?s ?p ?o }")

        assert result == []
        assert srv.call_count("POST", "/api/token") == 2
        assert srv.call_count("POST", "/ns/ep/query") == 2


def test_401_twice_raises():
    """A second 401 (after renewal) raises rather than retrying indefinitely."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, _token_body(srv.url, "tok1"))
        srv.add("POST", "/api/token", 200, _token_body(srv.url, "tok2"))
        srv.add("POST", "/ns/ep/query", 401, b"unauthorized")
        srv.add("POST", "/ns/ep/query", 401, b"unauthorized again")

        ft = FakeTime([0.0, 0.0, 1.0, 0.0, 2.0, 0.0])
        with patch("kg_query.store.time", ft), \
             pytest.raises(RuntimeError, match="HTTP 401"):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            store.select("SELECT * WHERE { ?s ?p ?o }")


# ---------------------------------------------------------------------------
# Tests: 503 handling
# ---------------------------------------------------------------------------

def test_503_retry_after_retries():
    """503 with Retry-After is retried; the second call succeeds."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, _token_body(srv.url))
        srv.add("POST", "/ns/ep/query", 503, b"starting", {"Retry-After": "1"})
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())

        # deadline(0→300), token-deadline, minted(1), 503-check(0+1<300→retry),
        # then loop-back: expiry-check(0−1=−1≤TOKEN_SECONDS→no renewal)
        ft = FakeTime([0.0, 0.0, 1.0, 0.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            result = store.select("SELECT * WHERE { ?s ?p ?o }")

        assert result == []
        assert ft.sleep_calls == [1]
        assert srv.call_count("POST", "/ns/ep/query") == 2


def test_503_no_retry_after_raises_immediately():
    """503 with no Retry-After raises at once without sleeping."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, _token_body(srv.url))
        srv.add("POST", "/ns/ep/query", 503, b"paused")

        ft = FakeTime([0.0, 0.0, 1.0])
        with patch("kg_query.store.time", ft), \
             pytest.raises(RuntimeError, match="HTTP 503"):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            store.select("SELECT * WHERE { ?s ?p ?o }")

        assert ft.sleep_calls == [], "sleep should not be called for 503 with no Retry-After"


# ---------------------------------------------------------------------------
# Tests: 502 wake retry on /api/token
# ---------------------------------------------------------------------------

def test_502_token_retried_until_wake_seconds():
    """502 from POST /api/token is retried; once past WAKE_SECONDS it raises."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 502, b"starting")
        srv.add("POST", "/api/token", 502, b"starting")
        srv.add("POST", "/api/token", 200, _token_body(srv.url))

        # deadline(0), token-deadline(0→300),
        # after 1st 502: check(10→10+5=15≤300→retry), sleep(5),
        # after 2nd 502: check(296→296+5=301>300→raise)
        ft = FakeTime([0.0, 0.0, 10.0, 296.0])
        with patch("kg_query.store.time", ft), \
             pytest.raises(RuntimeError, match="HTTP 502"):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            store.select("SELECT * WHERE { ?s ?p ?o }")

        assert srv.call_count("POST", "/api/token") == 2
        assert ft.sleep_calls == [5]


def test_502_token_succeeds_after_one_retry():
    """502 from POST /api/token is retried and succeeds on the second attempt."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 502, b"starting")
        srv.add("POST", "/api/token", 200, _token_body(srv.url))
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())

        # deadline(0), token-deadline(0), after 502: check(10→retry), minted(1)
        ft = FakeTime([0.0, 0.0, 10.0, 1.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            result = store.select("SELECT * WHERE { ?s ?p ?o }")

        assert result == []
        assert srv.call_count("POST", "/api/token") == 2


# ---------------------------------------------------------------------------
# Tests: new and old token shapes
# ---------------------------------------------------------------------------

def test_new_token_shape_uses_edge_token():
    """When /api/token returns pass+edgeUrl, the store POSTs to <edgeUrl>/edge/token."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, {
            "pass": "mypass",
            "edgeUrl": srv.url,
            "endpoint": srv.url,
            "platformUrl": srv.url,
        })
        srv.add("POST", "/edge/token", 200, _token_body(srv.url, "edgetok"))
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())

        ft = FakeTime([0.0, 0.0, 1.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            store.select("SELECT * WHERE { ?s ?p ?o }")

        assert srv.call_count("POST", "/api/token") == 1
        assert srv.call_count("POST", "/edge/token") == 1
        assert store._bearer == "edgetok"


def test_old_token_shape_no_edge_call():
    """When /api/token returns token+endpoint (old shape), no edge call is made."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, _token_body(srv.url, "oldtok"))
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())

        ft = FakeTime([0.0, 0.0, 1.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            store.select("SELECT * WHERE { ?s ?p ?o }")

        assert srv.call_count("POST", "/edge/token") == 0
        assert store._bearer == "oldtok"


def test_edge_502_retried_same_as_token_502():
    """502 from /edge/token propagates as RuntimeError('HTTP 502'), retried by _token's loop."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        # 1st attempt: edge returns 502
        srv.add("POST", "/api/token", 200, {
            "pass": "mypass", "edgeUrl": srv.url,
            "endpoint": srv.url, "platformUrl": srv.url,
        })
        srv.add("POST", "/edge/token", 502, b"starting")
        # 2nd attempt (loop retry): succeeds
        srv.add("POST", "/api/token", 200, {
            "pass": "mypass2", "edgeUrl": srv.url,
            "endpoint": srv.url, "platformUrl": srv.url,
        })
        srv.add("POST", "/edge/token", 200, _token_body(srv.url, "edgetok2"))
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())

        # deadline(0), token-deadline(0), after edge-502: check(10→retry), minted(1)
        ft = FakeTime([0.0, 0.0, 10.0, 1.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            store.select("SELECT * WHERE { ?s ?p ?o }")

        assert srv.call_count("POST", "/api/token") == 2
        assert srv.call_count("POST", "/edge/token") == 2


# ---------------------------------------------------------------------------
# Tests: endpoint selection
# ---------------------------------------------------------------------------

def test_zero_endpoints_raises_mentioning_env_var():
    """Zero endpoints raises a RuntimeError naming KG_STARDOG_RUN_ENDPOINT."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, [])
        with pytest.raises(RuntimeError, match="KG_STARDOG_RUN_ENDPOINT"):
            StardogRunStore(api_token="t", db="db", console=srv.url)


def test_multiple_endpoints_no_filter_raises():
    """More than one endpoint with no filter raises, naming KG_STARDOG_RUN_ENDPOINT."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, [
            {"name": "ep1", "target": "ns/ep1", "address": srv.url},
            {"name": "ep2", "target": "ns/ep2", "address": srv.url},
        ])
        with pytest.raises(RuntimeError, match="KG_STARDOG_RUN_ENDPOINT"):
            StardogRunStore(api_token="t", db="db", console=srv.url)


def test_endpoint_filter_selects_by_name():
    """KG_STARDOG_RUN_ENDPOINT filters to the matching endpoint by name."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, [
            {"name": "ep1", "target": "ns/ep1", "address": "addr1"},
            {"name": "ep2", "target": "ns/ep2", "address": "addr2"},
        ])
        store = StardogRunStore(api_token="t", db="db", console=srv.url, endpoint="ep2")
        assert store._target == "ns/ep2"


# ---------------------------------------------------------------------------
# Tests: default-graph-uri
# ---------------------------------------------------------------------------

def test_default_graph_uri_set_when_dataset():
    """With KG_STARDOG_DATASET set, the query POST body includes default-graph-uri."""
    alias = "tag:stardog:kit:alias:builddown_kg"
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, _token_body(srv.url))
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())

        ft = FakeTime([0.0, 0.0, 1.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url, dataset=alias)
            store.select("SELECT * WHERE { ?s ?p ?o }")

    body = srv.last_body("POST", "/ns/ep/query")
    params = parse_qs(body.decode())
    assert "default-graph-uri" in params
    assert params["default-graph-uri"][0] == alias


def test_no_default_graph_uri_when_no_dataset():
    """Without KG_STARDOG_DATASET, the query POST body has no default-graph-uri."""
    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, _token_body(srv.url))
        srv.add("POST", "/ns/ep/query", 200, _empty_sparql_result())

        ft = FakeTime([0.0, 0.0, 1.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url, dataset=None)
            store.select("SELECT * WHERE { ?s ?p ?o }")

    body = srv.last_body("POST", "/ns/ep/query")
    assert b"default-graph-uri" not in body


# ---------------------------------------------------------------------------
# Tests: token security
# ---------------------------------------------------------------------------

def test_no_token_in_error_messages():
    """API token and bearer token do not appear in exception messages."""
    secret_api = "SECRET-API-TOKEN-12345"
    secret_bearer = "SECRET-BEARER-67890"

    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, {"token": secret_bearer, "endpoint": srv.url})
        srv.add("POST", "/ns/ep/query", 400, b"bad request error text")

        ft = FakeTime([0.0, 0.0, 1.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token=secret_api, db="ns/ep", console=srv.url)
            with pytest.raises(RuntimeError) as exc_info:
                store.select("SELECT * WHERE { ?s ?p ?o }")

    err = str(exc_info.value)
    assert secret_api not in err, "API token leaked into error message"
    assert secret_bearer not in err, "bearer token leaked into error message"


# ---------------------------------------------------------------------------
# Tests: kg_neighbors integration
# ---------------------------------------------------------------------------

def test_kg_neighbors_returns_edges():
    """kg_neighbors returns edges when the fake endpoint returns SPARQL JSON bindings."""
    from kg_query import queries

    sparql_bindings = {
        "results": {
            "bindings": [
                {
                    "dir": {"type": "literal", "value": "out"},
                    "pred": {"type": "uri", "value": "http://example.org/pred"},
                    "other": {"type": "uri", "value": "http://example.org/neighbor"},
                }
            ]
        }
    }

    with _FakeServer() as srv:
        srv.add("GET", "/api/cli/endpoints", 200, _endpoints(srv.url))
        srv.add("POST", "/api/token", 200, _token_body(srv.url))
        srv.add("POST", "/ns/ep/query", 200, sparql_bindings)

        ft = FakeTime([0.0, 0.0, 1.0])
        with patch("kg_query.store.time", ft):
            store = StardogRunStore(api_token="t", db="ns/ep", console=srv.url)
            result = queries.kg_neighbors(store, "http://example.org/node/1")

    assert result["count"] > 0
    assert result["edges"][0]["neighbor"] == "http://example.org/neighbor"


# ---------------------------------------------------------------------------
# __main__ runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    tests = [
        test_get_store_returns_stardog_run_store,
        test_token_exchange_once,
        test_token_renewal_after_expiry,
        test_401_causes_one_renewal_then_success,
        test_401_twice_raises,
        test_503_retry_after_retries,
        test_503_no_retry_after_raises_immediately,
        test_502_token_retried_until_wake_seconds,
        test_502_token_succeeds_after_one_retry,
        test_new_token_shape_uses_edge_token,
        test_old_token_shape_no_edge_call,
        test_edge_502_retried_same_as_token_502,
        test_zero_endpoints_raises_mentioning_env_var,
        test_multiple_endpoints_no_filter_raises,
        test_endpoint_filter_selects_by_name,
        test_default_graph_uri_set_when_dataset,
        test_no_default_graph_uri_when_no_dataset,
        test_no_token_in_error_messages,
        test_kg_neighbors_returns_edges,
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
