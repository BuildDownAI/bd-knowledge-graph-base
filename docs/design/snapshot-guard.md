# Snapshot Guard

`kg-ingest guard` evaluates per-part line-count changes for a snapshot push before
the orchestrator rail does.  A KG repo can run this check locally, in CI, or in any
pipeline step — it always gives the same verdict as the rail.

## The two rules

### 1. Zero-shrink (issue.nt and comment.nt)

When `issue_count > 0` (the tracker reported at least one issue this run), `issue.nt`
and `comment.nt` must **not shrink at all**.  Even a one-line reduction refuses.

**Why zero-shrink?**  Tracker data is authoritative and append-heavy.  A comment or
issue disappearing almost always means a data-fetch error, not a real deletion.
Allowing any reduction would silently lose knowledge that took months to accumulate.
The 50% threshold used for code-derived parts is too permissive here.

When `issue_count == 0` (no tracker data this run) both parts fall back to the 50%
threshold rule — the caller opted out of tracker ingestion, so zero-shrink would block
every run.

### 2. Threshold (every part)

Every part that existed in the previous snapshot must remain at or above
`shrink_threshold` (default **50%**) of its previous line count.  A part that
disappears entirely counts as 0 lines and always refuses.

The check is strict `<`: a part at exactly 50% passes.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Pass — all parts within rules |
| 3    | Refuse — at least one part violated a rule |

Exit code 3 (not 1) avoids colliding with the shell's "general error" convention and
lets CI distinguish a guard refusal from a guard crash.

## Usage

```
kg-ingest guard --previous <dir-or-ref> --new <dir> [--issue-count N] [--json]
```

`--previous` accepts either a filesystem directory of `.nt` files or a git reference
in the form `<ref>:snapshot/parts` (e.g. `HEAD~1:snapshot/parts`).

`--issue-count` defaults to 0 (zero-shrink inactive).  CI must pass the value from
the tracker run explicitly.

`--json` emits `{ "verdict": "pass"|"refuse", "rows": [...], "refusal": "..." | null }`.

## The one sanctioned way past a zero-shrink refusal

A deliberate reclassification (e.g. migrating comments to a different part type) will
produce a legitimate shrink that the zero-shrink rule refuses.  The only authorised
bypass is the orchestrator's **accept-new-baseline** mechanism ([AII-628][aii628]):
the operator flags the snapshot as a new baseline in the orchestrator, which skips the
line-count comparison for that push and records the new counts as the reference point.

There is no `--force` flag in `kg-ingest guard`.  The guard's job is to catch
accidents; intentional changes go through the baseline process.

[aii628]: https://linear.app/eudoxus/issue/AII-628

## Golden table: how to read a delta, when to update it

`tests/fixtures/snapshot-golden.json` is a checked-in map of `"<part>.nt": <line-count>`
for a full ingest of the fixture repo plus `tests/fixtures/tracker-data.json` under the
default namespace. `tests/test_snapshot_delta.py` compares the actual line counts from a
fresh ingest to this table; any difference fails the test with a printed delta table:

```
part           golden   actual    delta
--------------------------------------
comment.nt         80       58      -22
person.nt          19       13       -6
```

**How to read the delta.** Each row names the part whose line count changed. A negative
delta means triples were removed (e.g. a classifier no longer emits certain comment nodes);
a positive delta means new triples were added. The delta directly corresponds to the change
in `snapshot/parts/<part>.nt` that will land in the downstream KG repos after the next
refresh.

**When to update the golden.** Any PR that changes a classifier outcome, adds a new
comment kind, or modifies the fixture data must also update the golden by running:

```
PYTHONPATH=. python tests/test_snapshot_delta.py --write
```

This is the only sanctioned way to change `snapshot-golden.json`. The PR body must state
the snapshot delta (the `part | golden | actual | delta` table from the failing test output)
so reviewers can see the impact at a glance.

**PR-derived parts are absent from the fixture golden by design.** The test calls
`add_spine(..., max_prs=0)`, so no `pr.nt` part appears in the fixture golden. This
is intentional — PR ingestion requires `gh` and live GitHub access, which are not
available in the unit-test environment.
