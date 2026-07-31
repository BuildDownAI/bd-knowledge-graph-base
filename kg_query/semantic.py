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
        from fastembed import TextEmbedding

        data = np.load(npz_path, allow_pickle=False)
        self.vectors = data["vectors"]
        self.iris = data["iris"]
        self.types = data["types"]
        self.titles = data["titles"]
        self.snippets = data["snippets"]
        self.meta = json.loads(str(data["meta"]))
        # Cache the model to avoid reloading on every query
        self.model = TextEmbedding(model_name=self.meta["model"])


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

    idx = _load(npz_path)
    qv = np.asarray(next(iter(idx.model.embed([query]))), dtype=np.float32)
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
