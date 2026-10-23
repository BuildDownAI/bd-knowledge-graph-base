"""Contract test: golden source-results.json files produced by the real ingest path.

Each test drives kg_ingest.cli.main() with a synthetic single-commit fixture repo
so counts are deterministic across PRs. Compares the written file to the golden
under tests/fixtures/source-results/.

Run normally:   pytest tests/test_source_results_contract.py
Update goldens: KG_UPDATE_GOLDEN=1 pytest tests/test_source_results_contract.py
"""
import difflib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
AI_OUTPUT_DIR = REPO_ROOT / "ai-output"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "source-results"
KGA7_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "tracker-data-kga7.json"
SOURCES_YML = REPO_ROOT / "sources.yml"

_SOURCES_MINIMAL = """\
namespace: https://example.org/kg/
code_repo:
  slug: your-org/your-project
  doc_globs:
    - docs/solutions/**/*.md
    - docs/adr/*.md
    - docs/plans/*.md
self_ingest: false
secondary_repos: []
trackers: []
"""

_SOURCES_WITH_DOCS_SITE = """\
namespace: https://example.org/kg/
code_repo:
  slug: your-org/your-project
  doc_globs:
    - docs/solutions/**/*.md
    - docs/adr/*.md
    - docs/plans/*.md
self_ingest: false
secondary_repos: []
trackers: []
docs_sites:
  - url: https://docs.example.com/
"""

_SOURCES_WITH_SECONDARY = """\
namespace: https://example.org/kg/
code_repo:
  slug: your-org/your-project
  doc_globs:
    - docs/solutions/**/*.md
    - docs/adr/*.md
    - docs/plans/*.md
self_ingest: false
secondary_repos:
  - slug: test-org/sec-missing
    path: ../nonexistent-missing-dir-kgb45
trackers: []
"""


