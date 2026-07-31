# Design note: the ingest process

**Status:** living reference · **Last updated:** 2026-07-28

How `kg_ingest` turns source systems into the graph, why it rebuilds the way it
does, which tracker relationships it captures, and what to add when the tracker
is Jira instead of Linear. Read this before changing the ingest, and when
standing up a KG for a **new** repo/tracker from scratch.

Entry point: `python -m kg_ingest.cli --repo ../<repo> [--tracker] [--secondary]`.
Sources are declared in [`sources.yml`](../../sources.yml).

---

## 1. Two layers, one graph

Every source is written into one of two layers (kept as separate named graphs in
the TriG so provenance stays auditable):

- **Spine** — deterministic facts from an authoritative API/VCS: code files,
  commits, PRs, and tracker **Issues** with their status/labels/branch and the
  **issue↔issue relationship graph**. `kg_ingest/spine.py` + `kg_ingest/tracker.py`.
- **Semantic** — provenanced interpretation: a `kg:Learning` or `kg:Decision`
  node per substantive tracker comment (the build-up/down learnings are the gold
  here), each `prov:wasDerivedFrom` its Issue and `prov:wasGeneratedBy` a run.
  `kg_ingest/semantic.py` + the comment loop in `tracker.py`.

The **loop to code** is closed on both ends: the spine reference-scanner emits
`<pr|commit> kg:references <issue>` whenever a PR/commit message mentions a
tracker key (e.g. `a tracker issue`), and the tracker ingester fleshes out that same
Issue node with its tracker edges. That stitching is what makes it a graph and
not two disconnected lists.

---

## 2. Update strategy: **full rebuild, not incremental** (deliberate)

Each run creates an empty `Dataset()`, re-reads every source, and **overwrites**
`out/graph.trig` (`cli.py`). There is no "what changed since last run" diff.

This is intentional. It works because **every node IRI is derived
deterministically from a stable key** — issue identifier, commit SHA, PR number,
`comment(issue, index)`. So a rebuild lands on the *same* nodes every time: no
duplication, no drift, idempotent by construction.

**Why full rebuild beats incremental append here:**

| Concern | Full rebuild | Incremental append |
|---|---|---|
| Duplicate nodes | Impossible (stable IRIs) | Needs upsert logic |
| **Retracting stale facts** (label removed, status changed, relation deleted) | Automatic — the fact simply isn't re-emitted | Hard — appended triples must be actively *deleted* |
| Complexity | One code path | Diff engine + deletion bookkeeping + `updatedAt` cursors |
| Cost today | ~10k quads, ~2 min | Not worth the complexity yet |

**The semantic embeddings sidecar rebuilds with the graph.** `kg_ingest.cli`
rebuilds `out/embeddings.npz` by default at the end of every run (immediately
before the snapshot, so the digest's Semantic-index line reflects the same run's
vectors), so semantic/hybrid search never drifts from the graph. It is skipped
gracefully when `fastembed` isn't installed — the core ingest stays
dependency-light — and `--no-embed` opts out explicitly. See
[`semantic-search.md`](semantic-search.md) and [`hybrid-search.md`](hybrid-search.md).

**When to revisit (the crossover point):**

- The corpus grows to **thousands of issues / tens of thousands of quads**, where
  re-pulling the whole tracker each run is slow or hits API rate limits. Linear's
  API supports an `updatedAt` filter and GitHub supports `since` — an incremental
  path would page only changed items into a per-run delta graph.
- You want **temporal provenance** — "when did this fact first appear / change" —
  which a rebuild throws away. That argues for append-with-versioning (a new
  named graph per run) or an enterprise store (see §5).

Until one of those bites, **keep the rebuild.** Correct-and-simple wins.

---

## 3. Tracker relationships captured (issue ↔ issue)

`tracker.py` pulls Linear's `parent`, `relations`, and `project` and maps them to
provider-neutral predicates. Verified present in the current graph:

| Source (Linear) | Graph predicate | Meaning | Count (2026-07-28) |
|---|---|---|---|
| `parent` (sub-issue) | `kg:partOf` | child → parent | 852 |
| relation `blocks` | `kg:dependsOn` | *other* depends on *this* (reverse) | 115 |
| relation `duplicate` | `kg:duplicateOf` | this → canonical | 3 |
| relation (other) | `kg:relatedTo` | soft link | 125 |
| `project` | `kg:inProject` | issue → project | 232 |

`kg:partOf` is what makes the **feature-branch grouping tree** queryable — you can
walk a parent to all its AI-Implement children, or follow `kg:dependsOn` to see
what blocks what. These predicates are **provider-neutral** (see §4) — only the
*fetch + map* in `tracker.py` is Linear-specific.

Lifecycle labels (`AI-Implement`, `Plan-Complete`, `Ready for Review`, …) are
kept as `kg:label` literals but deliberately **not** promoted to `kg:tagged`
topics — they are pipeline state, not subject matter, and would swamp real
topics. Tune via `_LIFECYCLE_LABELS` in `tracker.py`.

---

## 4. Learnings comments — scanned on **every** board

The build-up/down learnings are a text convention (from the `bd-build-up` /
`bd-build-down` skills) that works identically on Linear and Jira. The ingester
detects them in `_classify`:

- Marker (`ai-implement-build-up-learnings` / `-build-down-learnings`, + hyphen
  variants) on the comment's **first line** → `kg:Learning`. First-line only is
  deliberate: matching the marker *anywhere* in the body over-catches
  planning/summary comments that merely mention it (they become decisions
  instead). Keeps `_classify` aligned with `_comment_title`. Covered by
  `tests/test_classify.py`.
- Any other comment ≥ `_DECISION_MIN_CHARS` (400) of human rationale →
  `kg:Decision`.

