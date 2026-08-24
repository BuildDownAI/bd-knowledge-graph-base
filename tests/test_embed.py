"""Build embeddings over a fixture graph; assert the sidecar files + meta.
Downloads the model on first run. Gated on fastembed.
Run: PYTHONPATH=. ./.venv/bin/python tests/test_embed.py
"""
import json
import os
import tempfile
import zipfile
from pathlib import Path

try:
    import fastembed  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

from kg_query.store import RdflibStore
from kg_ingest.iris import DEFAULT_NAMESPACE as _DNS, NAMESPACE as _NS
from kg_ingest.embed import build_embeddings, MODEL, DIM

STAMP = "2026-08-01T00:00:00+00:00"

FIXTURE_RAW = """
@prefix kg: <https://example.org/kg/onto#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<https://example.org/kg/resource/graph/spine> {
  <https://example.org/kg/resource/graph/spine> dcterms:modified "STAMP_PLACEHOLDER"^^xsd:dateTime .
  <https://example.org/kg/resource/comment/PROJ-259/0> a kg:Learning ;
    dcterms:title "dedup has no TTL" ; kg:fix "Add a TTL to dispatch dedup." ;
    kg:tagged <https://example.org/kg/resource/topic/dedup> .
  <https://example.org/kg/resource/topic/dedup> a kg:Topic ; skos:prefLabel "dedup" .
  <https://example.org/kg/resource/adr/0001> a kg:Decision ;
    dcterms:title "Use rdflib" ; kg:detail "Zero-infra local dev." .
}
"""
FIXTURE = FIXTURE_RAW.replace(_DNS, _NS).replace("STAMP_PLACEHOLDER", STAMP)

def main():
    if not HAVE:
        print("SKIP: fastembed not installed"); return
    d = tempfile.mkdtemp()
    snap = os.path.join(d, "snapshot")
    trig = os.path.join(d, "g.trig")
    Path(trig).write_text(FIXTURE)
    meta = build_embeddings(RdflibStore(trig), out_dir=Path(d), snapshot_dir=Path(snap))

    # out/ files (server reads these)
    assert (Path(d) / "embeddings.npz").exists()
    assert (Path(d) / "embeddings.meta.json").exists()

    # snapshot/ files (committed, consumers copy these)
    snap_npz = Path(snap) / "embeddings.npz"
    snap_meta_path = Path(snap) / "embeddings.meta.json"
    assert snap_npz.exists(), "snapshot/embeddings.npz not written"
    assert snap_meta_path.exists(), "snapshot/embeddings.meta.json not written"

    # Every array in the compressed archive must be deflated (not stored raw).
    # At derivative scale (~1,463 cards) this is the difference between 2.5 MB and 12 MB.
    with zipfile.ZipFile(snap_npz) as zf:
        for info in zf.infolist():
            assert info.compress_type != 0, (
                f"{info.filename} is not compressed (compress_type=0); "
                "snapshot/embeddings.npz must use np.savez_compressed"
            )

    # metadata checks
    assert meta["model"] == MODEL and meta["dim"] == DIM
    assert meta["count"] == 2, meta
    assert meta["batch_count"] >= 1, meta
    assert meta["age_stamp"] == STAMP, f"age_stamp mismatch: {meta['age_stamp']!r} != {STAMP!r}"

    # on-disk out/ meta matches returned dict
    on_disk = json.loads((Path(d) / "embeddings.meta.json").read_text())
    assert on_disk == meta

    print(f"PASS: build_embeddings -> {meta['count']} vectors dim {meta['dim']} "
          f"batch_count {meta['batch_count']} age_stamp {meta['age_stamp']!r}")

if __name__ == "__main__":
    main()
