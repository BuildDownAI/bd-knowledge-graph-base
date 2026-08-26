# Security Policy

## Reporting a Vulnerability

If you discover a security issue, **do not open a public GitHub issue**. Instead:

- Open a private security advisory at https://github.com/BuildDownAI/bd-knowledge-graph-base/security/advisories/new.

Please include enough detail for the issue to be reproduced (the affected component, configuration, and repro steps). You'll get an acknowledgement within 5 business days.

## What's in scope

This repository is a **template for building a read-only knowledge graph** from a project's code, docs, and tracker — a set of Python packages (`kg_ingest`, `kg_query`), an ontology, and setup scripts. In-scope issues are those where this repository's own code can cause harm when a downstream operator runs it:

1. `setup.sh` and the `kg-ingest` / `kg-query` entry points — command injection, or arbitrary file write/read outside the intended graph and snapshot directories.
2. The docs/site crawler and any ingest path that fetches remote content — SSRF, path traversal, or unsafe parsing of fetched pages, `sources.yml`, or snapshot data.
3. The MCP query surface — a query that can read or exfiltrate files outside the built graph.

## What's out of scope

- The **AI-Implement** orchestrator service and its `/mcp` deployment — report those in that repository's own advisories (https://github.com/BuildDownAI/AI-Implement).
- A **downstream KG's own data**: the real `sources.yml` bindings, `out/`, `snapshot/`, tracker credentials, and any private graph a team builds from this template are that deployment's responsibility, not this template's.
- Vulnerabilities in dependencies that have not yet been disclosed upstream — please report those to the upstream project first.

## Disclosure

We aim to publish a fix and an advisory within 30 days of confirmation, coordinated with the reporter. Credit is given by default unless you ask otherwise.
