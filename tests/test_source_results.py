"""Test that kg_ingest writes ai-output/source-results.json with one entry per source.

Covers KGB-36: per-source results (success/skipped/failed) and the independent-source rule.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_source_results.py
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
AI_OUTPUT_DIR = REPO_ROOT / "ai-output"
TWO_TEAMS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "tracker-data-two-teams.json"
KGA7_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "tracker-data-kga7.json"
SOURCES_YML = REPO_ROOT / "sources.yml"

_SNAPSHOT_DIR = REPO_ROOT / "snapshot"
_snapshot_backup = Path(tempfile.mkdtemp(prefix="kg-snapshot-backup-")) / "snapshot"
if _SNAPSHOT_DIR.exists():
    shutil.copytree(_SNAPSHOT_DIR, _snapshot_backup)

_sources_backup = Path(tempfile.mkdtemp(prefix="kg-sources-backup-")) / "sources.yml"
shutil.copy2(SOURCES_YML, _sources_backup)


def _restore() -> None:
    if _snapshot_backup.exists():
        shutil.rmtree(_SNAPSHOT_DIR, ignore_errors=True)
        shutil.copytree(_snapshot_backup, _SNAPSHOT_DIR)
        shutil.rmtree(_snapshot_backup.parent, ignore_errors=True)
    if _sources_backup.exists():
        shutil.copy2(_sources_backup, SOURCES_YML)
        shutil.rmtree(_sources_backup.parent, ignore_errors=True)


atexit.register(_restore)


def check(name: str, got, want) -> None:
    assert got == want, f"FAIL {name}: got {got!r}, want {want!r}"
    print(f"PASS: {name}")


def _run_refresh(*extra_args, env=None) -> subprocess.CompletedProcess:
    base_env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
    base_env["PYTHONPATH"] = str(REPO_ROOT)
    if env:
        base_env.update(env)
    return subprocess.run(
        [
            sys.executable, "-m", "kg_ingest", "refresh",
            "--code-repo", str(REPO_ROOT),
            "--no-embed",
            "--no-docs-sites",
            "--max-prs", "0",
            "--max-commits", "5",
        ] + list(extra_args),
        capture_output=True, text=True, cwd=str(REPO_ROOT),
        env=base_env,
        timeout=300,
    )


def _read_results() -> list[dict]:
    results_file = AI_OUTPUT_DIR / "source-results.json"
    assert results_file.exists(), f"source-results.json not written: {results_file}"
    return json.loads(results_file.read_text(encoding="utf-8"))


# ---- Test 1: --no-docs-sites and self_ingest: false ----
# Default sources.yml has self_ingest: false; --no-docs-sites is passed explicitly.
result = _run_refresh("--tracker-data", str(KGA7_FIXTURE))
if result.returncode != 0:
    print(f"FAIL: refresh exited {result.returncode}")
    print("STDOUT:", result.stdout[-3000:])
    print("STDERR:", result.stderr[-1000:])
    sys.exit(1)

results = _read_results()

_docs_entry = next((r for r in results if r["kind"] == "docs_sites"), None)
assert _docs_entry is not None, "FAIL: no docs_sites entry in source-results.json"
check("--no-docs-sites: status is skipped", _docs_entry["status"], "skipped")
check("--no-docs-sites: reason names the flag", _docs_entry["reason"], "--no-docs-sites")

_self_entry = next((r for r in results if r["kind"] == "self"), None)
assert _self_entry is not None, "FAIL: no self entry in source-results.json"
check("self_ingest: false: status is skipped", _self_entry["status"], "skipped")

_spine_entry = next((r for r in results if r["kind"] == "spine"), None)
assert _spine_entry is not None, "FAIL: no spine entry in source-results.json"
check("spine entry status is success", _spine_entry["status"], "success")

for entry in results:
    assert all(k in entry for k in ("source", "kind", "status", "reason", "counts")), (
        f"FAIL: entry missing required keys: {entry}"
    )
check("all entries have required keys", True, True)

print("\nTest 1 passed: --no-docs-sites and self_ingest: false")

# ---- Test 2: two-team tracker writes two tracker entries ----
results_tracker = [r for r in results if r["kind"] == "tracker"]
# KGA7_FIXTURE has AII team issues
check("tracker: at least one entry per team",
      len(results_tracker) >= 1, True)
for tr in results_tracker:
    check(f"tracker {tr['source']}: status is success", tr["status"], "success")
    check(f"tracker {tr['source']}: has issues count", "issues" in tr["counts"], True)
    check(f"tracker {tr['source']}: issues > 0", tr["counts"]["issues"] > 0, True)

print("\nTest 2 passed: tracker entries with issue counts")

# ---- Test 3: two-team fixture writes two distinct tracker entries ----
result3 = _run_refresh("--tracker-data", str(TWO_TEAMS_FIXTURE))
if result3.returncode != 0:
    print(f"FAIL: two-teams refresh exited {result3.returncode}")
    print("STDOUT:", result3.stdout[-3000:])
    print("STDERR:", result3.stderr[-1000:])
    sys.exit(1)

results3 = _read_results()
tracker3 = [r for r in results3 if r["kind"] == "tracker"]
check("two-teams: exactly two tracker entries", len(tracker3), 2)
team_names3 = {r["source"] for r in tracker3}
check("two-teams: TST team present", "TST" in team_names3, True)
check("two-teams: EXT team present", "EXT" in team_names3, True)

tst = next(r for r in tracker3 if r["source"] == "TST")
ext = next(r for r in tracker3 if r["source"] == "EXT")
check("two-teams: TST issues count", tst["counts"]["issues"], 2)
check("two-teams: EXT issues count", ext["counts"]["issues"], 3)

print("\nTest 3 passed: two-team tracker fixture")

# ---- Test 4: secondary repo success + failed ----
# Temporarily modify sources.yml to declare two secondary repos.
_MODIFIED_SOURCES_YML = """\
namespace: https://example.org/kg/
code_repo:
  slug: your-org/your-project
  doc_globs:
    - docs/solutions/**/*.md
    - docs/adr/*.md
    - docs/plans/*.md
self_ingest: false
secondary_repos:
  - slug: test-org/sec-present
    path: ./
  - slug: test-org/sec-missing
    path: ../nonexistent-missing-dir-kgb36
trackers: []
"""
SOURCES_YML.write_text(_MODIFIED_SOURCES_YML, encoding="utf-8")

# Create a repos-root where sec-present exists (symlink to REPO_ROOT)
# and sec-missing does not.
_repos_root = Path(tempfile.mkdtemp(prefix="kg-sec-repos-"))
_present = _repos_root / "sec-present"
_present.symlink_to(REPO_ROOT)
# sec-missing is intentionally absent from _repos_root

result4 = _run_refresh(
    "--tracker-data", str(TWO_TEAMS_FIXTURE),
    "--repos-root", str(_repos_root),
)
if result4.returncode != 0:
    print(f"FAIL: secondary repos refresh exited {result4.returncode}")
    print("STDOUT:", result4.stdout[-3000:])
    print("STDERR:", result4.stderr[-1000:])
    sys.exit(1)

# Restore sources.yml before checking (atexit will also restore, but be explicit)
shutil.copy2(_sources_backup, SOURCES_YML)
shutil.rmtree(_repos_root, ignore_errors=True)

results4 = _read_results()

_sec_entries = [r for r in results4 if r["kind"] == "secondary_repo"]
check("secondary repos: two entries", len(_sec_entries), 2)

_present_entry = next((r for r in _sec_entries if r["source"] == "test-org/sec-present"), None)
_missing_entry = next((r for r in _sec_entries if r["source"] == "test-org/sec-missing"), None)
assert _present_entry is not None, "FAIL: no sec-present entry"
assert _missing_entry is not None, "FAIL: no sec-missing entry"

check("sec-present: status is success", _present_entry["status"], "success")
check("sec-present: has counts", bool(_present_entry["counts"]), True)

check("sec-missing: status is failed", _missing_entry["status"], "failed")
assert "nonexistent" in _missing_entry["reason"] or "sec-missing" in _missing_entry["reason"], (
    f"FAIL: sec-missing reason should name the missing path, got: {_missing_entry['reason']!r}"
)
print("PASS: sec-missing: reason names the missing path")

# Two tracker entries from two-teams fixture are also present
_tr4 = [r for r in results4 if r["kind"] == "tracker"]
check("secondary repos run: two tracker entries", len(_tr4), 2)

print("\nTest 4 passed: secondary repo success + failed")
print("\nall source_results tests passed")
