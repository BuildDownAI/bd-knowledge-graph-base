"""Tests for background warm-up in HTTP server mode (KGB-43).

Run (plain): PYTHONPATH=. ./.venv/bin/python tests/test_server_warmup.py
Run (pytest): PYTHONPATH=. ./.venv/bin/python -m pytest tests/test_server_warmup.py -v
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_server_http.py — cannot import from there)
# ---------------------------------------------------------------------------

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
            "Accept": "application/json, text/event-stream",
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


# ---------------------------------------------------------------------------
# Store warm-up tests
# ---------------------------------------------------------------------------

def test_store_warmup_builds_store_without_tool_call():
    """_warmup() builds _store before any tool call when get_store succeeds."""
    import kg_query.server as server

    sentinel = object()
    original_store = server._store
    try:
        server._store = None
        with patch.object(server, "get_store", return_value=sentinel):
            server._warmup()
        assert server._store is sentinel, (
            f"Expected _store to be the sentinel after _warmup(), got {server._store!r}"
        )
    finally:
        server._store = original_store


def test_store_concurrent_first_calls_invoke_get_store_once():
    """Two concurrent first calls to store() build the store exactly once."""
    import kg_query.server as server

    call_count = 0
    barrier = threading.Barrier(2)
    result_sentinel = object()

    def slow_get_store():
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        return result_sentinel

    original_store = server._store
    try:
        server._store = None
        results: list = []

        with patch.object(server, "get_store", side_effect=slow_get_store):
            def race():
                barrier.wait()
                results.append(server.store())

            t1 = threading.Thread(target=race)
            t2 = threading.Thread(target=race)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert call_count == 1, (
            f"Expected get_store called exactly once, got {call_count}"
        )
        assert len(results) == 2
        assert results[0] is results[1]
    finally:
        server._store = original_store


def test_store_warmup_failure_does_not_prevent_later_load():
    """A _warmup() failure leaves _store None; a later store() call succeeds."""
    import kg_query.server as server

    sentinel = object()
    original_store = server._store
    try:
        server._store = None

        with patch.object(server, "get_store", side_effect=RuntimeError("disk error")):
            server._warmup()  # must not raise

        assert server._store is None, (
            "Expected _store to remain None after a failed warm-up"
        )

        with patch.object(server, "get_store", return_value=sentinel):
            result = server.store()

        assert result is sentinel
        assert server._store is sentinel
    finally:
        server._store = original_store


# ---------------------------------------------------------------------------
# Embeddings index warm-up tests
# ---------------------------------------------------------------------------

def test_semantic_warmup_builds_index_without_tool_call():
    """_warmup() builds semantic._index when the npz file exists."""
    import kg_query.server as server
    import kg_query.semantic as semantic

    index_sentinel = object()
    store_sentinel = object()
    original_store = server._store
    original_index = semantic._index
    try:
        server._store = None
        semantic._index = None

        with patch.object(server, "get_store", return_value=store_sentinel), \
             patch.object(semantic, "resolve_npz", return_value=Path("/fake/embeddings.npz")), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("kg_query.semantic._Index", return_value=index_sentinel):
            server._warmup()

        assert semantic._index is index_sentinel, (
            f"Expected semantic._index to be the sentinel after _warmup(), got {semantic._index!r}"
        )
    finally:
        server._store = original_store
        semantic._index = original_index


def test_semantic_concurrent_first_calls_invoke_index_once():
    """Two concurrent first calls to semantic._load() build the index exactly once."""
    import kg_query.semantic as semantic

    call_count = 0
    barrier = threading.Barrier(2)
    index_sentinel = object()

    original_index = semantic._index
    try:
        semantic._index = None

        def slow_index(npz_path):
            nonlocal call_count
            call_count += 1
            time.sleep(0.05)
            return index_sentinel

        results: list = []

        with patch("kg_query.semantic._Index", side_effect=slow_index):
            def race():
                barrier.wait()
                results.append(semantic._load(Path("/fake/embeddings.npz")))

            t1 = threading.Thread(target=race)
            t2 = threading.Thread(target=race)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert call_count == 1, (
            f"Expected _Index constructed exactly once, got {call_count}"
        )
        assert len(results) == 2
        assert results[0] is results[1]
    finally:
        semantic._index = original_index


def test_semantic_warmup_failure_does_not_prevent_later_load():
    """A semantic warm-up failure leaves _index None; a later _load() call succeeds."""
    import kg_query.server as server
    import kg_query.semantic as semantic

    index_sentinel = object()
    store_sentinel = object()
    original_store = server._store
    original_index = semantic._index
    try:
        server._store = None
        semantic._index = None

        with patch.object(server, "get_store", return_value=store_sentinel), \
             patch.object(semantic, "resolve_npz", return_value=Path("/fake/embeddings.npz")), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("kg_query.semantic._Index", side_effect=RuntimeError("npz corrupt")):
            server._warmup()  # must not raise

        assert semantic._index is None, (
            "Expected semantic._index to remain None after a failed warm-up"
        )

        with patch("kg_query.semantic._Index", return_value=index_sentinel):
            result = semantic._load(Path("/fake/embeddings.npz"))

        assert result is index_sentinel
        assert semantic._index is index_sentinel
    finally:
        server._store = original_store
        semantic._index = original_index


# ---------------------------------------------------------------------------
# Integration tests: HTTP server actually starts (AC #1, planning bar #8/#10)
# ---------------------------------------------------------------------------

def test_http_server_warms_up_store_at_start():
    """HTTP server builds _store in the background before any tool call (AC #1).

    Starts main() in a thread, waits for the port to bind, then asserts
    server._store is set without having made any MCP tool call.
    """
    import kg_query.server as server

    port = _get_free_port()
    sentinel = object()
    store_loaded = threading.Event()
    original_store = server._store

    def tracked_get_store():
        store_loaded.set()
        return sentinel

    try:
        server._store = None

        env_patch = {
            "KG_HTTP": "1",
            "KG_HTTP_HOST": "127.0.0.1",
            "KG_HTTP_PORT": str(port),
        }
        with patch.object(server, "get_store", side_effect=tracked_get_store), \
             patch.dict(os.environ, env_patch):
            t = threading.Thread(
                target=server.main, daemon=True, name="test-warmup-http-server"
            )
            t.start()

            assert _wait_for_port(port, timeout=15), (
                "HTTP server did not bind port in time"
            )
            assert store_loaded.wait(timeout=10), (
                "get_store was never called by the warmup thread"
            )
            # Allow _store assignment to complete after get_store returns
            time.sleep(0.05)

            assert server._store is sentinel, (
                f"Expected _store to be sentinel after warmup (no tool call made), "
                f"got {server._store!r}"
            )
    finally:
        server._store = original_store


def test_http_server_stays_up_after_warmup_failure():
    """HTTP server stays up and answers tools/list even when warmup raises (planning #10).

    Uses a subprocess so the FastMCP session manager gets a fresh instance
    (it can only run() once per Python process). A non-existent KG_TRIG path
    makes the warmup store-load raise, which is caught and logged; the server
    must still answer tools/list.
    """
    port = _get_free_port()

    env = os.environ.copy()
    env.update({
        "KG_HTTP": "1",
        "KG_HTTP_HOST": "127.0.0.1",
        "KG_HTTP_PORT": str(port),
        "KG_TRIG": "/nonexistent/__no_such_file__.trig",
    })

    proc = subprocess.Popen(
        [sys.executable, "-m", "kg_query.server"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for_port(port, timeout=15), (
            "HTTP server did not bind port in time after warmup failure"
        )
        # Give the warmup thread time to log the failure
        time.sleep(0.5)

        # Server must still answer tools/list despite the warmup failure
        status, result = _post_jsonrpc(port, "tools/list")
        assert status == 200, (
            f"Expected HTTP 200 for tools/list after warmup failure, "
            f"got {status}: {result}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# __main__ runner for the CI script loop
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_store_warmup_builds_store_without_tool_call,
        test_store_concurrent_first_calls_invoke_get_store_once,
        test_store_warmup_failure_does_not_prevent_later_load,
        test_semantic_warmup_builds_index_without_tool_call,
        test_semantic_concurrent_first_calls_invoke_index_once,
        test_semantic_warmup_failure_does_not_prevent_later_load,
        test_http_server_warms_up_store_at_start,
        test_http_server_stays_up_after_warmup_failure,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:
            print(f"FAIL  {fn.__name__}: {exc}")
            failed += 1
    if failed:
        print(f"\n{failed} test(s) failed.")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")
