"""Tests for kg_ingest.mcp_source (KGB-12).

Run: PYTHONPATH=. python tests/test_mcp_source.py
Requires: mcp>=1.2,<2  (exits 0 with SKIP message when absent)
"""
from __future__ import annotations

import asyncio
import sys

try:
    import mcp  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False


# ── Fixture resources ──────────────────────────────────────────────────────────

FIXTURE_RESOURCES: dict[str, tuple[str, str]] = {
    "resource://guide": (
        "text/markdown",
        "# Getting Started\n\nWelcome to the guide.\n\n## Installation\n\nRun pip install.\n\n## Usage\n\nImport and call.\n",
    ),
    "resource://reference": (
        "text/markdown",
        "## Overview\n\nNo h1 in this document.\n\n## Details\n\nMore details here.\n",
    ),
    "resource://plain": (
        "text/plain",
        "Just plain text with no headings at all.",
    ),
}


def _make_fixture_server():
    from mcp.server.lowlevel import Server
    from mcp.server.lowlevel.server import ReadResourceContents
    from mcp import types

    server = Server("fixture-server")

    @server.list_resources()
    async def list_resources():
        return [
            types.Resource(uri=uri, name=uri.split("://", 1)[-1], mimeType=mime)
            for uri, (mime, _) in FIXTURE_RESOURCES.items()
        ]

    @server.read_resource()
    async def read_resource(uri):
        uri_str = str(uri)
        if uri_str not in FIXTURE_RESOURCES:
            raise ValueError(f"Unknown resource: {uri_str}")
        mime, text = FIXTURE_RESOURCES[uri_str]
        return [ReadResourceContents(content=text, mime_type=mime)]

    return server


# ── Helper ─────────────────────────────────────────────────────────────────────

async def _ingest_fixture(server):
    from mcp.shared.memory import create_connected_server_and_client_session
    from kg_ingest.mcp_source import _ingest_from_session, McpSourceConfig

    config = McpSourceConfig(name="fixture", max_resources=200)
    async with create_connected_server_and_client_session(server) as session:
        return await _ingest_from_session(session, config)


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_lazy_import() -> None:
    """Importing kg_ingest.mcp_source must not import mcp at module level."""
    import importlib

    for key in list(sys.modules):
        if key.startswith("kg_ingest.mcp_source"):
            del sys.modules[key]

    mcp_saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k == "mcp" or k.startswith("mcp.")}
    try:
        import kg_ingest.mcp_source  # must not raise ImportError
        assert hasattr(kg_ingest.mcp_source, "_parse_sections")
        assert hasattr(kg_ingest.mcp_source, "_ingest_from_session")
        assert hasattr(kg_ingest.mcp_source, "fetch_resources")
    finally:
        sys.modules.update(mcp_saved)

    print("PASS: test_lazy_import")


def test_parse_sections() -> None:
    """_parse_sections: title precedence, preamble level-0, no-h1 fallback."""
    from kg_ingest.mcp_source import _parse_sections

    # H1 becomes title; text directly under h1 is preamble (level 0)
    title, sections = _parse_sections(
        "# My Title\n\nPreamble text.\n\n## Section A\n\nBody A.", "resource://x"
    )
    assert title == "My Title", f"expected 'My Title', got {title!r}"
    preamble = [s for s in sections if s.level == 0]
    assert preamble and "Preamble text." in preamble[0].text, "missing preamble section"
    assert any(s.heading == "Section A" and s.level == 2 for s in sections)

    # No h1: title falls back to URI tail
    title2, sections2 = _parse_sections(
        "## Just h2\n\nSome body.", "resource://fallback-doc"
    )
    assert title2 == "fallback-doc", f"expected 'fallback-doc', got {title2!r}"
    assert any(s.heading == "Just h2" for s in sections2)

    # No headings at all: single section containing the full body, title from URI tail
    title3, sections3 = _parse_sections(
        "Plain text here.", "resource://plain-thing"
    )
    assert title3 == "plain-thing", f"expected 'plain-thing', got {title3!r}"
    assert len(sections3) == 1, f"expected 1 section, got {len(sections3)}"
    assert sections3[0].text == "Plain text here."

    print("PASS: test_parse_sections")


def test_end_to_end() -> None:
    """_ingest_from_session: page count, section cards, content_hash present."""
    pages = asyncio.run(_ingest_fixture(_make_fixture_server()))

    assert len(pages) == len(FIXTURE_RESOURCES), (
        f"expected {len(FIXTURE_RESOURCES)} pages, got {len(pages)}"
    )
    by_uri = {p.uri: p for p in pages}

    # guide: h1 → title, text under h1 → preamble, two h2 sections
    guide = by_uri["resource://guide"]
    assert guide.title == "Getting Started", f"got {guide.title!r}"
    assert any(s.level == 0 for s in guide.sections), "guide missing preamble section"
    assert len([s for s in guide.sections if s.level == 2]) == 2

    # reference: no h1 → title from URI tail; two h2 sections
    ref = by_uri["resource://reference"]
    assert ref.title == "reference", f"got {ref.title!r}"
    assert len([s for s in ref.sections if s.level == 2]) == 2

    # plain: no headings → single section with full body
    plain = by_uri["resource://plain"]
    assert len(plain.sections) == 1
    assert plain.sections[0].text == "Just plain text with no headings at all."

    for p in pages:
        assert p.content_hash.startswith("sha256:"), f"{p.uri}: bad content_hash {p.content_hash!r}"

    print("PASS: test_end_to_end")


