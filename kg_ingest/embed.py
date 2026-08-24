"""Build the semantic-search embedding sidecar from out/graph.trig.

Reads the graph, builds one compact index card per Learning/Decision/Issue
(kg_ingest.cards), embeds each with fastembed (BAAI/bge-small-en-v1.5, ONNX, no
torch), and writes out/embeddings.npz + out/embeddings.meta.json (both gitignored).
Also writes snapshot/embeddings.npz (compressed) and snapshot/embeddings.meta.json
(tracked by git) so consumers can copy the vectors without re-running inference.
fastembed/numpy are imported lazily so the core ingest never depends on them.

Run: python -m kg_ingest.embed
"""
from __future__ import annotations

import json
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "out"
SNAP_DIR = Path(__file__).resolve().parent.parent / "snapshot"
MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384
CARD_FIELDS = "title + tags/labels + <=400-char snippet"
# Embed in fixed-size slices so peak memory is one pre-allocated output array
# plus one batch of ONNX buffers, independent of total card count.
# Tune here if the image memory envelope changes; 64 × bge-small is ~10 MB/batch.
BATCH_SIZE = 64


def build_embeddings(store, out_dir: Path = OUT_DIR, snapshot_dir: Path = SNAP_DIR) -> dict:
    import gc
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

    # Query n_quads and age_stamp before releasing the graph handle.
    n_quads = int(store.select("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")[0]["n"])
    # RdflibStore flattens named graphs to a union, so query without GRAPH clause.
    from . import iris as _iris
    from rdflib.namespace import DCTERMS
    _g_spine = str(_iris.G_SPINE)
    _stamp_rows = store.select(
        f"SELECT ?stamp WHERE {{ <{_g_spine}> <{str(DCTERMS.modified)}> ?stamp }}"
    )
    age_stamp = _stamp_rows[0]["stamp"] if _stamp_rows else ""

    # Free the rdflib graph; cards contain everything needed for embedding.
    del store
    gc.collect()

    texts = [c["card_text"] for c in cards]
    n = len(texts)

    # Instantiate once to avoid reloading ONNX weights on every batch.
    model = TextEmbedding(model_name=MODEL)

    # Pre-allocate the full output array; fill BATCH_SIZE rows at a time so the
    # only live allocation beyond this array is a single batch of intermediate buffers.
    vecs = np.zeros((n, DIM), dtype=np.float32)
    batch_count = 0
    for start in range(0, n, BATCH_SIZE):
        batch = texts[start:start + BATCH_SIZE]
        vecs[start:start + len(batch)] = np.asarray(list(model.embed(batch)), dtype=np.float32)
        batch_count += 1

    vecs /= (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12)
    assert vecs.shape[1] == DIM, (vecs.shape, DIM)

    meta = {"model": MODEL, "dim": int(vecs.shape[1]), "count": len(cards),
            "built_from_quads": n_quads, "card_fields": CARD_FIELDS,
            "batch_count": batch_count, "age_stamp": age_stamp}

    arrays = dict(
        vectors=vecs,
        iris=np.array([c["iri"] for c in cards]),
        types=np.array([c["type"] for c in cards]),
        titles=np.array([c["title"] for c in cards]),
        snippets=np.array([c["snippet"] for c in cards]),
        meta=json.dumps(meta),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez(out_dir / "embeddings.npz", **arrays)
    (out_dir / "embeddings.meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    # Compressed committed snapshot — consumers copy this instead of re-running inference.
    # String columns (snippets) compress especially well; ~2.5 MB vs 12 MB uncompressed.
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(snapshot_dir / "embeddings.npz", **arrays)
    (snapshot_dir / "embeddings.meta.json").write_text(json.dumps(meta, indent=2) + "\n")

    return meta


def main() -> None:
    from kg_query.store import get_store
    meta = build_embeddings(get_store())
    if meta.get("skipped"):
        print(f"embeddings skipped: {meta['skipped']}")
    else:
        print(f"embedded {meta['count']} cards in {meta['batch_count']} batches "
              f"({meta['model']}, dim {meta['dim']}) "
              f"-> {OUT_DIR}/embeddings.npz + embeddings.meta.json")


if __name__ == "__main__":
    main()
