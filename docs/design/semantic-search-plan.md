# Semantic Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add paraphrase-tolerant semantic search to the KG as a lightweight index into the graph (one compact vector per Learning/Decision/Issue), exposed as a 5th read-only MCP tool.

**Architecture:** An offline build step embeds one short "index card" per node with fastembed into a gitignored `out/embeddings.npz` sidecar (+ plain-JSON meta). A query module loads the sidecar once and returns top-k by cosine similarity (numpy matrix–vector product, no ANN index). The existing lexical `kg_search` is untouched.

**Tech Stack:** Python 3.10+, rdflib (SPARQL over the `Store` abstraction), fastembed (`BAAI/bge-small-en-v1.5`, ONNX, no PyTorch), numpy.

## Global Constraints

- **Python 3.10+** (the `mcp` package floor; venv is `python3.10`).
- **Embedding model pinned:** `BAAI/bge-small-en-v1.5`, **384-dim**, via `fastembed>=0.3`. Model name is a module constant, swappable in one place.
- **Snippet length:** `SNIPPET_CHARS = 400` (reuses the existing convention).
- **Core ingest must NOT import fastembed** — `kg_ingest.cli`, `kg_ingest.semantic`, `kg_ingest.snapshot` stay fastembed/numpy-free at import time; fastembed is imported lazily only inside `kg_ingest/embed.py` and `kg_query/semantic.py`.
- **Sidecar is gitignored** — `out/embeddings.npz` and `out/embeddings.meta.json` live under `out/`, which `.gitignore` already ignores. Do NOT commit them.
- **No vector DB** — numpy brute-force cosine only.
- **Tests are standalone assert scripts** (repo has no pytest), run as `PYTHONPATH=. ./.venv/bin/python tests/<name>.py`, printing `PASS:`/failing via `assert`.
- **Determinism:** card order sorted by IRI; vectors L2-normalized at build time.

## File Structure

- Create `kg_ingest/cards.py` — build one index-card dict per Learning/Decision/Issue via SPARQL over a `Store`. No embedding dependency.
- Create `kg_ingest/embed.py` — CLI build step: read graph → `build_cards` → fastembed → write `out/embeddings.npz` + `out/embeddings.meta.json`.
- Create `kg_query/semantic.py` — load the sidecar once; `semantic_search(query, limit)` returns top-k.
- Modify `kg_query/server.py` — add the `kg_semantic_search` tool.
- Modify `kg_ingest/cli.py` — add `--embed` flag chaining the build step after ingest.
- Modify `kg_ingest/snapshot.py` — add a `## Semantic index` digest line from the meta JSON.
- Modify `requirements.txt` — add `fastembed>=0.3`.
- Modify `README.md` — document the tool + build step.
- Create `tests/test_cards.py`, `tests/test_semantic.py`.

---

### Task 1: Index card builder (`kg_ingest/cards.py`)

**Files:**
- Create: `kg_ingest/cards.py`
- Test: `tests/test_cards.py`

**Interfaces:**
- Consumes: a `Store` (duck-typed; `.select(sparql) -> list[dict[str,str]]`) from `kg_query.store`; `PREFIXES` from `kg_query.queries`.
- Produces: `build_cards(store) -> list[dict]`, each card `{"iri": str, "type": "Learning"|"Decision"|"Issue", "title": str, "snippet": str, "card_text": str}`, sorted by `iri`. `SNIPPET_CHARS = 400`.

- [ ] **Step 1: Write the failing test** — `tests/test_cards.py`

