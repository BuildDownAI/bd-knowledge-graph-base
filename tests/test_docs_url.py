"""Test that kg:docsUrl is stamped on Repo nodes and survives the snapshot round-trip.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_docs_url.py
"""
import tempfile
from pathlib import Path

from rdflib import Graph, Literal
from rdflib.namespace import RDF

from kg_ingest import iris, spine
from kg_ingest.snapshot import write_parts

KG = iris.KG


def _make_repo_graph(docs_url=None):
    """Build a minimal spine graph for a repo, optionally with docs_url."""
    g = Graph()
    # Use max_commits=0 and max_prs=0 to avoid network calls; git ls-files still runs
    # on the current directory (which is a valid git repo during test execution).
    spine.add_spine(g, Path("."), "test-org/test-repo",
                    max_commits=0, max_prs=0, docs_url=docs_url)
    return g


def test_docs_url_triple_present_and_roundtrip():
    """kg:docsUrl triple is present in the graph and survives snapshot → materialize."""
    url = "https://docs.example.com/test-project"
    g = _make_repo_graph(docs_url=url)

    repo_iri = iris.repo("test-org/test-repo")
    assert (repo_iri, KG.docsUrl, Literal(url)) in g, \
        "kg:docsUrl triple should be present after add_spine"

    # snapshot → materialize round-trip via N-Triples parts
    with tempfile.TemporaryDirectory() as tmpdir:
        parts_dir = Path(tmpdir) / "parts"
        write_parts(g, parts_dir)

        g2 = Graph()
        for nt_file in sorted(parts_dir.glob("*.nt")):
            g2.parse(str(nt_file), format="nt")

        results = list(g2.query(
            f"SELECT ?url WHERE {{ <{repo_iri}> <{KG.docsUrl}> ?url }}"
        ))
        assert len(results) == 1, \
            f"Expected exactly 1 kg:docsUrl triple after round-trip, got {len(results)}"
        assert str(results[0].url) == url, \
            f"Expected URL {url!r}, got {str(results[0].url)!r}"


def test_docs_url_absent_when_not_configured():
    """No kg:docsUrl triple is added when docs_url is not provided."""
    g = _make_repo_graph(docs_url=None)

    repo_iri = iris.repo("test-org/test-repo")
    results = list(g.query(
        f"SELECT ?url WHERE {{ <{repo_iri}> <{KG.docsUrl}> ?url }}"
    ))
    assert len(results) == 0, \
        f"Expected no kg:docsUrl triple when docs_url is absent, got {len(results)}"


if __name__ == "__main__":
    test_docs_url_triple_present_and_roundtrip()
    print("PASS: kg:docsUrl triple present when configured, survives snapshot round-trip")
    test_docs_url_absent_when_not_configured()
    print("PASS: no kg:docsUrl triple when docs_url not configured")
