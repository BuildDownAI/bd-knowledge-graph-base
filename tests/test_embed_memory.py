"""Memory-bounded embedding test: 5k synthetic cards under a virtual-memory cap.

Proves that the batched embed loop does not scale peak memory with card count.
Uses resource.setrlimit(RLIMIT_AS) to cap virtual address space at 512 MB —
this is "does not grow to multi-GB" proof, not "fits in 64 MB".
The fastembed model is stubbed so no ONNX download is required.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_embed_memory.py
"""
import importlib
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

    _dim = 384  # overridden inside test_embed_memory from the imported embed module

    def __init__(self, model_name: str, **_kwargs):
        self._rng = np.random.default_rng(42)

    def embed(self, texts, **_kwargs):
        for _ in texts:
            yield self._rng.random(self.__class__._dim).astype(np.float32)


_fastembed_mod = types.ModuleType("fastembed")
_fastembed_mod.TextEmbedding = _FakeTextEmbedding  # type: ignore[attr-defined]  # patching a dynamic module attribute at test setup time


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


def test_embed_memory():
    # monkeypatch.setitem on sys.modules is unreliable when the import
    # machinery re-registers fastembed during the test; use explicit
    # save/restore with try/finally so cleanup is guaranteed.
    _NOT_SET = object()
    _original_fastembed = sys.modules.get("fastembed", _NOT_SET)
    sys.modules["fastembed"] = _fastembed_mod
    try:
        import kg_ingest.embed as _embed_mod
        importlib.reload(_embed_mod)

        BATCH_SIZE = _embed_mod.BATCH_SIZE
        DIM = _embed_mod.DIM
        build_embeddings = _embed_mod.build_embeddings
        _FakeTextEmbedding._dim = DIM

        # Pre-warm numpy.random so _bounded_integers.so is mapped before the cap.
        # In a full test suite run other modules consume virtual address space, and
        # numpy's lazy .so load fails at 512 MB if not pre-initialized.
        np.random.default_rng(0)

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
    finally:
        if _original_fastembed is _NOT_SET:
            sys.modules.pop("fastembed", None)
        else:
            sys.modules["fastembed"] = _original_fastembed


if __name__ == "__main__":
    test_embed_memory()