def test_content_hash_determinism() -> None:
    """Two fetches of identical content produce identical (uri, hash) pairs."""
    pairs1 = {(p.uri, p.content_hash) for p in asyncio.run(_ingest_fixture(_make_fixture_server()))}
    pairs2 = {(p.uri, p.content_hash) for p in asyncio.run(_ingest_fixture(_make_fixture_server()))}
    assert pairs1 == pairs2, f"hash mismatch between runs:\n  run1={pairs1}\n  run2={pairs2}"
    print("PASS: test_content_hash_determinism")


# ── RDF emission helper ────────────────────────────────────────────────────────

def _build_rdf_graph_from_fixture():
    """Run cli._ingest_mcp_sources against the in-process fixture, return the spine Graph.

    Monkey-patches kg_ingest.mcp_source.fetch_resources so the CLI path runs
    without a subprocess while still exercising the full RDF emission code.
    """
    from mcp.shared.memory import create_connected_server_and_client_session
    from kg_ingest.mcp_source import _ingest_from_session, McpSourceConfig
    import kg_ingest.mcp_source as mcp_src_mod
    from kg_ingest import cli
    from rdflib import Graph

    config = McpSourceConfig(name="fixture", max_resources=200)

    async def _run():
        async with create_connected_server_and_client_session(_make_fixture_server()) as session:
            return await _ingest_from_session(session, config)

    pages = asyncio.run(_run())

    orig_fetch = mcp_src_mod.fetch_resources
    mcp_src_mod.fetch_resources = lambda cfg: pages
    try:
        spine_g = Graph()
        cli._ingest_mcp_sources(spine_g, [{"name": "fixture", "max_resources": 200}])
    finally:
        mcp_src_mod.fetch_resources = orig_fetch

    return spine_g


# ── RDF-level tests ────────────────────────────────────────────────────────────

def test_rdf_emission() -> None:
    """cli._ingest_mcp_sources emits kg:McpSource, kg:DocPage, and kg:DocSection triples."""
    from rdflib.namespace import RDF
    from kg_ingest import iris

    spine_g = _build_rdf_graph_from_fixture()
    KG = iris.KG

    mcp_sources = list(spine_g.subjects(RDF.type, KG.McpSource))
    assert mcp_sources, "no kg:McpSource node emitted"

    doc_pages = list(spine_g.subjects(RDF.type, KG.DocPage))
    assert len(doc_pages) >= len(FIXTURE_RESOURCES), (
        f"expected >= {len(FIXTURE_RESOURCES)} kg:DocPage nodes, got {len(doc_pages)}"
    )

    doc_sections = list(spine_g.subjects(RDF.type, KG.DocSection))
    assert doc_sections, "no kg:DocSection nodes emitted"

    # multi-heading resources (guide, reference) must each produce >=1 DocSection
    from kg_ingest.iris import KGR
    from rdflib import Literal, URIRef
    from rdflib.namespace import DCTERMS
    for page_iri in doc_pages:
        url_vals = list(spine_g.objects(page_iri, KG.url))
        if not url_vals:
            continue
        url = str(url_vals[0])
        if url in ("resource://guide", "resource://reference"):
            page_sections = [
                s for s in doc_sections
                if (s, KG.partOf, page_iri) in spine_g
            ]
            assert page_sections, f"no DocSection nodes for multi-heading page {url}"

    print("PASS: test_rdf_emission")


def test_rdf_shacl_conforms() -> None:
    """Graph emitted by cli._ingest_mcp_sources passes SHACL validation."""
    from pyshacl import validate
    from kg_ingest import ontology as ont_mod

    spine_g = _build_rdf_graph_from_fixture()

    shapes = ont_mod.load("shapes.ttl")
    onto = ont_mod.load("kg.ttl")
    union = spine_g + onto
    conforms, _, rtext = validate(
        union, shacl_graph=shapes, inference="rdfs",
        abort_on_first=False, meta_shacl=False,
    )
    assert conforms, f"SHACL violations:\n{rtext[:4000]}"

    print("PASS: test_rdf_shacl_conforms")


# ── Runner ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not HAVE_MCP:
        print("SKIP: mcp not installed — skipping test_mcp_source.py")
        sys.exit(0)

    test_lazy_import()
    test_parse_sections()
    test_end_to_end()
    test_content_hash_determinism()
    test_rdf_emission()
    test_rdf_shacl_conforms()
    print("\nALL TESTS PASSED")
