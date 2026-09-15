# Base drift is invisible to a template derivative, and the refresh should propose the upstream merge rather than run blind

- **Date:** 2026-09-15
- **From:** knowledge-graph-horizon-neo
- **Area:** snapshot | portability
- **Priority (suggested):** P2

## Symptom

A derivative was fifty commits behind the template when its first rail refresh ran, and every
preflight reported `base drift unknown`. The refresh then failed on a contract the derivative
lacked, and the gap was found by hand: fetching the template into the derivative's clone and
counting commits. Nothing in the refresh path had told the operator the derivative was stale,
so nothing prompted the upstream merge that would have prevented the failure.

## Root cause

The refresh preflight measures drift with the hosting platform's cross-repository compare, which
resolves only inside a fork network. A repository created from a template is not a fork, so the
compare returns not-found and the row degrades to "unknown" on exactly the repositories it was
meant to protect. Meanwhile the git history itself does carry the answer: the creation flow pushes
the template's commits, so derivative and template share a merge-base, and a plain fetch plus a
commit count between the two heads gives the drift with no platform API at all.

There is also no step that acts on drift once known. The documented remedy is a manual session
that opens a dated `kg-upstream/` branch, merges the template, runs the proof loop, and opens a
PR. A refresh that ran blind on a stale derivative had no cheaper path to that remedy.

## Suggested base change

1. Ship a drift check in the template that works for template derivatives: a small command
   (or a step in the existing refresh entry point) that reads `base_repo:` from `sources.yml`,
   fetches the template's default branch into the clone, and prints the commit count behind plus
   the changed paths grouped by area. The rail can call it in the runner, where the clone already
   exists, instead of the compare API.
2. Ship a scheduled workflow in the template that, when drift is non-zero, opens or refreshes a
   dated `kg-upstream/` pull request against the derivative's default branch. The PR carries the
   merge, keeps the derivative's `sources.yml` and `snapshot/` untouched, and runs the test suite.
   A human merges it. The refresh never merges the template itself: the manifest conflicts on
   every merge, the snapshot must never come from the template, and untested template code should
   not run in the same ingest that produces a served graph.
3. Have the refresh report drift on the refresh PR and in status, and turn a non-zero drift into
   a warning that names the open `kg-upstream/` PR, so the operator sees the remedy next to the
   symptom. Combined with the contract check suggested in the refresh-contract note, a stale
   derivative fails loudly before ingest rather than silently after it.
