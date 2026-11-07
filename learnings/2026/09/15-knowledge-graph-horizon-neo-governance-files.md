# Template-governance files ride into private derivatives, and the CLA check then fails on every PR

- **Date:** 2026-09-15
- **From:** knowledge-graph-horizon-neo
- **Area:** portability
- **Priority (suggested):** P2

## Symptom

A private derivative created from the template carried the template's own governance files:
the CLA workflow, the contributor guide, and the individual and corporate license agreements
under `legal/`. The CLA workflow ran on every pull request in the derivative, including the
orchestrator's automated workflow-sync PR, and failed each time with `Branch cla-signatures not
found`, because the signatures branch exists only in the public template. The derivative accepts
no outside contributions, so the check can never be satisfied there. Every PR shows a red check
that means nothing, which hides real failures and sends operators looking for a CLA problem that
does not exist.

## Root cause

The template mixes two kinds of files. Most of it is the product: ingest, query, ontology, docs,
tests. A few files govern the public template itself: the CLA workflow, the contributor guide,
and the agreements it points to. Template generation and the creation skill copy both kinds
without distinction. Nothing marks the governance files as template-only, and the CLA workflow
has no guard for a private repository or an absent signatures branch, so a stray copy is
actively harmful rather than inert. The same set of four files had to be removed by hand from
another private fork in this ecosystem, so the pattern is already repeating.

## Suggested base change

1. Declare the template-only files in one place, for example a `.template-only` list at the
   repo root naming the CLA workflow, the contributor guide, and `legal/`. Have the creation
   skill delete every listed path when it creates a derivative, and have the derivative's
   upstream-merge flow ignore them so a later base merge does not resurrect them.
2. Make the CLA workflow self-disable outside the template: gate the job on the repository
   being public and the signatures branch existing, so a copy that does reach a derivative is
   inert instead of red.
3. State in the template README which files belong to the template's governance and are safe
   to delete in a derivative, with the same list, so an operator who finds them knows the answer
   without asking.
