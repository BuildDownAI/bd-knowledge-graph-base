"""Doc pages become searchable cards (AII-345).

Run: PYTHONPATH=. ./.venv/bin/python tests/test_doc_cards.py

Two properties: (1) doc_meta extracts title/snippet from frontmatter (.mdx),
H1 (.md), and falls back to the filename; (2) a Doc node carrying
dcterms:title + kg:detail produces a "Doc" card from build_cards, so published
documentation is findable by meaning.
"""
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from kg_ingest import iris, cards
from kg_ingest.spine import doc_meta

KG = iris.KG
DCT = URIRef("http://purl.org/dc/terms/")


class _Store:
    def __init__(self, g: Graph):
        self._g = g

    def select(self, q: str):
        rows = []
        for r in self._g.query(q):
            rows.append({str(k): ("" if v is None else str(v))
                         for k, v in zip(r.labels, r)})
        return rows


def test_doc_meta():
    t, s = doc_meta('---\ntitle: "SSO Setup"\n---\n\nConfigure Google OAuth here.', "sso")
    assert t == "SSO Setup" and "Google OAuth" in s, (t, s)
    t, s = doc_meta("# Quick Start\n\nInstall the thing.", "quickstart")
    assert t == "Quick Start" and s == "Install the thing.", (t, s)
    t, s = doc_meta("no headings at all", "fallback-name")
    assert t == "fallback-name" and s == "no headings at all", (t, s)
    print("test_doc_meta: OK")


def test_doc_card_built():
    g = Graph()
    d = URIRef(f"{iris.NAMESPACE}resource/doc/org%2Fdocs/latest%2Fsetup%2Fsso.mdx")
    g.add((d, RDF.type, KG.Doc))
    g.add((d, URIRef(str(DCT) + "title"), Literal("SSO Setup")))
    g.add((d, KG.detail, Literal("Register the redirect URI in the console.")))
    g.add((d, KG.path, Literal("latest/setup/sso.mdx")))
    out = cards.build_cards(_Store(g))
    docs = [c for c in out if c["type"] == "Doc"]
    assert len(docs) == 1, out
    assert docs[0]["title"] == "SSO Setup"
    assert "redirect URI" in docs[0]["card_text"]
    print("test_doc_card_built: OK")


if __name__ == "__main__":
    test_doc_meta()
    test_doc_card_built()
