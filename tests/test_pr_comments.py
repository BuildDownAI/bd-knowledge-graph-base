"""PR comment node builder tests (KGB-7).

Feeds a fixture list of comment dicts into the node builder (no network) and
asserts node types, IRIs, provenance links, and stats.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_pr_comments.py
"""
import subprocess

from rdflib import Graph, Namespace
from rdflib.namespace import RDF, XSD

from kg_ingest import iris
from kg_ingest.iris import KG, KGR
from kg_ingest.spine import _add_pr_comments, _pr_comment_run_node

PROV = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")

REPO_SLUG = "owner/test-repo"
PR_NUMBER = 42

FIXTURE_COMMENTS = [
    {
        "id": 1001,
        "body": (
            "# ai-implement-kg-refresh-learnings\n"
            "**Refresh:** 2026-09-01\n"
            "## What happened\n"
            "Ingest issue during nightly refresh."
        ),
        "user": {"login": "ai-implement[bot]"},
        "created_at": "2026-09-01T10:00:00Z",
    },
    {
        "id": 1002,
        "body": "## \U0001f525 Smoke-Jumper Report\n\nAll checks passed. No regressions detected.",
        "user": {"login": "smoke-jumper[bot]"},
        "created_at": "2026-09-01T11:00:00Z",
    },
    {
        "id": 1003,
        "body": "x " * 250,  # 500 chars, human author -> decision
        "user": {"login": "alice"},
        "created_at": "2026-09-01T12:00:00Z",
    },
    {
        "id": 1004,
        "body": "lgtm",  # short, unclassified -> no node
        "user": {"login": "bob"},
        "created_at": "2026-09-01T13:00:00Z",
    },
]


def check(name: str, got, want) -> None:
    assert got == want, f"FAIL {name}: got {got!r}, want {want!r}"
    print(f"PASS: {name}")


def main():
    g = Graph()
    g.bind("kg", KG)
    g.bind("kgr", KGR)
    g.bind("prov", PROV)
    g.bind("dcterms", DCTERMS)

    run_id = iris.stable_run_id("0.1.0", REPO_SLUG + ":pr-comments")
    run = _pr_comment_run_node(g, run_id, "0.1.0")
    pnode = iris.pr(REPO_SLUG, PR_NUMBER)

    stats = {"pr_comments": 0}
    count = _add_pr_comments(g, run, REPO_SLUG, PR_NUMBER, pnode, FIXTURE_COMMENTS, stats)

    # 3 classified (learning, verification, decision); 1 short is dropped
    check("count returned == 3", count, 3)
    check("stats pr_comments == 3", stats["pr_comments"], 3)

    c1 = iris.pr_comment(REPO_SLUG, PR_NUMBER, 1001)
    c2 = iris.pr_comment(REPO_SLUG, PR_NUMBER, 1002)
    c3 = iris.pr_comment(REPO_SLUG, PR_NUMBER, 1003)
    c4 = iris.pr_comment(REPO_SLUG, PR_NUMBER, 1004)

    # IRI pattern: must end with pr/<encoded-slug>/<pr_number>/comment/<id>
    for cid, node in [(1001, c1), (1002, c2), (1003, c3)]:
        suffix = f"pr/owner%2Ftest-repo/{PR_NUMBER}/comment/{cid}"
        check(f"IRI pattern for comment {cid}", str(node).endswith(suffix), True)

    # Types
    check("comment 1001 is Learning", (c1, RDF.type, KG.Learning) in g, True)
    check("comment 1002 is Verification", (c2, RDF.type, KG.Verification) in g, True)
    check("comment 1003 is Decision", (c3, RDF.type, KG.Decision) in g, True)

    # kg:about -> pnode
    for node in [c1, c2, c3]:
        check(f"kg:about on {node}", (node, KG.about, pnode) in g, True)

    # prov:wasDerivedFrom -> pnode
    for node in [c1, c2, c3]:
        check(f"prov:wasDerivedFrom on {node}", (node, PROV.wasDerivedFrom, pnode) in g, True)

    # prov:wasGeneratedBy -> run (an ExtractionRun)
    for node in [c1, c2, c3]:
        check(f"prov:wasGeneratedBy on {node}", (node, PROV.wasGeneratedBy, run) in g, True)
    check("run is ExtractionRun", (run, RDF.type, KG.ExtractionRun) in g, True)

    # Short/unclassified comment 1004 produces no node
    check("short comment 1004 has no node",
          len(list(g.triples((c4, None, None)))), 0)

    # snapshot/ is clean (no new or modified files)
    result = subprocess.run(
        ["git", "status", "--porcelain", "snapshot/"],
        capture_output=True, text=True,
    )
    check("snapshot/ is clean", result.stdout.strip(), "")

    print("\nall pr_comments tests passed")


if __name__ == "__main__":
    main()
