"""Smoke test: stateless HTTP server mode (KGB-28).

Boots the server with KG_HTTP=1 and POSTs tools/list and one tools/call
without a session header, asserting HTTP 200 and the expected six kg_* tools.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_server_http.py
Run (custom namespace): KG_SERVER_NAME=custom-kg PYTHONPATH=. ./.venv/bin/python tests/test_server_http.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from kg_ingest.iris import NAMESPACE as NS

EXPECTED_TOOLS = {
    "kg_search",
    "kg_semantic_search",
    "kg_hybrid_search",
    "kg_neighbors",
    "kg_provenance",
    "kg_path",
}

FIXTURE = f"""
@prefix kg: <{NS}onto#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<{NS}resource/graph/spine> {{
  <{NS}resource/doc/example/test> a kg:Doc ;
    dcterms:title "Test doc for HTTP smoke" .
  <{NS}resource/topic/test> a kg:Topic ; skos:prefLabel "test" .
}}
<{NS}resource/graph/run/testrun> {{
  <{NS}resource/run/testrun> a kg:ExtractionRun ;
    kg:pipelineVer "0.0-test" ; kg:model "deterministic-parser" ;
    kg:promptHash "n/a" .
  <{NS}resource/learning/test-smoke> a kg:Learning ;
    dcterms:title "Smoke test learning" ;
    kg:fix "n/a" ;
    kg:tagged <{NS}resource/topic/test> ;
    prov:wasDerivedFrom <{NS}resource/doc/example/test> ;
    prov:wasGeneratedBy <{NS}resource/run/testrun> .
}}
"""


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _post_jsonrpc(port: int, method: str, params: dict | None = None) -> tuple[int, dict]:
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body.decode(errors="replace")}


def main() -> None:
    with tempfile.NamedTemporaryFile(suffix=".trig", mode="w", delete=False) as f:
        f.write(FIXTURE)
        trig_path = f.name

    try:
        port = _get_free_port()
        env = os.environ.copy()
        env.update({
            "KG_HTTP": "1",
            "KG_HTTP_HOST": "127.0.0.1",
            "KG_HTTP_PORT": str(port),
            "KG_TRIG": trig_path,
        })

        proc = subprocess.Popen(
            [sys.executable, "-m", "kg_query.server"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            if not _wait_for_port(port):
                stderr_out = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                print("FAIL: server did not bind port in time")
                if stderr_out:
                    print("--- server stderr ---")
                    print(stderr_out)
                sys.exit(1)

            namespace = env.get("KG_SERVER_NAME", "kg")
            print(f"== tools/list (port={port}, name={namespace}) ==")
            status, result = _post_jsonrpc(port, "tools/list")
            assert status == 200, f"Expected HTTP 200 for tools/list, got {status}: {result}"
            tools = result.get("result", {}).get("tools", [])
            names = {t["name"] for t in tools}
            assert names == EXPECTED_TOOLS, (
                f"Expected tools {sorted(EXPECTED_TOOLS)}, got {sorted(names)}"
            )
            print(f"  tools: {sorted(names)}")

            print("== tools/call kg_search (no session header) ==")
            status, call_result = _post_jsonrpc(port, "tools/call", {
                "name": "kg_search",
                "arguments": {"term": "test"},
            })
            assert status == 200, f"Expected HTTP 200 for tools/call, got {status}: {call_result}"
            assert "result" in call_result, f"Expected result key in response, got: {call_result}"
            print(f"  kg_search OK")

            print("\nALL HTTP SMOKE CHECKS PASSED")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    finally:
        os.unlink(trig_path)


if __name__ == "__main__":
    main()
