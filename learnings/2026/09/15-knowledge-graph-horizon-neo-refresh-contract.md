# The template lacks the refresh rail's `kg-ingest refresh` contract, so a fresh derivative cannot be refreshed

- **Date:** 2026-09-15
- **From:** knowledge-graph-horizon-neo
- **Area:** ingest | portability
- **Priority (suggested):** P1

## Symptom

A derivative created from this template passed every earlier stage of the refresh rail and
failed at the ingest step. The rail invoked `python -m kg_ingest refresh --code-repo <dir>
--repos-root <dir> --tracker-data <file>`. The template's CLI has no `refresh` subcommand, so
the unknown word fell through to the default `build` subcommand, which requires `--repo` and
exited 2 with `the following arguments are required: --repo`. The rail recorded
`KG_INGEST_FAILED` and left the previous snapshot serving. Every derivative created from the
template today fails its first rail refresh the same way.

## Root cause

The rail's ingest contract was implemented on the reference derivative, not on the template:
a `refresh` subcommand that reads everything from `sources.yml`, a `--tracker-data` input for
the pipeline-fetched issue export so no tracker API key enters the runner, and `--repos-root`
for the secondary-repo clones. Those changes were merged forward on the reference derivative
each time it pulled the template, but never ported back. The template even carries the
`tests/fixtures/tracker-data.json` fixture without the test that consumes it. Learnings notes
flow from derivatives to the template, but code so far has flowed only from the template to
derivatives, so the one derivative that had the contract kept it to itself.

## Suggested base change

1. Port the refresh contract into the template: the `refresh` subcommand, `--tracker-data`,
   `--repos-root`, the single exported issue-field list, and the tracker-data contract test.
   On the reference derivative this is four files under `kg_ingest/` plus one test, about
   340 lines, and the ported package is byte-identical to that derivative's copy.
2. Add a CI check to the template that `python -m kg_ingest refresh -h` exits 0, so the rail
   contract is pinned and any future drift between the rail and the template fails in CI
   rather than on a derivative's first refresh.
3. Document the exact rail invocation in the template README, so a derivative operator can
   recognise the failure string and the missing subcommand at a glance.
