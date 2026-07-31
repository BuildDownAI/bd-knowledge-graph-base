# learnings/ — the improvement loop's inbox

Time-based, **sanitized** notes from teams operating downstream KGs, submitted as
small PRs into `testing` (see [CONTRIBUTING.md](../CONTRIBUTING.md)). Accepted
notes are triaged by priority; the base changes they suggest are implemented by
priority.

## File convention

```
learnings/YYYY/MM/DD-<project-slug>.md
```

One note per file. Branch name: `kg-learnings/YYYY-MM-DD-<project-slug>`.

## Note format

```markdown
# <one-line title of the pattern>

- **Date:** YYYY-MM-DD
- **From:** <project-slug>            # the only identifying reference allowed
- **Area:** ingest | classifier | search | snapshot | mcp | portability | perf
- **Priority (suggested):** P1 | P2 | P3

## Symptom
What was observed, described generically.

## Root cause
Why it happened — in terms of base logic, not business specifics.

## Suggested base change
The smallest change to this template that would absorb the learning.
```

## Sanitization (hard rule — this repo will be public)

No ticket contents, no internal names beyond the project slug, no private code,
no graph extracts. Patterns only. See CONTRIBUTING.md.
