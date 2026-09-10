"""doc_exclude glob filtering in add_spine (KGB-18).

Creates a fixture git repo with three doc files and verifies that
doc_exclude suppresses matching paths — no Doc or File node emitted,
stats["docs_excluded"] incremented.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_spine.py
Also verified under a custom namespace (KGB-6 gate).
"""
import subprocess
import tempfile
from pathlib import Path

from rdflib import Graph
from rdflib.namespace import RDF

from kg_ingest import iris
from kg_ingest.spine import add_spine

KG = iris.KG


def _make_fixture_repo(tmp: Path) -> Path:
    """Create a minimal git repo with a.md, sub/b.md, latest/c.mdx."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"],
                   check=True, capture_output=True)
    (repo / "a.md").write_text("# A\nContent of a.")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.md").write_text("# B\nContent of b.")
    (repo / "latest").mkdir()
    (repo / "latest" / "c.mdx").write_text("# C\nContent of c.")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "fixture"],
                   check=True, capture_output=True)
    return repo


def _doc_paths(g: Graph, slug: str) -> set[str]:
    """Return the kg:path values of all kg:Doc nodes for this repo slug."""
    from rdflib import Literal
    paths = set()
    for node in g.subjects(RDF.type, KG.Doc):
        for _, _, p in g.triples((node, KG.path, None)):
            paths.add(str(p))
    return paths


def _file_paths(g: Graph) -> set[str]:
    paths = set()
    for node in g.subjects(RDF.type, KG.File):
        for _, _, p in g.triples((node, KG.path, None)):
            paths.add(str(p))
    return paths


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        repo = _make_fixture_repo(tmp)
        slug = "test-org/fixture"

        # --- Test 1: with doc_exclude — only a.md should become a Doc node ---
        g1 = Graph()
        stats1 = add_spine(g1, repo, slug,
                           max_commits=None, max_prs=0,
                           doc_exclude=["sub/**", "*.mdx"])
        doc_paths1 = _doc_paths(g1, slug)
        assert doc_paths1 == {"a.md"}, \
            f"Expected only a.md as Doc, got: {doc_paths1}"
        assert "sub/b.md" not in _file_paths(g1), \
            "sub/b.md should produce no File node when excluded"
        assert "latest/c.mdx" not in _file_paths(g1), \
            "latest/c.mdx should produce no File node when excluded"
        assert stats1.get("docs_excluded") == 2, \
            f"Expected docs_excluded=2, got: {stats1.get('docs_excluded')}"
        print(f"[PASS] doc_exclude: 1 Doc node, docs_excluded={stats1['docs_excluded']}")

        # --- Test 2: no doc_exclude — all three paths become Doc nodes ---
        g2 = Graph()
        stats2 = add_spine(g2, repo, slug,
                           max_commits=None, max_prs=0)
        doc_paths2 = _doc_paths(g2, slug)
        assert doc_paths2 == {"a.md", "sub/b.md", "latest/c.mdx"}, \
            f"Expected all three as Doc nodes, got: {doc_paths2}"
        assert stats2.get("docs_excluded") is None or stats2.get("docs_excluded") == 0, \
            f"Expected no docs_excluded counter, got: {stats2.get('docs_excluded')}"
        print(f"[PASS] no doc_exclude: {len(doc_paths2)} Doc nodes (regression guard)")

        # --- Test 3: doc_exclude=None is equivalent to no doc_exclude ---
        g3 = Graph()
        stats3 = add_spine(g3, repo, slug,
                           max_commits=None, max_prs=0,
                           doc_exclude=None)
        assert _doc_paths(g3, slug) == {"a.md", "sub/b.md", "latest/c.mdx"}, \
            "doc_exclude=None should behave identically to omitting it"
        print("[PASS] doc_exclude=None: same as omitted")

    print(f"[OK] namespace={iris.NAMESPACE}")


if __name__ == "__main__":
    main()