```python
"""cards.build_cards over a tiny fixture graph.
Run: PYTHONPATH=. ./.venv/bin/python tests/test_cards.py
"""
import os, tempfile
from kg_ingest.cards import build_cards, SNIPPET_CHARS
from kg_query.store import RdflibStore

FIXTURE = """
@prefix kg: <https://example.org/kg/onto#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<https://example.org/kg/resource/graph/spine> {
  <https://example.org/kg/resource/issue/a tracker issue> a kg:Issue ;
    dcterms:title "Dispatch dedup has no TTL" ;
    kg:tagged <https://example.org/kg/resource/topic/dedup> ;
    kg:description "A torn-down issue can never be re-dispatched." .
  <https://example.org/kg/resource/topic/dedup> a kg:Topic ; skos:prefLabel "dedup" .
  <https://example.org/kg/resource/comment/a tracker issue/0> a kg:Learning ;
    dcterms:title "a tracker issue build-up learnings - dedup TTL" ;
    kg:fix "Add a TTL so torn-down issues can be re-dispatched." ;
    kg:tagged <https://example.org/kg/resource/topic/dedup> .
  <https://example.org/kg/resource/adr/0001> a kg:Decision ;
    dcterms:title "Use rdflib backend" ;
    kg:detail "Chosen for zero-infra local dev." .
}
"""

def main():
    with tempfile.NamedTemporaryFile("w", suffix=".trig", delete=False) as f:
        f.write(FIXTURE); path = f.name
    try:
        cards = build_cards(RdflibStore(path))
    finally:
        os.unlink(path)
    types = {c["type"] for c in cards}
    assert types == {"Learning", "Decision", "Issue"}, types
    learning = next(c for c in cards if c["type"] == "Learning")
    assert "dedup" in learning["card_text"].lower(), learning["card_text"]
    assert "TTL" in learning["card_text"], learning["card_text"]
    issue = next(c for c in cards if c["type"] == "Issue")
    assert "dedup" in issue["card_text"].lower(), issue["card_text"]
    assert all(len(c["snippet"]) <= SNIPPET_CHARS for c in cards)
    assert cards == sorted(cards, key=lambda c: c["iri"]), "must be IRI-sorted"
    print(f"PASS: build_cards -> {len(cards)} cards {types}")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. ./.venv/bin/python tests/test_cards.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'kg_ingest.cards'`.

- [ ] **Step 3: Write the implementation** — `kg_ingest/cards.py`

```python
"""Build compact 'index cards' — one short text per Learning/Decision/Issue —
for the semantic-search embedding step. Read-only SPARQL over a Store; NO
embedding dependency here so it stays trivially testable.
"""
from __future__ import annotations

from kg_query.queries import PREFIXES

SNIPPET_CHARS = 400

_LEARNING_Q = PREFIXES + """
SELECT ?iri ?title (GROUP_CONCAT(DISTINCT ?tag; SEPARATOR=", ") AS ?tags)
       (SAMPLE(?fixv) AS ?fix) WHERE {
  ?iri a kg:Learning ; dcterms:title ?title .
  OPTIONAL { ?iri kg:fix ?fixv }
  OPTIONAL { ?iri kg:tagged ?t . ?t skos:prefLabel ?tag }
} GROUP BY ?iri ?title
"""

_DECISION_Q = PREFIXES + """
SELECT ?iri ?title (SAMPLE(?d) AS ?detail) WHERE {
  ?iri a kg:Decision ; dcterms:title ?title .
  OPTIONAL { ?iri kg:detail ?d }
} GROUP BY ?iri ?title
"""

_ISSUE_Q = PREFIXES + """
SELECT ?iri ?title (GROUP_CONCAT(DISTINCT ?tag; SEPARATOR=", ") AS ?tags)
       (SAMPLE(?descr) AS ?description) WHERE {
  ?iri a kg:Issue ; dcterms:title ?title .
  OPTIONAL { ?iri kg:tagged ?t . ?t skos:prefLabel ?tag }
  OPTIONAL { ?iri kg:description ?descr }
} GROUP BY ?iri ?title
"""


def _card(iri: str, ntype: str, title: str, extras: list[str], snippet: str) -> dict:
    parts = [title] + [e for e in extras if e]
    snip = (snippet or "")[:SNIPPET_CHARS]
    if snip:
        parts.append(snip)
    return {"iri": iri, "type": ntype, "title": title,
            "snippet": snip, "card_text": "\n".join(parts)}


def build_cards(store) -> list[dict]:
    cards: list[dict] = []
    for r in store.select(_LEARNING_Q):
        cards.append(_card(r["iri"], "Learning", r.get("title", ""),
                           [r.get("tags", "")], r.get("fix", "")))
    for r in store.select(_DECISION_Q):
        cards.append(_card(r["iri"], "Decision", r.get("title", ""),
                           [], r.get("detail", "")))
    for r in store.select(_ISSUE_Q):
        cards.append(_card(r["iri"], "Issue", r.get("title", ""),
                           [r.get("tags", "")], r.get("description", "")))
    cards.sort(key=lambda c: c["iri"])
    return cards
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. ./.venv/bin/python tests/test_cards.py`
Expected: `PASS: build_cards -> 3 cards {...}`.

