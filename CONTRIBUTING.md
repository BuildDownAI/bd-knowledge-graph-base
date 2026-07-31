# Contributing — the learnings loop

This base improves through **learning PRs** from teams operating downstream KGs.
The loop is deliberately small:

## Submitting a learning

1. **When:** the close of a KG create/refresh surfaced something base-relevant —
   an ingest failure class, a classifier miss, a portability gap, a performance
   issue. A routine, uneventful refresh produces **no** learning PR.
2. **Write one time-based note:** `learnings/YYYY/MM/DD-<project-slug>.md`
   (see [learnings/README.md](learnings/README.md) for the format).
3. **Branch + PR:** branch `kg-learnings/YYYY-MM-DD-<project-slug>` cut from
   `testing`; open a **small PR into `testing`** containing just the note (and,
   when obvious, the proposed base change alongside it).

## Sanitization rule (hard requirement)

This repo is destined to be public. A learning note must contain **no business
data**: no ticket bodies or titles, no customer or internal project names beyond
the submitting project slug, no code from private repos, no graph extracts.
Describe the *pattern* (symptom → root cause → suggested base change), never the
proprietary instance. PRs violating this are closed on sight.

## Evaluation

- **One approval from the maintainer team merges a learning PR into `testing`.**
- Maintainers periodically triage accepted learnings **by priority**, implement
  the corresponding base changes (also by priority), and promote `testing` →
  `main` through the normal release flow.
- A learning can be accepted (merged) but deliberately not implemented — the
  note itself is the durable record either way.

## Everything else

- Code PRs follow the same path: PR into `testing`, one approval, promotion to
  `main` on release.
- `main` and `testing` are protected: PRs only, no force-pushes.
- Never commit real `sources.yml` bindings, `out/`, or `snapshot/` data from a
  downstream KG to this repo.
