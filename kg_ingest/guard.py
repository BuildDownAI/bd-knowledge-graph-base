"""Snapshot push guard — evaluates part-level line-count changes before the rail does.

Rules (matching the orchestrator's kg-snapshot-push step):
  1. Zero-shrink: issue.nt and comment.nt must not shrink at all when issue_count > 0.
     When issue_count == 0 both fall back to the threshold rule.
  2. Threshold: every part must remain at or above shrink_threshold (default 0.5) of
     its previous line count. A part that disappears counts as 0 lines.

Exit codes: 0 = pass, 3 = refuse.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


_ZERO_SHRINK_PARTS = frozenset({"issue.nt", "comment.nt"})


@dataclass
class PartRow:
    part: str
    prev: int
    new: int
    ratio: Optional[float]  # None when prev == 0 (new part)
    status: str  # "pass" | "refuse" | "new"


@dataclass
class Verdict:
    verdict: str  # "pass" | "refuse"
    rows: list[PartRow]
    refusal: Optional[str]


def evaluate(
    previous: dict[str, int],
    new: dict[str, int],
    *,
    issue_count: int = 0,
    shrink_threshold: float = 0.5,
) -> Verdict:
    """Evaluate snapshot part counts against push-guard rules.

    Keys in `previous` and `new` are filenames including the .nt extension
    (e.g. "comment.nt").  A part absent from `new` is treated as 0 lines.
    """
    rows: list[PartRow] = []
    refusal: Optional[str] = None

    all_parts = sorted(set(previous) | set(new))
    for part in all_parts:
        prev_count = previous.get(part, 0)
        new_count = new.get(part, 0)

        if prev_count == 0:
            rows.append(PartRow(part=part, prev=0, new=new_count, ratio=None, status="new"))
            continue

        ratio = new_count / prev_count
        zero_shrink_active = part in _ZERO_SHRINK_PARTS and issue_count > 0

        refused = False
        if zero_shrink_active:
            if new_count < prev_count:
                refused = True
                if refusal is None:
                    refusal = (
                        f"{part}: shrank from {prev_count} to {new_count} lines"
                        f" (zero-shrink rule; issue_count={issue_count})"
                    )
        else:
            if new_count < prev_count * shrink_threshold:
                refused = True
                if refusal is None:
                    pct = ratio * 100
                    refusal = (
                        f"{part}: shrank from {prev_count} to {new_count} lines"
                        f" ({pct:.1f}% of previous, below {shrink_threshold * 100:.0f}% threshold)"
                    )

        rows.append(PartRow(
            part=part,
            prev=prev_count,
            new=new_count,
            ratio=ratio,
            status="refuse" if refused else "pass",
        ))

    verdict = "refuse" if refusal else "pass"
    return Verdict(verdict=verdict, rows=rows, refusal=refusal)


def count_parts_dir(d: Path) -> dict[str, int]:
    """Count non-empty lines per .nt file in a snapshot/parts directory."""
    return {
        f.name: sum(1 for line in f.read_text(encoding="utf-8").splitlines() if line.strip())
        for f in sorted(d.glob("*.nt"))
    }


def _count_parts_gitref(gitref_path: str) -> dict[str, int]:
    """Resolve counts from a '<git-ref>:<tree-path>' string."""
    ref, _, tree_path = gitref_path.partition(":")
    tree_path = tree_path.rstrip("/")

    ls = subprocess.run(
        ["git", "ls-tree", "--name-only", ref, tree_path + "/"],
        capture_output=True,
        text=True,
    )
    if ls.returncode != 0:
        raise SystemExit(f"[guard] cannot list {gitref_path}: {ls.stderr.strip()}")

    counts: dict[str, int] = {}
    for full_path in ls.stdout.splitlines():
        full_path = full_path.strip()
        if not full_path.endswith(".nt"):
            continue
        basename = Path(full_path).name
        show = subprocess.run(
            ["git", "show", f"{ref}:{full_path}"],
            capture_output=True,
            text=True,
        )
        if show.returncode != 0:
            counts[basename] = 0
        else:
            counts[basename] = sum(
                1 for line in show.stdout.splitlines() if line.strip()
            )
    return counts


def _resolve_parts(arg: str) -> dict[str, int]:
    if ":" in arg:
        return _count_parts_gitref(arg)
    return count_parts_dir(Path(arg))


def _print_table(v: Verdict) -> None:
    header = f"{'part':<20} {'prev':>7} {'new':>7} {'ratio':>7}  status"
    print(header)
    print("-" * len(header))
    for row in v.rows:
        ratio_s = f"{row.ratio * 100:.1f}%" if row.ratio is not None else "  (new)"
        status_s = row.status.upper() if row.status == "refuse" else row.status
        print(f"{row.part:<20} {row.prev:>7} {row.new:>7} {ratio_s:>7}  {status_s}")
    print()
    if v.refusal:
        print(f"REFUSED: {v.refusal}")
    else:
        print("PASS")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="kg-ingest guard",
        description=(
            "Evaluate snapshot/parts line-count changes against the push-guard rules. "
            "Exits 0 on pass, 3 on refusal."
        ),
    )
    ap.add_argument(
        "--previous",
        required=True,
        metavar="DIR_OR_REF",
        help=(
            "Previous snapshot/parts: a filesystem directory or "
            "'<git-ref>:snapshot/parts' (e.g. HEAD~1:snapshot/parts)."
        ),
    )
    ap.add_argument(
        "--new",
        required=True,
        metavar="DIR",
        help="New snapshot/parts directory.",
    )
    ap.add_argument(
        "--issue-count",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Number of issues the tracker reported this run. "
            "When > 0, issue.nt and comment.nt must not shrink at all (default: 0)."
        ),
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON {verdict, rows, refusal} instead of the human-readable table.",
    )
    args = ap.parse_args(argv)

    previous = _resolve_parts(args.previous)
    new_counts = count_parts_dir(Path(args.new))

    v = evaluate(previous, new_counts, issue_count=args.issue_count)

    if args.json:
        rows_out = [asdict(r) for r in v.rows]
        print(json.dumps({"verdict": v.verdict, "rows": rows_out, "refusal": v.refusal}))
    else:
        _print_table(v)

    return 3 if v.verdict == "refuse" else 0


if __name__ == "__main__":
    sys.exit(main())
