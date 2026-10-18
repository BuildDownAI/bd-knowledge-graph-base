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
JIRA_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "tracker-data-jira.json"
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

_docs_entry = next((r for r in results if r["kind"] == "docs_site"), None)
assert _docs_entry is not None, "FAIL: no docs_site entry in source-results.json"
check("--no-docs-sites: status is skipped", _docs_entry["status"], "skipped")
check("--no-docs-sites: reason names the flag", _docs_entry["reason"], "--no-docs-sites")
# no docs_sites configured → wildcard source
check("--no-docs-sites: source is docs_site:*", _docs_entry["source"], "docs_site:*")
check("--no-docs-sites: kind is docs_site", _docs_entry["kind"], "docs_site")

_self_entry = next((r for r in results if r["kind"] == "self"), None)
assert _self_entry is not None, "FAIL: no self entry in source-results.json"
check("self_ingest: false: status is skipped", _self_entry["status"], "skipped")
check("self_ingest: false: source is self:*", _self_entry["source"], "self:*")
check("self_ingest: false: kind is self", _self_entry["kind"], "self")

_spine_entry = next((r for r in results if r["kind"] == "code_repo"), None)
assert _spine_entry is not None, "FAIL: no code_repo entry in source-results.json"
check("code_repo entry status is success", _spine_entry["status"], "success")
check("code_repo entry kind is code_repo", _spine_entry["kind"], "code_repo")
check("code_repo entry source is code_repo:<slug>", _spine_entry["source"], "code_repo:your-org/your-project")

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
team_sources3 = {r["source"] for r in tracker3}
check("two-teams: tracker:TST present", "tracker:TST" in team_sources3, True)
check("two-teams: tracker:EXT present", "tracker:EXT" in team_sources3, True)

tst = next(r for r in tracker3 if r["source"] == "tracker:TST")
ext = next(r for r in tracker3 if r["source"] == "tracker:EXT")
check("two-teams: TST issues count", tst["counts"]["issues"], 2)
check("two-teams: EXT issues count", ext["counts"]["issues"], 3)
check("two-teams: TST kind is tracker", tst["kind"], "tracker")
check("two-teams: EXT kind is tracker", ext["kind"], "tracker")

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

_present_entry = next((r for r in _sec_entries if r["source"] == "secondary_repo:test-org/sec-present"), None)
_missing_entry = next((r for r in _sec_entries if r["source"] == "secondary_repo:test-org/sec-missing"), None)
assert _present_entry is not None, "FAIL: no sec-present entry (expected source='secondary_repo:test-org/sec-present')"
assert _missing_entry is not None, "FAIL: no sec-missing entry (expected source='secondary_repo:test-org/sec-missing')"

check("sec-present: status is success", _present_entry["status"], "success")
check("sec-present: kind is secondary_repo", _present_entry["kind"], "secondary_repo")
check("sec-present: source is secondary_repo:<slug>", _present_entry["source"], "secondary_repo:test-org/sec-present")
check("sec-present: has counts", bool(_present_entry["counts"]), True)

check("sec-missing: status is failed", _missing_entry["status"], "failed")
check("sec-missing: kind is secondary_repo", _missing_entry["kind"], "secondary_repo")
check("sec-missing: source is secondary_repo:<slug>", _missing_entry["source"], "secondary_repo:test-org/sec-missing")
assert "nonexistent" in _missing_entry["reason"] or "sec-missing" in _missing_entry["reason"], (
    f"FAIL: sec-missing reason should name the missing path, got: {_missing_entry['reason']!r}"
)
print("PASS: sec-missing: reason names the missing path")

# Two tracker entries from two-teams fixture are also present
_tr4 = [r for r in results4 if r["kind"] == "tracker"]
check("secondary repos run: two tracker entries", len(_tr4), 2)

print("\nTest 4 passed: secondary repo success + failed")

# ---- Test 5: raising secondary repo does not stop the run ----
(AI_OUTPUT_DIR / "source-results.json").unlink(missing_ok=True)

_SOURCES_TWO_SECS = """\
namespace: https://example.org/kg/
code_repo:
  slug: your-org/your-project
  doc_globs:
    - docs/solutions/**/*.md
self_ingest: false
secondary_repos:
  - slug: test-org/sec-raise
    path: ./
  - slug: test-org/sec-ok
    path: ./
trackers: []
"""
SOURCES_YML.write_text(_SOURCES_TWO_SECS, encoding="utf-8")
_repos_root5 = Path(tempfile.mkdtemp(prefix="kg-sec-repos5-"))
(_repos_root5 / "sec-raise").symlink_to(REPO_ROOT)
(_repos_root5 / "sec-ok").symlink_to(REPO_ROOT)

