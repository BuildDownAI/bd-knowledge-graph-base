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
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from kg_ingest.snapshot import write_parts, write_snapshot
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


def test_write_parts_splits_with_small_cap() -> None:
    """With a tiny cap, write_parts splits into <type>.00.nt pieces; no subject spans pieces."""
    from kg_ingest.iris import NAMESPACE as NS
    g = Graph()
    s1 = URIRef(f"{NS}resource/issue/EX-1")
    s2 = URIRef(f"{NS}resource/issue/EX-2")
    rdf_type = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")
    tp = URIRef(f"{NS}onto#Issue")
    g.add((s1, rdf_type, tp))
    g.add((s2, rdf_type, tp))

    with tempfile.TemporaryDirectory() as tmp:
        parts_dir = Path(tmp) / "parts"
        result = write_parts(g, parts_dir, _cap=1)

        assert "issue" in result, f"Expected 'issue' in result keys, got: {list(result.keys())}"
        assert not (parts_dir / "issue.nt").exists(), "Expected piece files, not single issue.nt"

        piece_files = sorted(parts_dir.glob("issue.*.nt"))
        assert len(piece_files) >= 1, f"Expected piece files, got: {list(parts_dir.glob('*.nt'))}"

        seen_subjects: dict[str, str] = {}
        for pf in piece_files:
            for line in pf.read_text().splitlines():
                if not line.strip():
                    continue
                subj = line[:line.index('>') + 1]
                assert subj not in seen_subjects, (
                    f"Subject {subj} appears in {seen_subjects[subj]} and {pf.name}"
                )
                seen_subjects[subj] = pf.name

        print(f"PASS: write_parts splits into pieces: {[f.name for f in piece_files]}")


def test_write_parts_deterministic() -> None:
    """Two runs on the same graph produce byte-identical files."""
    union = _build_union()

    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        parts1 = Path(tmp1) / "parts"
        parts2 = Path(tmp2) / "parts"
        write_parts(union, parts1)
        write_parts(union, parts2)

        files1 = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                  for f in sorted(parts1.glob("*.nt"))}
        files2 = {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                  for f in sorted(parts2.glob("*.nt"))}

        assert files1 == files2, (
            f"Two runs differ: {set(files1.items()) ^ set(files2.items())}"
        )
        print(f"PASS: write_parts is deterministic ({len(files1)} files)")


def test_write_snapshot_fails_on_oversized_file() -> None:
    """write_snapshot raises SystemExit if any file in snap_dir exceeds 95 MB."""
    from kg_ingest.iris import NAMESPACE as NS
    g = Graph()
    g.add((URIRef(f"{NS}resource/issue/EX-1"), RDF.type, URIRef(f"{NS}onto#Issue")))

    with tempfile.TemporaryDirectory() as tmp:
        snap_dir = Path(tmp) / "snapshot"
        snap_dir.mkdir()
        large_file = snap_dir / "embeddings.npz"
        large_file.write_bytes(b"x" * (96 * 1024 * 1024))

        try:
            write_snapshot(g, snap_dir)
            assert False, "write_snapshot should have raised SystemExit"
        except SystemExit as exc:
            msg = str(exc)
            assert "embeddings.npz" in msg, f"exit message must name the file; got: {msg!r}"
            assert "96" in msg or "MB" in msg.upper(), (
                f"exit message should mention size; got: {msg!r}"
            )
            assert "raise _PART_SIZE_CAP" not in msg, (
                f"exit message must not advise raising _PART_SIZE_CAP; got: {msg!r}"
            )
            assert "follow-up" in msg.lower(), (
                f"exit message for embeddings.npz must mention follow-up; got: {msg!r}"
            )
        print("PASS: write_snapshot fails with clear message on oversized file")


def main() -> None:
    test_write_parts_splits_with_small_cap()
    test_write_parts_deterministic()
    test_write_snapshot_fails_on_oversized_file()

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
