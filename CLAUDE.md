# bd-knowledge-graph-base — guide for agents

This is the **template** every BuildDown knowledge graph is created from (`bd-kg-create`
copies it). Changes here reach every derived KG on its next `git merge upstream/main`.

## Branches

- `testing` is the working branch — PRs target it. The orchestrator's KGB mapping bases on
  `testing`. `main` is the release line. Keep `testing` ≥ `main` (a stale `testing` strands
  pipeline PRs — it happened once, 2026-08-19).

## Tests

Plain-python scripts, not pytest. Run per file with raw exit codes:
`PYTHONPATH=. ./.venv/bin/python tests/<file>.py`. Provision first:
`python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt` (py3.10+).
Template changes must pass under the default namespace AND a custom one (KGB-6 adds the CI
gate; until then run both legs by hand).

## The learnings loop (how this base improves)

Downstream KG teams submit sanitized learning notes as PRs into `testing`
(`learnings/YYYY/MM/DD-<slug>.md`). Each gets one disposition — **accept** (merge + KGB
issue for the base change), **decline** (merge + justification in the PR comment, no
issue), **defer** (merge + Backlog issue). Declined notes still merge: the diagnosis is the
valuable part. Full rules: `CONTRIBUTING.md` → *Evaluating a learning*.

## Hard rules

- This repo will be public: **no** ticket contents, internal names beyond a project slug,
  private code, or graph extracts — patterns only (CONTRIBUTING).
- Never commit a virtualenv (`.venv*/` is ignored — pipeline runners create alternate-named venvs).
- Base first, always: a mechanism lands here, then derived KGs port it.
