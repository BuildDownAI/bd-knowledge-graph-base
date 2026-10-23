# source-results golden fixtures

These are golden copies of `ai-output/source-results.json`, one per ingest
outcome. They are written by the real `kg_ingest/cli.py` code path and kept
here as a contract between KGB and AI-Implement.

## Files

| File | Scenario |
|---|---|
| `success.json` | code_repo + tracker succeed; docs_site and self are skipped (none configured / `self_ingest: false`) |
| `skipped.json` | A configured docs_site is explicitly skipped via `--no-docs-sites` |
| `failed.json` | A secondary repo with a missing path fails; code_repo succeeds |
| `ingest-crash.json` | A post-source-loop exception triggers the `ingest:*` entry written in `cli.py`'s `finally` block |

## Schema

Every entry in the list has exactly these keys:

```json
{
  "source": "<kind>:<slug or *>",
  "kind":   "code_repo | docs_site | tracker | secondary_repo | self | ingest",
  "status": "success | skipped | failed",
  "reason": "<string or null>",
  "counts": { ... }
}
```

## AI-Implement reads these files

AI-Implement (`parseSourceResults` in `src/kg-source-results.ts`) reads the
same schema. A field rename or status-value change in `kg_ingest/cli.py` must
be reflected here **and** in the corresponding AI-Implement issue (AII-1528).

## Updating

Regenerate all four files from the real code path:

```sh
KG_UPDATE_GOLDEN=1 pytest tests/test_source_results_contract.py
```

Run without the flag immediately after to confirm the new files round-trip:

```sh
pytest tests/test_source_results_contract.py
```

Commit both the updated golden files and the test run in the same PR so the
diff is reviewable alongside the code change that caused it.
