# bd-knowledge-graph-base

The **BuildDown base knowledge graph** — a template for giving any project a
**read-only, queryable knowledge graph (KG)**: durable institutional memory an AI
coding agent (or a human) can search before planning work, instead of
rediscovering past learnings, decisions, and tracker history.

Use this repo as a **template** (GitHub "Use this template", or the `bd-kg-create`
skill from [BuildDownAI/skills](https://github.com/BuildDownAI/skills)) to stand up
a `knowledge-graph-<your-project>` repo in minutes.

## What you get

- **Two-layer RDF graph** (rdflib + SHACL; Stardog optional):
  - **Spine** (deterministic, zero-LLM): repos, files, commits, PRs, people from
    git/GitHub, plus tracker issues/projects and their relation graph
    (parent/blocks/related) from the Linear API.
  - **Semantic** (parsed, always provenanced): learnings and decisions from your
    docs (`docs/solutions/`, ADRs, plans) and from marked tracker comments —
    every node carries `prov:wasDerivedFrom` + `prov:wasGeneratedBy`, enforced by
    SHACL.
- **Hybrid search**: lexical + vector (fastembed, no PyTorch) fused with
  reciprocal rank fusion — robust to exact IDs *and* paraphrases.
- **A read-only MCP server** exposing `kg_hybrid_search` (preferred),
  `kg_search`, `kg_semantic_search`, `kg_neighbors`, `kg_provenance`, `kg_path`.
- **Git-diffable snapshots**: the binary graph stays untracked; a compact
  `snapshot/` digest + per-type parts make data changes reviewable in git.

## Quickstart

Requires **Python 3.10+**.

```bash
# 1. Create your repo from this template, then:
./setup.sh                      # venv + deps + build the graph
                                # (uses uv when installed, else venv + pip;
                                #  defaults build a small self-graph of this repo)

# 2. Point it at your project: edit sources.yml
#    - namespace:  https://kg.<your-org>.dev/   (set once, up front)
#    - code_repo:  your project's slug + local path
#    - trackers:   your Linear team(s)          (needs LINEAR_API_KEY)
./setup.sh --tracker --secondary

# 3. Search from a terminal:
./.venv/bin/kg-query search "your first query"
./.venv/bin/kg-query search --hybrid "a prose query"   # lexical+vector, RRF-fused

# 4. Register the MCP server:
cp .mcp.json.example .mcp.json  # fill in __REPO_DIR__ (absolute) + __PROJECT_SLUG__
# list the same entry in your project repo's .mcp.json; restart your MCP client
```

The canonical refresh is `./setup.sh --tracker --secondary` (or
`kg-ingest build …`) — it rebuilds the graph **and** the vector index
in one pass. The MCP server loads the graph once at startup: **restart your MCP
client after a refresh**.

Installing the project (editable, via `uv sync` or `pip install -e .`) puts two
console scripts in the venv — **`kg-ingest`** (`build` / `embed` / `snapshot` /
`materialize`) and **`kg-query`** (`search`, with `--semantic` / `--hybrid` /
`--neighbors` / `--provenance` modes, and `serve` for the MCP server). The
`python -m kg_ingest.<module>` / `python -m kg_query.<module>` forms still work,
and `PYTHONPATH=.` is no longer needed anywhere.

## Layout

```
sources.yml            # THE source manifest (edit this first; includes namespace)
ontology/              # kg.ttl (classes/predicates) + shapes.ttl (SHACL provenance invariant)
kg_ingest/             # spine + semantic + tracker ingesters, embeddings, snapshot writer
kg_query/              # store seam (rdflib|Stardog), read tools, hybrid search, MCP server
docs/design/           # how the ingest works, semantic + hybrid search design
learnings/             # the improvement loop's inbox — accept / decline / defer, see CONTRIBUTING.md
tests/                 # mechanical validation: SHACL invariant, tools, search
pyproject.toml         # canonical dependency list (uv; uv.lock pins the resolve)
setup.sh               # one-shot: venv (uv or pip) + deps + build
```

## Keeping a downstream KG current

Your KG repo should track this base via a git remote (forks of a public repo
must be public, so private KGs use a remote instead):

```bash
git remote add upstream https://github.com/BuildDownAI/bd-knowledge-graph-base.git
git fetch upstream && git merge upstream/main
```

Your business config (`sources.yml`, `snapshot/`, `out/`) never conflicts — the
base never ships real bindings or data.

## Improving the base — the learnings loop

Operating a KG teaches you things the base should absorb (ingest failure
classes, classifier misses, portability gaps). Contribute them as small,
**sanitized**, time-based learning notes PR'd into `testing` — see
[CONTRIBUTING.md](CONTRIBUTING.md) and [learnings/README.md](learnings/README.md).

## License

Apache-2.0 — see [LICENSE](LICENSE).
