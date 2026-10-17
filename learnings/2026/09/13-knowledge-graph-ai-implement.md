# A sidecar proxied statelessly must run its MCP transport in stateless mode and pin the SDK

- **Date:** 2026-09-13
- **From:** knowledge-graph-ai-implement
- **Area:** mcp | portability
- **Priority (suggested):** P1

## Symptom

A refresh completed and the rail reported `serving` with a new stamp, yet every `kg_*` tool
disappeared from the orchestrator's `tools/list` for new client sessions and failed for existing
ones. The orchestrator logged `KG sidecar tools/list returned no result (status 400):
{"code":-32600,"message":"Bad Request: Missing session ID"}`. No degraded flag fired; the deploy
was recorded as healthy.

## Root cause

`kg_query/server.py` runs `FastMCP(...).run(transport="streamable-http")` with the SDK's default
**stateful** session handling. The dependency is pinned as a range (`mcp>=1.2,<2`), and the sidecar
image is rebuilt from the repo on every orchestrator deploy, so a new SDK release inside the range
(1.30.0, 2026-09-07) reached production on the next build. That release rejects a POST that does
not carry an `mcp-session-id` from a prior `initialize`. The orchestrator proxies `tools/list` and
`tools/call` one POST at a time with no session, which is the correct shape for a sidecar behind the
orchestrator's own auth — so the two sides stopped agreeing without any code change in either repo.

## Suggested base change

1. Construct the HTTP-mode server stateless (`stateless_http=True`, `json_response=True`) so each
   POST is self-contained; keep stdio mode as is.
2. Pin the MCP SDK to a tested version or a narrow range, with a comment naming this incident, so
   an image rebuild cannot change transport behaviour silently.
3. Add a CI smoke that boots the HTTP server and POSTs `tools/list` and one `tools/call` **without**
   a session header, asserting 200 and a non-empty tool list — the check that would have caught this
   before the image shipped.
4. Document in the README that HTTP mode is stateless by design because a proxy fronts it.
