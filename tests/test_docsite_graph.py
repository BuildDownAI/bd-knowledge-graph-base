"""KGB-4: DocSite/DocPage/DocSection graph model, SHACL conformance, and cards.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_docsite_graph.py
"""
import tempfile
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD, Namespace
from pyshacl import validate

from kg_ingest import iris, ontology
from kg_ingest.cards import build_cards, DOCSECTION_SNIPPET_CHARS
from kg_ingest.snapshot import write_parts

KG = iris.KG
DCTERMS = Namespace("http://purl.org/dc/terms/")

_ROOT = "https://docs.example.com"
_PAGE_URL = "https://docs.example.com/guide"
_ANCHOR = "getting-started"


class _Store:
    def __init__(self, g: Graph):
        self._g = g

    def select(self, q: str):
        rows = []
        for r in self._g.query(q):
            rows.append({str(k): ("" if v is None else str(v))
                         for k, v in zip(r.labels, r)})
        return rows


def _build_fixture_graph() -> Graph:
    g = Graph()

    site_iri = iris.doc_site(_ROOT)
    page_iri = iris.doc_page(_PAGE_URL)
    sec_iri = iris.doc_section(_PAGE_URL, _ANCHOR)

    _fetched_at = Literal("2024-01-01T00:00:00Z", datatype=XSD.dateTime)

    g.add((site_iri, RDF.type, KG.DocSite))
    g.add((site_iri, KG.rootUrl, Literal(_ROOT)))
    g.add((site_iri, DCTERMS.title, Literal("Example Docs")))
    g.add((site_iri, DCTERMS.modified, _fetched_at))

    g.add((page_iri, RDF.type, KG.DocPage))
    g.add((page_iri, KG.url, Literal(_PAGE_URL)))
    g.add((page_iri, DCTERMS.title, Literal("Guide")))
    g.add((page_iri, KG.contentHash, Literal("abc123")))
    g.add((page_iri, KG.partOf, site_iri))
    g.add((page_iri, DCTERMS.modified, _fetched_at))

    g.add((sec_iri, RDF.type, KG.DocSection))
    g.add((sec_iri, KG.heading, Literal("Getting Started")))
    g.add((sec_iri, KG.level, Literal(1, datatype=XSD.integer)))
    g.add((sec_iri, KG.anchor, Literal(_ANCHOR)))
    g.add((sec_iri, KG.text, Literal("Learn how to get started with the guide.")))
    g.add((sec_iri, KG.partOf, page_iri))

    return g


def test_triples_produced():
    """DocSite/DocPage/DocSection triples are present with correct structure."""
    g = _build_fixture_graph()

    site_iri = iris.doc_site(_ROOT)
    page_iri = iris.doc_page(_PAGE_URL)
    sec_iri = iris.doc_section(_PAGE_URL, _ANCHOR)

    assert (site_iri, RDF.type, KG.DocSite) in g
    assert (site_iri, KG.rootUrl, Literal(_ROOT)) in g

    assert (page_iri, RDF.type, KG.DocPage) in g
    assert (page_iri, KG.partOf, site_iri) in g
    assert (page_iri, KG.contentHash, Literal("abc123")) in g

    assert (sec_iri, RDF.type, KG.DocSection) in g
    assert (sec_iri, KG.partOf, page_iri) in g
    assert (sec_iri, KG.anchor, Literal(_ANCHOR)) in g
    assert (sec_iri, KG.level, Literal(1, datatype=XSD.integer)) in g


def test_shacl_conforms():
    """DocSite/DocPage/DocSection triples satisfy SHACL shapes and the
    SemanticNodeShape provenance rule does not fire on these spine classes."""
    g = _build_fixture_graph()
    shapes = ontology.load("shapes.ttl")
    onto = ontology.load("kg.ttl")
    g_with_onto = g + onto
    conforms, _, txt = validate(g_with_onto, shacl_graph=shapes,
                                inference="rdfs", abort_on_first=False)
    assert conforms, f"SHACL should conform for DocSite/DocPage/DocSection:\n{txt[:2000]}"


def test_section_card_iri():
    """DocSection IRI ends with #<anchor>."""
    sec_iri = iris.doc_section(_PAGE_URL, _ANCHOR)
    assert sec_iri is not None
    assert str(sec_iri).endswith(f"#{_ANCHOR}")


def test_preamble_guard_returns_none():
    """doc_section with empty anchor (preamble) returns None — no spurious IRI minted."""
    assert iris.doc_section(_PAGE_URL, "") is None


def test_iri_stability():
    """Same inputs always produce the same IRI (re-ingest idempotency)."""
    assert iris.doc_site(_ROOT) == iris.doc_site(_ROOT)
    assert iris.doc_page(_PAGE_URL) == iris.doc_page(_PAGE_URL)
    assert iris.doc_section(_PAGE_URL, _ANCHOR) == iris.doc_section(_PAGE_URL, _ANCHOR)


