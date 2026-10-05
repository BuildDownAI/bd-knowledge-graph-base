# A fresh derivative's first refresh fails because its own bookkeeping team is empty

- **Date:** 2026-09-15
- **From:** knowledge-graph-horizon-neo
- **Area:** ingest | portability
- **Priority (suggested):** P2

## Symptom

The first real refresh of a newly created derivative KG failed at the tracker-data step. The
refresh rail had already cloned every secondary repo and fetched issues for every product team.
It then stopped with `KG_TRACKER_DATA_FETCH_FAILED: team <key> returned 0 issues — a configured
team must not be empty`, where `<key>` was the tracker team that exists only to give the KG repo's
own project mapping a team key. The served graph was untouched. Every retry fails the same way
until that team holds at least one issue.

## Root cause

Two rail rules disagree about the KG repo's own mapping. The refresh rail requires a project
mapping whose repository is the KG source repo, and a mapping needs a tracker team key, so a
bookkeeping team is created for it. On a fresh derivative that team is empty by design: nothing is
filed there yet. Scope reconcile correctly skips the KG repo as a secondary of itself, but it still
adds that mapping's team to `sources.yml` under `trackers:`. The tracker-data step then applies its
zero-issue guard uniformly to every configured team, including the bookkeeping one. The guard is
right for product teams, where zero issues signals a broken fetch. It is wrong for the one team
that is expected to start empty.

The upstream reference KG never observed this because its bookkeeping team already held issues by
the time the guard shipped. Every derivative created from the template after that will hit it on
its first refresh.

## Suggested base change

1. Let the manifest mark a tracker team as allowed to be empty, for example `allow_empty: true`
   on a `trackers:` entry in `sources.yml`, and have the ingest treat zero issues on such a team
   as a warning rather than a failure. The guard stays strict for every other team.
2. Have the KG creation skill write that flag on the bookkeeping team when it fills `sources.yml`,
   so a fresh derivative passes its first refresh without a manual step.
3. Until the flag exists, document in the setup guide that the bookkeeping team must hold at least
   one issue before the first refresh, and name the exact failure string so operators can match it.
