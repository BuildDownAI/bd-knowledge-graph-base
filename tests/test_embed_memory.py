"""Memory-bounded embedding test: 5k synthetic cards under a virtual-memory cap.

Proves that the batched embed loop does not scale peak memory with card count.
Uses resource.setrlimit(RLIMIT_AS) to cap virtual address space at 512 MB —
this is "does not grow to multi-GB" proof, not "fits in 64 MB".
The fastembed model is stubbed so no ONNX download is required.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_embed_memory.py
"""
import resource
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import numpy as np

# 512 MB virtual address cap — proves "does not grow to multi-GB", not "fits in 64 MB"
VIRT_CAP = 512 * 1024 * 1024
N_CARDS = 5_000


class _FakeTextEmbedding:
    """Fastembed-compatible stub: yields deterministic float32 vectors, no ONNX."""

    def __init__(self, model_name: str, **_kwargs):
        self._rng = np.random.default_rng(42)

    def embed(self, texts, **_kwargs):
        for _ in texts:
            yield self._rng.random(DIM).astype(np.float32)


# Pre-install a fastembed stub so kg_ingest.embed's lazy import resolves without
# downloading ONNX weights. Set TextEmbedding on the stub before importing
# kg_ingest so the class is present when embed.py's import runs.
_fastembed_mod = types.ModuleType("fastembed")
_fastembed_mod.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]
sys.modules["fastembed"] = _fastembed_mod

import kg_ingest.cards  # ensure module is in sys.modules before patch resolves it
from kg_ingest.embed import BATCH_SIZE, DIM, build_embeddings  # noqa: E402


def _fake_store(n_quads: int = 21_000):
    store = types.SimpleNamespace()
    store.select = lambda _q: [{"n": str(n_quads)}]
    return store


def _synthetic_cards(n: int):
    snippet = "x" * 400
    return [
        {
            "iri": f"https://example.org/kg/resource/section/{i}",
            "type": "DocSection",
            "title": f"Section {i}",
            "snippet": snippet,
            "card_text": f"Section {i} " + snippet,
        }
        for i in range(n)
    ]


def main():
    # The virtual-memory cap is best-effort: macOS refuses to lower RLIMIT_AS
    # (ValueError), so the cap is enforced only where the platform allows it —
    # in practice the Linux CI legs. The functional batching assertions below
    # run everywhere regardless.
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    new_soft = VIRT_CAP
    if hard != resource.RLIM_INFINITY and hard < new_soft:
        new_soft = hard
    capped = True
    try:
        resource.setrlimit(resource.RLIMIT_AS, (new_soft, hard))
    except (ValueError, OSError):
        capped = False

    try:
        with tempfile.TemporaryDirectory() as d:
            with patch("kg_ingest.cards.build_cards", return_value=_synthetic_cards(N_CARDS)):
                meta = build_embeddings(_fake_store(), out_dir=Path(d))
    finally:
        if capped:
            resource.setrlimit(resource.RLIMIT_AS, (soft, hard))

    assert meta["count"] == N_CARDS, meta
    expected_batches = (N_CARDS + BATCH_SIZE - 1) // BATCH_SIZE
    assert meta["batch_count"] == expected_batches, meta
    cap_note = (f"within {new_soft // (1024 * 1024)} MB virtual cap" if capped
                else "cap not settable on this platform (enforced in Linux CI)")
    print(f"PASS: {N_CARDS} synthetic cards in {meta['batch_count']} batches {cap_note}")


if __name__ == "__main__":
    main()
