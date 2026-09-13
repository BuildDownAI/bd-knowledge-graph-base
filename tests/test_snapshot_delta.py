"""Golden snapshot-delta test: ingest the fixture and assert every part's line
count against the checked-in table (tests/fixtures/snapshot-golden.json).

A PR that changes any classifier outcome changes line counts in one or more
parts, which fails this test until the golden is regenerated in the same PR —
making the delta reviewable where the change is made.

Run:
  PYTHONPATH=. ./.venv/bin/python tests/test_snapshot_delta.py
  PYTHONPATH=. ./.venv/bin/python tests/test_snapshot_delta.py --write

--write regenerates tests/fixtures/snapshot-golden.json (the only sanctioned
way to change it). Run it twice and verify the file is byte-identical both
times before committing.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from rdflib import Graph

from kg_ingest.snapshot import write_parts
from kg_ingest.spine import add_spine
from kg_ingest.tracker import _add_issue, _bind, _run_node

_FIXTURES = Path(__file__).parent / "fixtures"
_GOLDEN = _FIXTURES / "snapshot-golden.json"
_TRACKER_DATA = _FIXTURES / "tracker-data.json"

_SLUG = "test-org/fixture"
_TEAM = "TEST"
_HEADINGS = ("AI Planning:",)
_TO_GOLDEN_KEY = lambda t: f"{t}.nt"


def _make_fixture_repo(tmp: Path) -> Path:
    """Minimal git repo with a.md, sub/b.md, latest/c.mdx (same as test_spine.py)."""
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
    (repo / "latest").mkdir()
    (repo / "latest" / "c.mdx").write_text("# C\nContent of c.")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"],
                   check=True, capture_output=True)
    return repo


def _build_union() -> Graph:
    tracker_data = json.loads(_TRACKER_DATA.read_text())
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        repo = _make_fixture_repo(tmp)

        spine_g, run_g = Graph(), Graph()
        _bind(spine_g)
        _bind(run_g)

        add_spine(spine_g, repo, _SLUG, max_commits=None, max_prs=0)

        run = _run_node(run_g, "fixture-delta-run", "0.0.0-test")
        stats = {
            "issues": 0,
            "comment_learnings": 0,
            "comment_decisions": 0,
            "comment_planning_notes": 0,
            "comment_verifications": 0,
            "comment_implementation_notes": 0,
        }
        for iss in tracker_data:
            _add_issue(spine_g, run_g, run, _TEAM, iss, stats, headings=_HEADINGS)

        union = Graph()
        for triple in spine_g:
            union.add(triple)
        for triple in run_g:
            union.add(triple)
        return union


def main() -> None:
    write_mode = "--write" in sys.argv

    union = _build_union()

    with tempfile.TemporaryDirectory() as parts_tmp:
        parts_dir = Path(parts_tmp) / "parts"
        actual_raw = write_parts(union, parts_dir)

    actual = {_TO_GOLDEN_KEY(k): v for k, v in actual_raw.items()}

    if write_mode:
        _FIXTURES.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        print(f"wrote {_GOLDEN} ({len(actual)} parts)")
        return

    if not _GOLDEN.exists():
        print("ERROR: tests/fixtures/snapshot-golden.json not found.")
        print("Run: PYTHONPATH=. python tests/test_snapshot_delta.py --write")
        sys.exit(1)

    golden = json.loads(_GOLDEN.read_text())

    all_keys = sorted(set(golden) | set(actual))
    mismatches = [
        (k, golden.get(k, 0), actual.get(k, 0))
        for k in all_keys
        if golden.get(k, 0) != actual.get(k, 0)
    ]

    if not mismatches:
        print(f"PASS: snapshot delta test ({len(actual)} parts match golden)")
        return

    col_w = max(len(k) for k, _, _ in mismatches) + 2
    print(f"\n{'part':<{col_w}} {'golden':>8} {'actual':>8} {'delta':>8}")
    print("-" * (col_w + 26))
    for part, g, a in mismatches:
        print(f"{part:<{col_w}} {g:>8} {a:>8} {a - g:>+8}")
    print()
    print("update tests/fixtures/snapshot-golden.json in this PR and state the "
          "Snapshot delta in the PR body")
    print("(run: PYTHONPATH=. python tests/test_snapshot_delta.py --write)")
    sys.exit(1)


if __name__ == "__main__":
    main()