def _make_fixture_repo(tmp: Path) -> Path:
    """Minimal single-commit git repo for deterministic counts."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"],
                   check=True, capture_output=True)
    (repo / "a.md").write_text("# A\nContent of a.")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.md").write_text("# B\nContent of b.")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"],
                   check=True, capture_output=True)
    return repo


_FIXED_DATE = "1970-01-01T00:00:00+00:00"
_FIXED_BRANCH = "main"


def _normalize(results: list[dict]) -> list[dict]:
    """Replace run-specific fields with fixed sentinels before comparing.

    Replaced: first_commit_date, last_commit_date (wall-clock), history_branch
    (depends on init.defaultBranch git config), is_default_branch (derived).
    """
    normalized = []
    for entry in results:
        e = dict(entry)
        e.pop("timestamp", None)
        counts = dict(e.get("counts") or {})
        if "first_commit_date" in counts:
            counts["first_commit_date"] = _FIXED_DATE
        if "last_commit_date" in counts:
            counts["last_commit_date"] = _FIXED_DATE
        if "history_branch" in counts:
            counts["history_branch"] = _FIXED_BRANCH
        if "is_default_branch" in counts:
            counts["is_default_branch"] = False
        e["counts"] = counts
        normalized.append(e)
    return normalized


def _run_main(main_args: list[str]) -> list[dict]:
    """Call cli.main() with args and return normalized source-results.json content.

    Always unlinks the file first so a previous run's output is never returned.
    Swallows RuntimeError (expected from the crash scenario).
    """
    import kg_ingest.cli as _cli_mod
    AI_OUTPUT_DIR.mkdir(exist_ok=True)
    (AI_OUTPUT_DIR / "source-results.json").unlink(missing_ok=True)
    try:
        _cli_mod.main(main_args)
    except RuntimeError:
        pass
    results_file = AI_OUTPUT_DIR / "source-results.json"
    assert results_file.exists(), "source-results.json was not written"
    data = json.loads(results_file.read_text(encoding="utf-8"))
    return _normalize(data)


def _assert_matches_golden(case_name: str, got: list[dict]) -> None:
    """Compare got to the golden file; rewrite on KG_UPDATE_GOLDEN=1."""
    golden_path = FIXTURES_DIR / f"{case_name}.json"
    if os.environ.get("KG_UPDATE_GOLDEN"):
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(got, indent=2) + "\n", encoding="utf-8")
        return
    assert golden_path.exists(), (
        f"Golden file not found: {golden_path}\n"
        f"Run with KG_UPDATE_GOLDEN=1 to create it."
    )
    want = json.loads(golden_path.read_text(encoding="utf-8"))
    if got != want:
        diff = "\n".join(difflib.unified_diff(
            json.dumps(want, indent=2).splitlines(),
            json.dumps(got, indent=2).splitlines(),
            fromfile=str(golden_path.relative_to(REPO_ROOT)),
            tofile="actual",
            lineterm="",
        ))
        pytest.fail(
            f"source-results.json drifted from {golden_path.name}:\n{diff}"
        )


def test_success(snapshot_sources_backup, tmp_path):
    """Every configured source succeeds: code_repo + tracker."""
    SOURCES_YML.write_text(_SOURCES_MINIMAL, encoding="utf-8")
    repo = _make_fixture_repo(tmp_path)
    got = _run_main([
        "--repo", str(repo),
        "--no-embed", "--no-docs-sites",
        "--max-prs", "0", "--max-commits", "5",
        "--tracker-data", str(KGA7_FIXTURE),
    ])
    _assert_matches_golden("success", got)


def test_skipped(snapshot_sources_backup, tmp_path):
    """One source is skipped: configured docs_site skipped by --no-docs-sites."""
    SOURCES_YML.write_text(_SOURCES_WITH_DOCS_SITE, encoding="utf-8")
    repo = _make_fixture_repo(tmp_path)
    got = _run_main([
        "--repo", str(repo),
        "--no-embed", "--no-docs-sites",
        "--max-prs", "0", "--max-commits", "5",
    ])
    skipped = [e for e in got if e["status"] == "skipped"]
    assert skipped, "Expected at least one skipped entry"
    assert any(e["reason"] is not None for e in skipped), "Skipped entry must have a reason"
    _assert_matches_golden("skipped", got)


def test_failed(snapshot_sources_backup, tmp_path):
    """One source fails (missing secondary repo path); code_repo succeeds."""
    SOURCES_YML.write_text(_SOURCES_WITH_SECONDARY, encoding="utf-8")
    repo = _make_fixture_repo(tmp_path)
    got = _run_main([
        "--repo", str(repo),
        "--no-embed", "--no-docs-sites",
        "--max-prs", "0", "--max-commits", "5",
        "--secondary",
    ])
    failed = [e for e in got if e["status"] == "failed"]
    succeeded = [e for e in got if e["status"] == "success"]
    assert failed, "Expected at least one failed entry"
    assert succeeded, "Expected at least one succeeded entry"
    _assert_matches_golden("failed", got)


def test_ingest_crash(snapshot_sources_backup, tmp_path):
    """Post-source-loop crash adds ingest:* entry; written in finally block."""
    import kg_ingest.snapshot as _snapshot_mod

    def _raise(*args, **kw):
        raise RuntimeError("deliberate post-loop failure for contract test")

    SOURCES_YML.write_text(_SOURCES_MINIMAL, encoding="utf-8")
    repo = _make_fixture_repo(tmp_path)
    with patch.object(_snapshot_mod, "write_snapshot", side_effect=_raise):
        got = _run_main([
            "--repo", str(repo),
            "--no-embed", "--no-docs-sites",
            "--max-prs", "0", "--max-commits", "5",
        ])
    ingest_entry = next((e for e in got if e["source"] == "ingest:*"), None)
    assert ingest_entry is not None, "Expected ingest:* entry from crash"
    assert ingest_entry["kind"] == "ingest"
    assert ingest_entry["status"] == "failed"
    _assert_matches_golden("ingest-crash", got)