- [ ] **Step 5: Commit**

```bash
git add kg_ingest/cards.py tests/test_cards.py
git commit -m "feat(semantic): index-card builder over Learning/Decision/Issue"
```

---

### Task 2: Embedding build step (`kg_ingest/embed.py`)

**Files:**
- Create: `kg_ingest/embed.py`
- Modify: `requirements.txt` (add `fastembed>=0.3`)
- Test: `tests/test_embed.py`

**Interfaces:**
- Consumes: `build_cards` (Task 1); `get_store` / `RdflibStore` from `kg_query.store`.
- Produces: `build_embeddings(store, out_dir=OUT_DIR) -> dict` (the `meta` dict), writing `out/embeddings.npz` (arrays `vectors,iris,types,titles,snippets` + `meta` json string) and `out/embeddings.meta.json`. Constants: `MODEL = "BAAI/bge-small-en-v1.5"`, `DIM = 384`, `OUT_DIR`.

> First run downloads the model (~130 MB) — needs network once, then fully offline/cached.

- [ ] **Step 1: Add the dependency** — append to `requirements.txt`

```
fastembed>=0.3
```

- [ ] **Step 2: Install it**

Run: `./.venv/bin/pip install -r requirements.txt`
Expected: fastembed + onnxruntime + numpy resolve and install.

- [ ] **Step 3: Write the failing test** — `tests/test_embed.py`

```python
"""Build embeddings over a fixture graph; assert the sidecar files + meta.
Downloads the model on first run. Gated on fastembed.
Run: PYTHONPATH=. ./.venv/bin/python tests/test_embed.py
"""
import os, json, tempfile
from pathlib import Path

try:
    import fastembed  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

from kg_query.store import RdflibStore
from kg_ingest.embed import build_embeddings, MODEL, DIM

FIXTURE = """
@prefix kg: <https://example.org/kg/onto#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<https://example.org/kg/resource/graph/spine> {
  <https://example.org/kg/resource/comment/a tracker issue/0> a kg:Learning ;
    dcterms:title "dedup has no TTL" ; kg:fix "Add a TTL to dispatch dedup." .
  <https://example.org/kg/resource/adr/0001> a kg:Decision ;
    dcterms:title "Use rdflib" ; kg:detail "Zero-infra local dev." .
}
"""

def main():
    if not HAVE:
        print("SKIP: fastembed not installed"); return
    d = tempfile.mkdtemp()
    trig = os.path.join(d, "g.trig")
    Path(trig).write_text(FIXTURE)
    meta = build_embeddings(RdflibStore(trig), out_dir=Path(d))
    assert (Path(d) / "embeddings.npz").exists()
    assert (Path(d) / "embeddings.meta.json").exists()
    assert meta["model"] == MODEL and meta["dim"] == DIM
    assert meta["count"] == 2, meta
    on_disk = json.loads((Path(d) / "embeddings.meta.json").read_text())
    assert on_disk == meta
    print(f"PASS: build_embeddings -> {meta['count']} vectors dim {meta['dim']}")

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `PYTHONPATH=. ./.venv/bin/python tests/test_embed.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'kg_ingest.embed'`.

- [ ] **Step 5: Write the implementation** — `kg_ingest/embed.py`

```python
"""Build the semantic-search embedding sidecar from out/graph.trig.

Reads the graph, builds one compact index card per Learning/Decision/Issue
(kg_ingest.cards), embeds each with fastembed (BAAI/bge-small-en-v1.5, ONNX, no
torch), and writes out/embeddings.npz + out/embeddings.meta.json (both gitignored).
fastembed/numpy are imported lazily so the core ingest never depends on them.

Run: python -m kg_ingest.embed
"""
from __future__ import annotations