This runs for **every team listed in `sources.yml`**, primary and secondary —
both **AII** and **BDS** boards are covered today. When you add a new board, add
it to `sources.yml` and its learnings are picked up with no code change.

> Invariant to preserve for any new tracker: each board's learnings comments MUST
> be scanned. The marker text is tracker-agnostic, so this is a fetch concern, not
> a detection concern — reuse `_classify` / `_comment_title` verbatim.

---

## 5. Adding Jira (and other trackers)

The graph **vocabulary is already provider-neutral** — `kg:Issue`, `kg:partOf`,
`kg:dependsOn`, `kg:duplicateOf`, `kg:relatedTo`, `kg:inProject`, `kg:Learning`,
`kg:Decision` say nothing about Linear. Only `tracker.py`'s fetch+map is
Linear-shaped (GraphQL + Linear's relation-type names). A Jira ingester is
therefore a **new fetch layer onto the same targets**:

1. **Fetch** via Jira REST (`/rest/api/3/search` with JQL per configured
   project), paginating like `_fetch_team_issues`.
2. **Map relationships to the same predicates:**
   - parent / Epic Link / sub-task → `kg:partOf`
   - issue link `blocks` → `kg:dependsOn`; `duplicates` → `kg:duplicateOf`;
     `relates to` → `kg:relatedTo`
   - project → `kg:inProject`
3. **Reuse `_classify` / `_comment_title` unchanged** on Jira comment bodies —
   the learning markers are identical.
4. **Declare it in `sources.yml`** as `- kind: jira` alongside the existing
   `- kind: linear` entries; `add_tracker` already dispatches per `kind` (today it
   `continue`s on non-`linear` — that branch becomes the Jira call).

Net: parent/child, dependencies, and learnings behave the same way on Jira as on
Linear because they resolve to the same predicates and the same detector.

---

## 5a. Reviewing data changes in git — digest + broken-up parts

The runtime graph `out/graph.trig` (~1.8 MB) is a **gitignored build artifact**:
one monolith the MCP server loads, rewritten wholesale each run. To review *data*
changes over time without committing that blob, every ingest also writes two
small, **committed** artifacts under `snapshot/` (see `kg_ingest/snapshot.py`,
also runnable standalone as `python -m kg_ingest.snapshot`):

- **`snapshot/digest.md`** — a compact, human-readable summary: node counts by
  type, issues per board, relationship-edge counts, the learnings inventory, and
  a **one-line-per-issue table** (`KEY | status | project | labels | parent |
  learnings`). A status change or a new learning is a clean 1-line diff.
- **`snapshot/parts/<type>.nt`** — the whole graph **split by subject type**
  (issue, comment, file, commit, pr, topic, person, …) into sorted N-Triples.

**Why the split answers "only update the parts that changed":** a normal tracker
refresh only rewrites `issue.nt` / `comment.nt`; a code change only rewrites
`file.nt` / `commit.nt` / `pr.nt`. Because there are **no blank nodes** and every
file is **sorted**, an unchanged partition serializes **byte-identically** — git
shows nothing for it. The split is physical (separate files), so the diff is
localized even though the ingest still does a full in-memory rebuild (§2).

`cat snapshot/parts/*.nt` reconstitutes the full graph, so the parts also serve as
a checked-in, diffable **backup** that can rebuild `out/graph.trig` offline.

> Determinism rules the snapshot must keep: **no timestamps anywhere**, sort every
> file, and never introduce blank nodes upstream. One known churn source: the
> semantic run-id embeds a fingerprint of the solution-doc filenames, so changing
> those docs rewrites the `wasGeneratedBy` edges in `comment.nt` even if a comment
> didn't change. That is a real "new extraction run" fact; a tracker-only refresh
> (docs unchanged) leaves the run-id — and those parts — untouched.

## 6. For later: enterprise ingest tooling (Stardog etc.)

The current ingest is a hand-rolled Python rebuild → TriG file. That is right for
this scale, but formal graph platforms offer more sophisticated ingest worth
exploring as the corpus and number of sources grow. This KG already ships a
Stardog **query** backend (`KG_BACKEND=stardog` in `kg_query/store.py`); the
*ingest* side is what these tools would upgrade:

- **Stardog Virtual Graphs** — map a live source (relational DB, or an API via a
  mapping layer) directly to graph triples with **no ETL copy**, so the graph
  reflects the source without a rebuild step.
- **Incremental / transactional loads + named-graph versioning** — true deltas
  and temporal provenance (the §2 crossover motivations) as first-class features.
- **Stardog Designer / managed pipelines / connectors** (incl. Databricks) — GUI
  mapping and scheduled ingestion instead of a cron'd script.

None of this is needed now. The trigger to evaluate it: multiple large sources,
a need for near-real-time freshness, or a need for time-travel over the graph.
Revisit here when one of those becomes real.

---

## 7. Standing up a KG for a new repo/tracker from scratch

The reusable recipe (this repo is the worked example):

1. Copy the `kg_ingest` / `kg_query` / `ontology` structure; keep the two-layer
   spine/semantic split and the deterministic-IRI rule (§2) — they are what make
   rebuilds safe.
2. Edit **`sources.yml`**: point `code_repo` at the new repo, list its `doc_globs`,
   and declare the `trackers` (Linear teams and/or Jira projects) with `tier:`.
3. If the tracker is Jira, add the fetch layer per §5; the target predicates and
   the learnings detector carry over unchanged.
4. Run `--tracker --secondary`, confirm `SHACL validation → conforms: True`, and
   spot-check relationship counts (§3) and learnings-by-board (§4).
5. Register the read-only MCP server (`.mcp.json.example` → `.mcp.json` via
   `setup.sh`). Remember the graph loads **once at server start** — re-run ingest,
   then restart the MCP client to serve the new graph.
