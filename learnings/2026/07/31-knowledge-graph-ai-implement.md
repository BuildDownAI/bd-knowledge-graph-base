# Validation gates must run (and pass) before the commit that claims them

- **Date:** 2026-07-31
- **From:** knowledge-graph-ai-implement
- **Area:** portability
- **Priority (suggested):** P2

## Symptom

A change making the test fixtures namespace-agnostic was committed and PR'd with
the commit message claiming validation under a custom namespace. The custom-
namespace run had actually failed: the fixture-rebase edit had silently collapsed
to a no-op (`.replace(X, X)`), which is invisible under the default namespace —
the only configuration the author had watched pass before committing.

## Root cause

Two compounding patterns: (1) a batched script ran the validation *and* the
commit in one shot, so a failing gate could not stop the commit; (2) the defect
class — "transformation that is a no-op under the default configuration" — is
exactly the class that only a non-default configuration exposes, and the
non-default run was treated as optional follow-up rather than a gate.

## Suggested base change

Add a CI workflow to this template that runs the full test suite twice: once
under the neutral default namespace and once with a custom `namespace:` value
injected into `sources.yml`. Any template change that breaks re-homing then
fails in CI rather than in a downstream repo. (Until CI exists: CONTRIBUTING
should state the dual-namespace suite is a pre-commit gate for template changes.)
