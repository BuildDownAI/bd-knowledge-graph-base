"""Tests for KG_NAMESPACE environment variable override (KGB-49).

NAMESPACE and its dependents (KG, KGR, G_SPINE) are module-level constants
evaluated at import time.  Every test case launches a fresh Python subprocess
so the import happens with the desired environment, rather than patching after
the fact (which would see the already-computed value).

Run (plain): PYTHONPATH=. ./.venv/bin/python tests/test_namespace_env.py
Run (pytest): PYTHONPATH=. ./.venv/bin/python -m pytest tests/test_namespace_env.py -v
"""
from __future__ import annotations

import os
import subprocess
import sys


def _run(code: str, env: dict | None = None) -> str:
    """Run a snippet in a subprocess and return stdout (stripped)."""
    full_env = os.environ.copy()
    if env is not None:
        full_env.update(env)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert result.returncode == 0, (
        f"Subprocess exited {result.returncode}:\n{result.stderr}"
    )
    return result.stdout.strip()


def _baseline_namespace() -> str:
    """Return the namespace that sources.yml resolves to (without KG_NAMESPACE set)."""
    env = {k: v for k, v in os.environ.items() if k != "KG_NAMESPACE"}
    result = subprocess.run(
        [sys.executable, "-c",
         "from kg_ingest.iris import NAMESPACE; print(NAMESPACE)"],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert result.returncode == 0, (
        f"Could not read baseline namespace:\n{result.stderr}"
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_unset_keeps_baseline():
    """With KG_NAMESPACE unset, NAMESPACE equals what sources.yml provides."""
    env_without = {k: v for k, v in os.environ.items() if k != "KG_NAMESPACE"}
    result = subprocess.run(
        [sys.executable, "-c",
         "from kg_ingest.iris import NAMESPACE; print(NAMESPACE)"],
        capture_output=True,
        text=True,
        env=env_without,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert result.returncode == 0, result.stderr
    ns = result.stdout.strip()
    assert ns == _baseline_namespace()
    assert ns.endswith("/")


def test_env_set_overrides_sources_yml():
    """With KG_NAMESPACE set, all four values use that namespace."""
    test_ns = "https://kg.acme.test/"
    out = _run(
        "from kg_ingest.iris import NAMESPACE, KG, KGR, G_SPINE; "
        "print(NAMESPACE, str(KG), str(KGR), str(G_SPINE))",
        env={"KG_NAMESPACE": test_ns},
    )
    namespace, kg, kgr, g_spine = out.split(" ", 3)
    assert namespace == test_ns, f"NAMESPACE={namespace!r}"
    assert kg.startswith(test_ns), f"KG={kg!r}"
    assert kgr.startswith(test_ns), f"KGR={kgr!r}"
    assert g_spine.startswith(test_ns), f"G_SPINE={g_spine!r}"


def test_env_without_trailing_slash_gets_slash():
    """KG_NAMESPACE without a trailing / has one appended."""
    out = _run(
        "from kg_ingest.iris import NAMESPACE; print(NAMESPACE)",
        env={"KG_NAMESPACE": "https://kg.acme.test"},
    )
    assert out == "https://kg.acme.test/", f"Got {out!r}"


def test_empty_env_falls_back_to_sources_yml():
    """An empty KG_NAMESPACE has the same result as no variable."""
    baseline = _baseline_namespace()
    out = _run(
        "from kg_ingest.iris import NAMESPACE; print(NAMESPACE)",
        env={"KG_NAMESPACE": ""},
    )
    assert out == baseline, f"Expected {baseline!r}, got {out!r}"


def test_whitespace_only_env_falls_back_to_sources_yml():
    """A whitespace-only KG_NAMESPACE is treated as unset."""
    baseline = _baseline_namespace()
    out = _run(
        "from kg_ingest.iris import NAMESPACE; print(NAMESPACE)",
        env={"KG_NAMESPACE": "   "},
    )
    assert out == baseline, f"Expected {baseline!r}, got {out!r}"


def test_queries_prefixes_uses_env_namespace():
    """kg_query.queries.PREFIXES contains the env namespace when KG_NAMESPACE is set."""
    test_ns = "https://kg.acme.test/"
    out = _run(
        "from kg_query.queries import PREFIXES; print(PREFIXES)",
        env={"KG_NAMESPACE": test_ns},
    )
    expected_prefix = f"PREFIX kg: <{test_ns}onto#>"
    assert expected_prefix in out, (
        f"Expected {expected_prefix!r} in PREFIXES, got:\n{out}"
    )


# ---------------------------------------------------------------------------
# __main__ runner for the CI script loop
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_unset_keeps_baseline,
        test_env_set_overrides_sources_yml,
        test_env_without_trailing_slash_gets_slash,
        test_empty_env_falls_back_to_sources_yml,
        test_whitespace_only_env_falls_back_to_sources_yml,
        test_queries_prefixes_uses_env_namespace,
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
