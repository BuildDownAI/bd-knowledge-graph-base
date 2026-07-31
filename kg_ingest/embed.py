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
        # A fresh KG (no Learnings/Decisions/Issues yet) has nothing to embed.
        # Skip without writing a sidecar: hybrid search degrades to lexical-only
        # until content exists, exactly like the sidecar-absent case.
        n_quads = int(store.select("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")[0]["n"])
        return {"model": MODEL, "dim": DIM, "count": 0,
                "built_from_quads": n_quads, "card_fields": CARD_FIELDS,
                "skipped": "no Learning/Decision/Issue cards in the graph yet"}
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
