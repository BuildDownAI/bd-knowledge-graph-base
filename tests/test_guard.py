"""Snapshot push guard: evaluate() rules and CLI exit codes.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_guard.py
Dual-namespace gate (KGB-6):
  KG_NAMESPACE=https://kg.acme.test/ PYTHONPATH=. ./.venv/bin/python tests/test_guard.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from kg_ingest.guard import Verdict, count_parts_dir, evaluate


def check(name: str, got, want) -> None:
    assert got == want, f"FAIL {name}: got {got!r}, want {want!r}"
    print(f"PASS: {name}")


# --------------------------------------------------------------------------- #
# Fixture: 2026-09-12 table (from AI-Implement run 34708713553)
# --------------------------------------------------------------------------- #
_PREV_2026_09_12 = {
    "commit.nt": 5000,
    "comment.nt": 9995,
    "file.nt": 3000,
    "issue.nt": 1000,
    "pr.nt": 2000,
    "topic.nt": 500,
}
_NEW_2026_09_12 = {
    "commit.nt": 5000,
    "comment.nt": 3807,   # shrank from 9995 → refused under zero-shrink
    "file.nt": 3000,
    "issue.nt": 1000,
    "pr.nt": 2000,
    "topic.nt": 500,
}


def test_2026_09_12_refuses() -> None:
    """The 2026-09-12 table refuses on comment.nt (zero-shrink, issue_count=1)."""
    v = evaluate(_PREV_2026_09_12, _NEW_2026_09_12, issue_count=1)
    check("verdict is refuse", v.verdict, "refuse")
    assert v.refusal is not None, "FAIL: refusal should not be None"
    assert "comment.nt: shrank from 9995 to 3807 lines" in v.refusal, (
        f"FAIL: refusal text does not contain expected string: {v.refusal!r}"
    )
    print("PASS: 2026-09-12 table refuses with correct message")


def test_2026_09_12_passes_when_comment_restored() -> None:
    """The same table with comment.nt new=9995 passes."""
    new = {**_NEW_2026_09_12, "comment.nt": 9995}
    v = evaluate(_PREV_2026_09_12, new, issue_count=1)
    check("verdict is pass", v.verdict, "pass")
    check("refusal is None", v.refusal, None)


def test_part_drops_to_zero_refuses() -> None:
    """A part dropping to 0 refuses regardless of issue_count."""
    prev = {"commit.nt": 500}
    new = {}  # commit.nt absent → counted as 0

    v_with_issues = evaluate(prev, new, issue_count=1)
    check("zero drop refuses with issue_count=1", v_with_issues.verdict, "refuse")
    assert v_with_issues.refusal is not None
    assert "shrank from 500 to 0 lines" in v_with_issues.refusal, (
        f"FAIL: {v_with_issues.refusal!r}"
    )
    print("PASS: part dropping to 0 refuses with issue_count=1")

    v_no_issues = evaluate(prev, new, issue_count=0)
    check("zero drop refuses with issue_count=0", v_no_issues.verdict, "refuse")
    assert v_no_issues.refusal is not None
    assert "shrank from 500 to 0 lines" in v_no_issues.refusal, (
        f"FAIL: {v_no_issues.refusal!r}"
    )
    print("PASS: part dropping to 0 refuses with issue_count=0")


def test_issue_count_zero_relaxes_zero_shrink() -> None:
    """issue_count=0 falls back to 50% threshold for comment.nt (60% passes)."""
    prev = {"comment.nt": 100}
    new = {"comment.nt": 60}   # 60% > 50% → pass

    v = evaluate(prev, new, issue_count=0)
    check("60% passes when issue_count=0", v.verdict, "pass")
    check("refusal is None when issue_count=0", v.refusal, None)


def test_exactly_50pct_passes() -> None:
    """A part at exactly 50% of previous passes (threshold is strict <)."""
    prev = {"file.nt": 100}
    new = {"file.nt": 50}  # exactly 50% → passes

    v = evaluate(prev, new, issue_count=0)
    check("exactly 50% passes", v.verdict, "pass")


def test_new_part_does_not_refuse() -> None:
    """A part that appears for the first time (no previous) gets status 'new'."""
    prev: dict[str, int] = {}
    new = {"run.nt": 42}

    v = evaluate(prev, new, issue_count=1)
    check("new part: verdict is pass", v.verdict, "pass")
    check("new part: status is new", v.rows[0].status, "new")


# --------------------------------------------------------------------------- #
# CLI tests: exit codes and --json
# --------------------------------------------------------------------------- #
def _write_parts(d: Path, counts: dict[str, int]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for name, n in counts.items():
        lines = [f"<s{i}> <p> <o> ." for i in range(n)]
        (d / name).write_text("\n".join(lines) + ("\n" if lines else ""))


def test_cli_exit_codes() -> None:
    """CLI exits 3 on refusal and 0 on pass."""
    with tempfile.TemporaryDirectory() as tmp:
        prev_dir = Path(tmp) / "prev"
        new_dir = Path(tmp) / "new"
        _write_parts(prev_dir, {"comment.nt": 9995, "commit.nt": 500})
        _write_parts(new_dir, {"comment.nt": 3807, "commit.nt": 500})

        result = subprocess.run(
            [sys.executable, "-m", "kg_ingest", "guard",
             "--previous", str(prev_dir), "--new", str(new_dir),
             "--issue-count", "1"],
            capture_output=True, text=True,
        )
        check("CLI refuse exits 3", result.returncode, 3)
        print("PASS: CLI exits 3 on refusal")

        _write_parts(new_dir, {"comment.nt": 9995, "commit.nt": 500})
        result = subprocess.run(
            [sys.executable, "-m", "kg_ingest", "guard",
             "--previous", str(prev_dir), "--new", str(new_dir),
             "--issue-count", "1"],
            capture_output=True, text=True,
        )
        check("CLI pass exits 0", result.returncode, 0)
        print("PASS: CLI exits 0 on pass")


def test_cli_json() -> None:
    """--json emits valid JSON with verdict, rows, and refusal keys."""
    with tempfile.TemporaryDirectory() as tmp:
        prev_dir = Path(tmp) / "prev"
        new_dir = Path(tmp) / "new"
        _write_parts(prev_dir, {"comment.nt": 9995})
        _write_parts(new_dir, {"comment.nt": 3807})

        result = subprocess.run(
            [sys.executable, "-m", "kg_ingest", "guard",
             "--previous", str(prev_dir), "--new", str(new_dir),
             "--issue-count", "1", "--json"],
            capture_output=True, text=True,
        )
        check("JSON CLI exits 3", result.returncode, 3)
        data = json.loads(result.stdout)
        assert "verdict" in data, f"FAIL: 'verdict' missing from {data}"
        assert "rows" in data, f"FAIL: 'rows' missing from {data}"
        assert "refusal" in data, f"FAIL: 'refusal' missing from {data}"
        check("JSON verdict", data["verdict"], "refuse")
        assert data["refusal"] is not None
        assert "comment.nt: shrank from 9995 to 3807 lines" in data["refusal"], (
            f"FAIL: {data['refusal']!r}"
        )
        print("PASS: --json emits correct structure and refusal text")


def test_count_parts_dir() -> None:
    """count_parts_dir counts non-empty lines per .nt file."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "issue.nt").write_text("<a> <b> <c> .\n<d> <e> <f> .\n")
        (d / "comment.nt").write_text("<x> <y> <z> .\n")
        counts = count_parts_dir(d)
        check("count_parts_dir issue.nt", counts["issue.nt"], 2)
        check("count_parts_dir comment.nt", counts["comment.nt"], 1)


def main() -> None:
    test_2026_09_12_refuses()
    test_2026_09_12_passes_when_comment_restored()
    test_part_drops_to_zero_refuses()
    test_issue_count_zero_relaxes_zero_shrink()
    test_exactly_50pct_passes()
    test_new_part_does_not_refuse()
    test_cli_exit_codes()
    test_cli_json()
    test_count_parts_dir()

    # Import iris only for the namespace stamp — guard.py itself never imports it.
    from kg_ingest import iris
    print(f"\n[OK] all tests passed — namespace={iris.NAMESPACE}")


if __name__ == "__main__":
    main()
