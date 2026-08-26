# Contributing — the learnings loop

This base improves through **learning PRs** from teams operating downstream KGs.
The loop is deliberately small. Code PRs follow the same path.

## Contributor License Agreement — required

**Before we can merge your pull request — a learning note or code alike — you need
to sign a Contributor License Agreement.** It's a one-time signature that covers
everything you contribute to BuildDown projects in future.

| You are… | Sign this |
|---|---|
| An individual contributing your own work | [Individual CLA](legal/ICLA.md) |
| Contributing on behalf of an employer, or using employer time or equipment | Your employer signs the [Corporate CLA](legal/CCLA.md), **and** you sign the Individual CLA |

When you open your first PR, the CLA bot will comment with a link. The status check blocks merge until it's signed.

**You keep ownership of your work.** The CLA is a licence, not an assignment — you can keep using, licensing and distributing your contribution however you like, including in competing projects.

**BuildDown gets a broad licence, including the right to relicense.** Section 4 of the CLA lets us distribute your contribution under a commercial or proprietary licence, change the project's licence in future, offer the project under several licences at once, and include your contribution in paid products and hosted services — without asking again and without paying you. If that's not acceptable, please don't sign and don't contribute.

**Check your employment agreement first.** If you're employed as a developer, it may assign to your employer everything you write — including on your own time and equipment. Section 5.2 of the Individual CLA asks you to represent that you're entitled to grant the licence. If your employer owns your work, we need a Corporate CLA from them instead.

Questions: **[PLACEHOLDER: cla@builddown.ai]**

## Setting up

```bash
./setup.sh          # uv sync (or venv + pip -e .), then build the graph from sources.yml
```

Python 3.10+ is required. `setup.sh` prefers `uv` (a `.venv` from `pyproject.toml` / `uv.lock`) and falls back to `venv` + `pip`. Run the tests with `PYTHONPATH=. python tests/test_*.py`, and mind the **dual-namespace test gate** documented below — both legs must be green before a PR can merge.

**Disclose third-party and AI-generated material.** If your contribution includes material under a third-party licence, or was generated in substantial part by an AI system, mark it and say so in the PR description (CLA Sections 5.4 and 5.5). This is a disclosure requirement, not a prohibition.

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

## Dual-namespace test gate

This repo is a template — every downstream KG re-homes it under its own `namespace:` in `sources.yml`. The CI suite runs the full test suite **twice**: once under the shipped default namespace (`https://example.org/kg/`) and once with `https://kg.acme.test/` injected, exercising the re-homing path. Both legs must be green before a PR can merge. Run both legs locally before claiming namespace-related validation in a commit message: first with `sources.yml` unchanged, then with the `namespace:` line replaced (e.g. via `sed -i 's|namespace: https://example.org/kg/|namespace: https://kg.acme.test/|' sources.yml`), running `PYTHONPATH=. python tests/test_*.py` after each. A change that is a no-op under the default namespace but breaks re-homing will only surface in the second leg — which is exactly the defect class this gate is designed to catch.

## Everything else

- Code PRs follow the same path: PR into `testing`, one approval, promotion to
  `main` on release.
- `main` and `testing` are protected: PRs only, no force-pushes.
- Never commit real `sources.yml` bindings, `out/`, or `snapshot/` data from a
  downstream KG to this repo.

## Evaluating a learning — the three dispositions

Every learning PR gets **one** of three dispositions from a maintainer, recorded as a PR
comment so the decision is itself searchable (the KG ingests PR comments):

| Disposition | What happens | Where it lives |
|---|---|---|
| **Accept** | The note merges into `testing` AND a tracker issue is filed in the base repo's team (KGB) for the suggested base change — the issue links the learning file; the PR comment links the issue. The base change is then built by priority like any other issue. | Note in `learnings/`, work in the tracker |
| **Decline** | The note **still merges** — a pattern we considered is knowledge even when we reject its fix — but the PR comment states the justification plainly ("declined: <why>; what we'd do instead: <alternative>"). No issue is filed. | Note in `learnings/`, decision in the PR comment |
| **Defer** | The note merges; an issue is filed in **Backlog** with the deferral reason and what would unblock it. | Note + a parked issue |

Why declined notes still merge: the expensive part of a learning is the *diagnosis*, not
the fix. Dropping the note because we reject the fix throws away the diagnosis, and the
next team rediscovers it. Merging the note with a declined-with-reason comment gives the
KG both the pattern and our reasoning.

What never merges: a note that fails sanitization (CONTRIBUTING, above) — that is
returned for a rewrite, not declined.

Precedent: `learnings/2026/07/31-knowledge-graph-ai-implement.md` → **accepted** → KGB-6
(dual-namespace CI gate).
