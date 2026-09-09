# In-place refresh materialize is OOM-killed beside the serving sidecar on 512 MB hosts

- **Date:** 2026-09-08
- **From:** knowledge-graph-ai-implement
- **Area:** perf / portability
- **Priority (suggested):** P2

## Symptom

In-place refresh fails at the materialize gate with a bare "Command failed". The host
log shows the process was OOM-killed. The old graph continues serving without
interruption; a retry on a freshly provisioned host with more memory succeeds.

## Root cause

`kg_ingest/materialize.py` parses every `snapshot/parts/*.nt` file into a single
in-memory rdflib graph before serializing `out/graph.trig`. Resident memory grows
proportionally with graph size — approximately 270 MB at ~31k quads. When materialize
runs beside the serving sidecar (which holds a fastembed model at 300–400 MB), the
combined footprint exceeds the 512 MB host limit and the OS kills the process.

## Suggested base change

(a) Document a memory-sizing rule in the base docs: serving alone is viable at 512 MB;
in-place refresh beside the sidecar requires 1 GB or more for graphs up to ~40k quads.

(b) Evaluate whether the serving layer can consume the N-Triples parts files directly,
eliminating the need for `kg_ingest/materialize.py` to re-serialize into a single
`.trig` file during refresh — this would remove the peak-RSS spike from the refresh
path entirely.

(c) Add peak RSS reporting to `kg_ingest/materialize.py` so that when the process is
killed the failure message names the memory ceiling rather than surfacing only a bare
"Command failed".