import json
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "out"
MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384
CARD_FIELDS = "title + tags/labels + <=400-char snippet"


def build_embeddings(store, out_dir: Path = OUT_DIR) -> dict:
    import numpy as np
    from fastembed import TextEmbedding
    from .cards import build_cards

    cards = build_cards(store)
    if not cards:
        raise RuntimeError("no cards built — is out/graph.trig populated?")
    texts = [c["card_text"] for c in cards]
    model = TextEmbedding(model_name=MODEL)
    vecs = np.asarray(list(model.embed(texts)), dtype=np.float32)
    vecs /= (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12)
    assert vecs.shape[1] == DIM, (vecs.shape, DIM)

    n_quads = int(store.select("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")[0]["n"])
    meta = {"model": MODEL, "dim": int(vecs.shape[1]), "count": len(cards),
            "built_from_quads": n_quads, "card_fields": CARD_FIELDS}

    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez(out_dir / "embeddings.npz",
             vectors=vecs,
             iris=np.array([c["iri"] for c in cards]),
             types=np.array([c["type"] for c in cards]),
             titles=np.array([c["title"] for c in cards]),
             snippets=np.array([c["snippet"] for c in cards]),
             meta=json.dumps(meta))
    (out_dir / "embeddings.meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def main() -> None:
    from kg_query.store import get_store
    meta = build_embeddings(get_store())
    print(f"embedded {meta['count']} cards ({meta['model']}, dim {meta['dim']}) "
          f"-> {OUT_DIR}/embeddings.npz + embeddings.meta.json")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `PYTHONPATH=. ./.venv/bin/python tests/test_embed.py`
Expected: `PASS: build_embeddings -> 2 vectors dim 384` (first run also prints model-download progress).

- [ ] **Step 7: Commit**

```bash
git add kg_ingest/embed.py tests/test_embed.py requirements.txt
git commit -m "feat(semantic): fastembed build step -> gitignored embeddings sidecar"
```

---

### Task 3: Query module + MCP tool (`kg_query/semantic.py`, `kg_query/server.py`)

**Files:**
- Create: `kg_query/semantic.py`
- Modify: `kg_query/server.py` (import `semantic`; add `kg_semantic_search` tool)
- Test: `tests/test_semantic.py`

**Interfaces:**
- Consumes: `build_embeddings` (Task 2); the sidecar `out/embeddings.npz`.
- Produces: `semantic_search(query, limit=10, npz_path=NPZ) -> dict` with shape `{"query", "model", "count", "results": [{"iri","type","title","snippet","score"}]}`; missing sidecar → `{"query","error","count":0,"results":[]}`. Module singleton `_index` (reset via `semantic._index = None` in tests). Constant `NPZ`.

- [ ] **Step 1: Write the failing test** — `tests/test_semantic.py`

```python
"""End-to-end: build embeddings over a fixture, then assert a PARAPHRASED query
ranks the dedup learning first. Also the missing-sidecar path.
Gated on fastembed. Run: PYTHONPATH=. ./.venv/bin/python tests/test_semantic.py
"""
import os, tempfile
from pathlib import Path

try:
    import fastembed  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

from kg_query import semantic
from kg_query.store import RdflibStore
from kg_ingest.embed import build_embeddings

FIXTURE = """
@prefix kg: <https://example.org/kg/onto#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
<https://example.org/kg/resource/graph/spine> {
  <https://example.org/kg/resource/comment/a tracker issue/0> a kg:Learning ;
    dcterms:title "Dispatch dedup has no TTL - a torn-down issue can never be re-dispatched" ;
    kg:fix "Add a TTL so a torn-down issue can be re-dispatched." .
  <https://example.org/kg/resource/comment/a tracker issue/0> a kg:Learning ;
    dcterms:title "Auto-merge child PRs into feature branches" ;
    kg:fix "Recursively merge completed child PRs up the feature tree." .
}
"""

def test_missing_sidecar():
    r = semantic.semantic_search("anything", npz_path=Path("/nonexistent/x.npz"))
    assert r["results"] == [] and "error" in r, r
    print("PASS: missing sidecar -> friendly error")

def test_ranks_dedup_first():
    d = tempfile.mkdtemp()
    Path(d, "g.trig").write_text(FIXTURE)
    build_embeddings(RdflibStore(os.path.join(d, "g.trig")), out_dir=Path(d))
    semantic._index = None  # reset singleton for the fixture path
    r = semantic.semantic_search("how do we stop re-dispatching a torn-down issue",
                                 limit=2, npz_path=Path(d) / "embeddings.npz")
    assert r["results"], r
    top = r["results"][0]
    assert "dedup" in (top["title"] + top["snippet"]).lower(), top
    assert r["results"][0]["score"] >= r["results"][-1]["score"]
    print(f"PASS: paraphrase ranked dedup first (score {top['score']})")

def main():
    test_missing_sidecar()
    if not HAVE:
        print("SKIP: fastembed not installed — build/query path not exercised"); return
    test_ranks_dedup_first()
    print("\nall semantic tests passed")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. ./.venv/bin/python tests/test_semantic.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'kg_query.semantic'`.

- [ ] **Step 3: Write the query module** — `kg_query/semantic.py`

```python
"""Semantic (vector) search over the KG — the read side of the embedding sidecar.

Loads out/embeddings.npz once, embeds the query with the same pinned model, and
returns the top-k nearest index cards by cosine similarity (a single normalized
matrix-vector product; no ANN index needed at this scale). Read-only.
"""
from __future__ import annotations

import json
from pathlib import Path

NPZ = Path(__file__).resolve().parent.parent / "out" / "embeddings.npz"

_index = None  # lazy singleton


class _Index:
    def __init__(self, npz_path: Path):
        import numpy as np
        data = np.load(npz_path, allow_pickle=False)
        self.vectors = data["vectors"]
        self.iris = data["iris"]
        self.types = data["types"]
        self.titles = data["titles"]
        self.snippets = data["snippets"]
        self.meta = json.loads(str(data["meta"]))


def _load(npz_path: Path):
    global _index
    if _index is None:
        _index = _Index(npz_path)
    return _index


def semantic_search(query: str, limit: int = 10, npz_path: Path = NPZ) -> dict:
    if not Path(npz_path).exists():
        return {"query": query,
                "error": "no embeddings index — run `python -m kg_ingest.embed`",
                "count": 0, "results": []}
    import numpy as np
    from fastembed import TextEmbedding

    idx = _load(npz_path)
    model = TextEmbedding(model_name=idx.meta["model"])
    qv = np.asarray(next(iter(model.embed([query]))), dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-12)
    sims = idx.vectors @ qv
    order = np.argsort(-sims)[: int(limit)]
    results = [{
        "iri": str(idx.iris[i]), "type": str(idx.types[i]),
        "title": str(idx.titles[i]), "snippet": str(idx.snippets[i]),
        "score": round(float(sims[i]), 4),
    } for i in order]
    return {"query": query, "model": idx.meta["model"],
            "count": len(results), "results": results}
```

- [ ] **Step 4: Wire the MCP tool** — in `kg_query/server.py`, change the import line `from . import queries` to:

```python
from . import queries, semantic
```

Then add this tool after the `kg_search` tool definition:

```python
@mcp.tool()
def kg_semantic_search(query: str, limit: int = 10) -> dict:
    """Semantic (vector) search over prior LEARNINGS, DECISIONS, and ISSUES — use
    for paraphrased or conceptual queries that the exact-term `kg_search` misses
    (e.g. "how do we stop re-dispatching a torn-down issue"). Returns ranked index
    cards {iri, type, title, snippet, score}; follow up with kg_neighbors /
    kg_provenance for full content. Requires the embeddings sidecar (build with
    `python -m kg_ingest.embed`); returns a friendly error if absent. Read-only.
    """
    return semantic.semantic_search(query, limit)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `PYTHONPATH=. ./.venv/bin/python tests/test_semantic.py`
Expected: `PASS: missing sidecar -> friendly error` then `PASS: paraphrase ranked dedup first (score ...)`.

- [ ] **Step 6: Verify the tool is registered (import smoke)**

Run: `./.venv/bin/python -c "import kg_query.server as s; print('kg_semantic_search' in [t.name for t in __import__('asyncio').run(s.mcp.list_tools())])"`
Expected: `True`.

- [ ] **Step 7: Commit**

```bash
git add kg_query/semantic.py kg_query/server.py tests/test_semantic.py
git commit -m "feat(semantic): kg_semantic_search query module + MCP tool"
```

---

### Task 4: Pipeline integration (`--embed` flag + digest line)

**Files:**
- Modify: `kg_ingest/cli.py` (add `--embed` flag + chained build)
- Modify: `kg_ingest/snapshot.py` (add `## Semantic index` digest section)

**Interfaces:**
- Consumes: `build_embeddings` (Task 2); `out/embeddings.meta.json`.
- Produces: `kg_ingest.cli --embed` builds the sidecar after ingest; `snapshot.build_digest` appends a `## Semantic index` section when the meta JSON exists.

- [ ] **Step 1: Add the `--embed` flag** — in `kg_ingest/cli.py`, after the `--secondary` argument definition add:

```python
    ap.add_argument("--embed", action="store_true",
                    help="also build the semantic-search embedding sidecar "
                         "(needs fastembed; writes out/embeddings.npz)")
```

- [ ] **Step 2: Chain the build after the snapshot** — in `kg_ingest/cli.py`, immediately after the `snap = snapshot.write_snapshot(...)` block and before `return 0 if conforms else 1`, add:

```python
    if args.embed:
        from . import embed as embed_mod
        from kg_query.store import RdflibStore
        e = embed_mod.build_embeddings(RdflibStore(trig_path))
        print(f"== embeddings -> {e['count']} cards ({e['model']}, dim {e['dim']}) ==")
```

- [ ] **Step 3: Add the digest section** — in `kg_ingest/snapshot.py`, add `import json` at the top (next to the existing imports), then inside `build_digest`, immediately before the final `return "\n".join(L)`, add:

```python
    # ---- semantic index (only if the embedding sidecar has been built) ----
    _meta = Path(__file__).resolve().parent.parent / "out" / "embeddings.meta.json"
    if _meta.exists():
        m = json.loads(_meta.read_text())
        L.append("## Semantic index")
        L.append("")
        L.append(f"- model: {m.get('model')}")
        L.append(f"- dim: {m.get('dim')}")
        L.append(f"- vectors: {m.get('count')}")
        L.append("")
```

- [ ] **Step 4: Verify end-to-end** — run a real ingest with embeddings, then confirm the digest line.

Run:
```bash
LINEAR_API_KEY=$(grep '^LINEAR_API_KEY=' ../AI-Implement/.env | head -1 | sed -E 's/^LINEAR_API_KEY=//; s/^["'\'']//; s/["'\'']$//') \
  ./.venv/bin/python -m kg_ingest.cli --repo ../AI-Implement --tracker --secondary --embed
grep -A4 "## Semantic index" snapshot/digest.md
```
Expected: ingest ends with `== embeddings -> N cards (BAAI/bge-small-en-v1.5, dim 384) ==`, and `snapshot/digest.md` contains the `## Semantic index` block with model/dim/vectors.

- [ ] **Step 5: Confirm the sidecar is NOT staged (gitignored)**

Run: `git status --porcelain | grep -E "embeddings\.(npz|meta\.json)" || echo "not tracked ✓"`
Expected: `not tracked ✓`.

- [ ] **Step 6: Commit**

```bash
git add kg_ingest/cli.py kg_ingest/snapshot.py snapshot/digest.md
git commit -m "feat(semantic): --embed flag + semantic-index digest line"
```

---

### Task 5: Docs + full-suite green

**Files:**
- Modify: `README.md`
- Test: run all `tests/*.py`

**Interfaces:**
- Consumes: everything above. Produces: user-facing docs; a green suite.

- [ ] **Step 1: Document the tool + build step** — in `README.md`, add `kg_semantic_search` to the "Query surface" tool list:

```markdown
- `kg_semantic_search(query, limit)` — **semantic (vector)** search for paraphrased/conceptual queries the lexical `kg_search` misses. Needs the embeddings sidecar (`python -m kg_ingest.embed`).
```

Then add this subsection after the "Setup & run" section:

```markdown
## Semantic search (optional)

`kg_search` is lexical (exact term / tag match). For paraphrase-tolerant search,
build the embedding sidecar once, then use `kg_semantic_search`:

    ./.venv/bin/pip install -r requirements.txt          # pulls fastembed
    ./.venv/bin/python -m kg_ingest.embed                # first run downloads the model (~130 MB)
    # or fold it into a full ingest:
    ./.venv/bin/python -m kg_ingest.cli --repo ../AI-Implement --tracker --secondary --embed

This writes the gitignored `out/embeddings.npz` (one vector per Learning /
Decision / Issue — an index into the graph, not a copy of the corpus). Restart
the MCP client so the server loads it. Design: `docs/design/semantic-search.md`.
```

- [ ] **Step 2: Run the full test suite**

Run:
```bash
for t in test_shacl test_classify test_query test_cards test_embed test_semantic; do
  echo "--- $t ---"; PYTHONPATH=. ./.venv/bin/python tests/$t.py 2>&1 | tail -3
done
```
Expected: every script prints its `PASS`/`all ... passed` lines; no tracebacks.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(semantic): document kg_semantic_search + the embed build step"
```

---

## Self-Review

**Spec coverage:**
- Scope B (Learning/Decision/Issue cards) → Task 1 (`cards.py`). ✓
- Index card fields (title + tags/labels + ≤400 snippet) → Task 1 `_card` + `SNIPPET_CHARS`. ✓
- fastembed `BAAI/bge-small-en-v1.5`, no torch, lazy import → Task 2 (`MODEL`, lazy imports). ✓
- Sidecar `.npz` + plain-JSON meta, gitignored → Task 2 (writes both), Task 4 Step 5 (gitignore check). ✓
- numpy brute-force cosine, no ANN → Task 3 (`idx.vectors @ qv`). ✓
- New `kg_semantic_search` tool, `kg_search` untouched → Task 3 Step 4. ✓
- Decoupled build step + optional `--embed` chain → Task 2 (`embed.py` main), Task 4 (flag). ✓
- Digest line from meta, numpy-free snapshot → Task 4 Step 3 (reads JSON, no numpy). ✓
- Missing-sidecar friendly error → Task 3 (`semantic_search` guard) + test. ✓
- Determinism (IRI-sorted, normalized) → Task 1 sort + Task 2 normalize. ✓
- Test: paraphrase ranks dedup first → Task 3 `test_ranks_dedup_first`. ✓
- Dependency added → Task 2 Step 1. ✓
- README usage → Task 5. ✓

**Placeholder scan:** No TBD/TODO; every code step has full code. ✓

**Type consistency:** `build_cards(store) -> list[dict]` with keys `iri/type/title/snippet/card_text` used identically in Tasks 1–2; `build_embeddings(store, out_dir) -> meta` consumed in Tasks 2–4; `semantic_search(query, limit, npz_path)` result keys `iri/type/title/snippet/score` consistent across Task 3 test + tool. `MODEL`/`DIM` defined once in `embed.py`, referenced by test. ✓