def test_materialize_roundtrip():
    """DocSection triples survive snapshot (write_parts) → reload round-trip."""
    g = _build_fixture_graph()
    with tempfile.TemporaryDirectory() as tmpdir:
        parts_dir = Path(tmpdir) / "parts"
        write_parts(g, parts_dir)

        g2 = Graph()
        for nt_file in sorted(parts_dir.glob("*.nt")):
            g2.parse(str(nt_file), format="nt")

        sec_iri = iris.doc_section(_PAGE_URL, _ANCHOR)
        results = list(g2.query(
            f"SELECT ?text WHERE {{ <{sec_iri}> <{KG.text}> ?text }}"
        ))
        assert len(results) == 1, f"Expected 1 kg:text triple after round-trip, got {len(results)}"
        assert "get started" in str(results[0].text).lower()


def test_docsection_card():
    """DocSection card has type='DocSection', correct title format, and IRI with #anchor."""
    g = _build_fixture_graph()
    all_cards = build_cards(_Store(g))

    sec_cards = [c for c in all_cards if c["type"] == "DocSection"]
    assert len(sec_cards) == 1, f"Expected 1 DocSection card, got {len(sec_cards)}"

    card = sec_cards[0]
    assert card["title"] == "Guide \u203a Getting Started"
    assert str(card["iri"]).endswith(f"#{_ANCHOR}")
    assert "get started" in card["snippet"].lower()


def test_docpage_card():
    """DocPage card is built for each crawled page."""
    g = _build_fixture_graph()
    # Add preamble text to the page
    page_iri = iris.doc_page(_PAGE_URL)
    g.add((page_iri, KG.detail, Literal("Intro text before first heading.")))

    all_cards = build_cards(_Store(g))
    page_cards = [c for c in all_cards if c["type"] == "DocPage"]
    assert len(page_cards) == 1, f"Expected 1 DocPage card, got {len(page_cards)}"
    assert page_cards[0]["title"] == "Guide"
    assert "Intro text" in page_cards[0]["snippet"]


def test_docsection_snippet_cap():
    """DocSection card snippet is capped at DOCSECTION_SNIPPET_CHARS."""
    g = Graph()
    site_iri = iris.doc_site(_ROOT)
    page_iri = iris.doc_page(_PAGE_URL)
    sec_iri = iris.doc_section(_PAGE_URL, _ANCHOR)

    g.add((site_iri, RDF.type, KG.DocSite))
    g.add((site_iri, KG.rootUrl, Literal(_ROOT)))
    g.add((site_iri, DCTERMS.title, Literal("Docs")))
    g.add((page_iri, RDF.type, KG.DocPage))
    g.add((page_iri, KG.url, Literal(_PAGE_URL)))
    g.add((page_iri, DCTERMS.title, Literal("Guide")))
    g.add((page_iri, KG.contentHash, Literal("x")))
    g.add((page_iri, KG.partOf, site_iri))
    long_text = "word " * 500
    g.add((sec_iri, RDF.type, KG.DocSection))
    g.add((sec_iri, KG.heading, Literal("Long Section")))
    g.add((sec_iri, KG.level, Literal(1, datatype=XSD.integer)))
    g.add((sec_iri, KG.anchor, Literal(_ANCHOR)))
    g.add((sec_iri, KG.text, Literal(long_text)))
    g.add((sec_iri, KG.partOf, page_iri))

    all_cards = build_cards(_Store(g))
    sec_cards = [c for c in all_cards if c["type"] == "DocSection"]
    assert len(sec_cards) == 1
    assert len(sec_cards[0]["snippet"]) <= DOCSECTION_SNIPPET_CHARS


def test_duplicate_heading_anchors_dedupe():
    """Two same-slug headings on one page must yield DISTINCT DocSection IRIs
    (-2 suffix, matching site-generator behaviour) — not one merged node."""
    from kg_ingest.cli import _ingest_docs_sites
    from unittest.mock import patch
    from kg_ingest.docsite import Page, Section

    page = Page(url=f"{_ROOT}/dup", title="Dup",
                sections=[Section(heading="Required", level=2, text="first block"),
                          Section(heading="Required", level=2, text="second block")],
                fetched_at="2024-01-01T00:00:00Z", content_hash="h1")
    g = Graph()
    with patch("kg_ingest.docsite.crawl_site", return_value=[page]):
        stats = _ingest_docs_sites(g, [{"url": _ROOT}])
    secs = sorted(g.subjects(RDF.type, KG.DocSection))
    assert stats["docsite_sections"] == 2
    assert len(secs) == 2, f"expected 2 distinct section IRIs, got {len(secs)}"
    anchors = sorted(str(g.value(s, KG.anchor)) for s in secs)
    assert anchors == ["required", "required-2"], anchors
    texts = {str(g.value(s, KG.text)) for s in secs}
    assert texts == {"first block", "second block"}


if __name__ == "__main__":
    test_triples_produced()
    print("PASS: triples produced")
    test_shacl_conforms()
    print("PASS: SHACL conforms")
    test_section_card_iri()
    print("PASS: section card IRI ends with #anchor")
    test_preamble_guard_returns_none()
    print("PASS: preamble guard returns None")
    test_iri_stability()
    print("PASS: IRI stability")
    test_materialize_roundtrip()
    print("PASS: materialize round-trip")
    test_docsection_card()
    print("PASS: DocSection card")
    test_docpage_card()
    print("PASS: DocPage card")
    test_docsection_snippet_cap()
    print("PASS: DocSection snippet cap")
    test_duplicate_heading_anchors_dedupe()
    print("PASS: duplicate heading anchors dedupe")