try:
    import kg_ingest.spine as _spine_mod
    import kg_ingest.cli as _cli_mod
    from unittest.mock import patch

    _real_add_spine = _spine_mod.add_spine

    def _raising_add_spine(g, path, slug, **kw):
        if slug == "test-org/sec-raise":
            raise RuntimeError("deliberate raise for sec-raise")
        return _real_add_spine(g, path, slug, **kw)

    with patch.object(_spine_mod, "add_spine", side_effect=_raising_add_spine):
        rc5 = _cli_mod.main([
            "--repo", str(REPO_ROOT),
            "--no-embed", "--no-docs-sites",
            "--max-prs", "0", "--max-commits", "5",
            "--repos-root", str(_repos_root5),
        ])

    results5 = _read_results()
    _sec_raise_entry = next((r for r in results5 if r["source"] == "secondary_repo:test-org/sec-raise"), None)
    _sec_ok_entry = next((r for r in results5 if r["source"] == "secondary_repo:test-org/sec-ok"), None)

    assert _sec_raise_entry is not None, "FAIL: no sec-raise entry in results"
    assert _sec_ok_entry is not None, "FAIL: no sec-ok entry in results"

    check("raising-source: run continues", rc5 is not None, True)
    check("raising-source: failed source has status failed", _sec_raise_entry["status"], "failed")
    check("raising-source: failed source has reason",
          "deliberate raise for sec-raise" in _sec_raise_entry["reason"], True)
    check("raising-source: ok source is success", _sec_ok_entry["status"], "success")

    print("\nTest 5 passed: raising secondary does not stop the run")
finally:
    shutil.copy2(_sources_backup, SOURCES_YML)
    shutil.rmtree(_repos_root5, ignore_errors=True)

# ---- Test 6: tracker id uses team field on both success and failure ----
(AI_OUTPUT_DIR / "source-results.json").unlink(missing_ok=True)

# 6a: success path derives source from team field (not identifier prefix)
result6a = _run_refresh("--tracker-data", str(JIRA_FIXTURE))
if result6a.returncode != 0:
    print(f"FAIL: jira tracker refresh exited {result6a.returncode}")
    print("STDOUT:", result6a.stdout[-3000:])
    print("STDERR:", result6a.stderr[-1000:])
    sys.exit(1)

results6a = _read_results()
_tr6a = [r for r in results6a if r["kind"] == "tracker"]
assert _tr6a, "FAIL: no tracker entries in 6a"
check("tracker-id: team field used on success",
      any(r["source"] == "tracker:acme-app" for r in _tr6a), True)

# 6b: failure path uses the same team field → same source as success
(AI_OUTPUT_DIR / "source-results.json").unlink(missing_ok=True)

import kg_ingest.tracker as _tracker_mod

def _raising_add_tracker(*args, **kw):
    raise RuntimeError("deliberate tracker failure for id test")

with patch.object(_tracker_mod, "add_tracker", side_effect=_raising_add_tracker):
    _cli_mod.main([
        "--repo", str(REPO_ROOT),
        "--no-embed", "--no-docs-sites",
        "--max-prs", "0", "--max-commits", "5",
        "--tracker-data", str(JIRA_FIXTURE),
    ])

results6b = _read_results()
_tr6b = [r for r in results6b if r["kind"] == "tracker"]
assert _tr6b, "FAIL: no tracker entries in 6b"
check("tracker-id: same source on failure",
      all(r["source"] == "tracker:acme-app" for r in _tr6b), True)
check("tracker-id: failure status is failed",
      all(r["status"] == "failed" for r in _tr6b), True)

print("\nTest 6 passed: tracker id uses team field on success and failure")

# ---- Test 7: non-list tracker-data gives tracker:* failed and no crash ----
(AI_OUTPUT_DIR / "source-results.json").unlink(missing_ok=True)

_nonlist_file = Path(tempfile.mktemp(suffix="-nonlist.json"))
_nonlist_file.write_text('{"key": "value"}', encoding="utf-8")

try:
    rc7 = _cli_mod.main([
        "--repo", str(REPO_ROOT),
        "--no-embed", "--no-docs-sites",
        "--max-prs", "0", "--max-commits", "5",
        "--tracker-data", str(_nonlist_file),
    ])
    results7 = _read_results()
    _tr7 = [r for r in results7 if r["kind"] == "tracker"]
    assert _tr7, "FAIL: no tracker entries in test 7"
    check("non-list tracker-data: status is failed", _tr7[0]["status"], "failed")
    check("non-list tracker-data: source is tracker:*", _tr7[0]["source"], "tracker:*")
    check("non-list tracker-data: no crash", rc7 is not None, True)
    print("\nTest 7 passed: non-list tracker-data gives tracker:* failed")
finally:
    _nonlist_file.unlink(missing_ok=True)

# ---- Test 8: exception after source loop still writes source-results.json ----
(AI_OUTPUT_DIR / "source-results.json").unlink(missing_ok=True)

import kg_ingest.snapshot as _snapshot_mod

def _raising_write_snapshot(*args, **kw):
    raise RuntimeError("deliberate post-loop failure for finally test")

with patch.object(_snapshot_mod, "write_snapshot", side_effect=_raising_write_snapshot):
    try:
        _cli_mod.main([
            "--repo", str(REPO_ROOT),
            "--no-embed", "--no-docs-sites",
            "--max-prs", "0", "--max-commits", "5",
        ])
    except RuntimeError:
        pass  # expected: outer except re-raises after appending ingest:* entry

results8 = _read_results()
_ingest_entry = next((r for r in results8 if r["source"] == "ingest:*"), None)
assert _ingest_entry is not None, "FAIL: no ingest:* entry after post-loop exception"
check("finally-write: ingest:* entry present after forced post-loop exception",
      _ingest_entry is not None, True)
check("finally-write: ingest:* status is failed", _ingest_entry["status"], "failed")
check("finally-write: ingest:* reason present", bool(_ingest_entry["reason"]), True)

print("\nTest 8 passed: finally-write after post-loop exception")

print("\nall source_results tests passed")
