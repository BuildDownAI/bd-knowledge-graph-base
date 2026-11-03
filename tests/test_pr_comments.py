"""PR comment and review content node builder tests (KGB-7, KGB-47).

Feeds fixture dicts into the node builders (no network) and asserts node types,
IRIs, provenance links, review properties, and stats.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_pr_comments.py
Dual-namespace gate (KGB-6):
  KG_NAMESPACE=https://kg.acme.test/ PYTHONPATH=. ./.venv/bin/python tests/test_pr_comments.py
"""
import subprocess

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from kg_ingest import iris
from kg_ingest.iris import KG, KGR
from kg_ingest.spine import _add_pr_comments, _add_pr_review_content, _pr_comment_run_node

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


def test_pr_comments():
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

    result = subprocess.run(
        ["git", "status", "--porcelain", "snapshot/"],
        capture_output=True, text=True,
    )
    assert result.stdout.strip() == "", (
        f"snapshot/ has unexpected changes after test:\n{result.stdout}"
    )
    print("PASS: snapshot/ is clean after test")

    print("\nall pr_comments tests passed")


REVIEW_REPO = "owner/test-repo"
REVIEW_PR = 112


def _make_review_graph():
    g = Graph()
    g.bind("kg", KG)
    g.bind("kgr", KGR)
    run_id = iris.stable_run_id("0.1.0", REVIEW_REPO + ":pr-comments")
    run = _pr_comment_run_node(g, run_id, "0.1.0")
    pnode = iris.pr(REVIEW_REPO, REVIEW_PR)
    return g, run, pnode


def test_codex_inline_and_receipt():
    """CON #112: Codex P2 inline + receipt body both produce Verification nodes."""
    g, run, pnode = _make_review_graph()

    inline = {
        "id": 100001,
        "body": (
            "<!-- codex-code-review-finding:v1 -->\n"
            "**[P2]** Unnecessary re-renders on every keystroke in this component.\n"
            "Consider memoizing the event handler with useCallback."
        ),
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "path": "src/pages/orchestrator-detail.tsx",
        "line": 198,
        "original_line": 198,
        "commit_id": "abc123def456",
        "original_commit_id": "abc123def456",
        "created_at": "2026-10-01T10:00:00Z",
    }
    receipt = {
        "id": 200001,
        "body": (
            "<!-- codex-review-receipt:v1 -->\n"
            "Codex code review completed. 1 finding (P2) in this PR."
        ),
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "state": "COMMENTED",
        "submitted_at": "2026-10-01T10:01:00Z",
        "commit_id": "abc123def456",
    }
    thread_states = {100001: "resolved"}

    stats = {}
    count = _add_pr_review_content(
        g, run, REVIEW_REPO, REVIEW_PR, pnode,
        [inline], [receipt], thread_states, stats,
    )

    check("count == 2", count, 2)
    check("pr_review_items == 2", stats.get("pr_review_items"), 2)

    inode = iris.pr_review_comment(REVIEW_REPO, REVIEW_PR, 100001)
    check("inline IRI suffix", str(inode).endswith(
        f"pr/owner%2Ftest-repo/{REVIEW_PR}/review-comment/100001"), True)
    check("inline type Verification", (inode, RDF.type, KG.Verification) in g, True)
    check("inline reviewerKind codex", (inode, KG.reviewerKind, Literal("codex")) in g, True)
    check("inline severity P2", (inode, KG.severity, Literal("P2")) in g, True)
    check("inline reviewFilePath",
          (inode, KG.reviewFilePath, Literal("src/pages/orchestrator-detail.tsx")) in g, True)
    check("inline reviewLine 198",
          (inode, KG.reviewLine, Literal(198, datatype=XSD.integer)) in g, True)
    check("inline commitId", (inode, KG.commitId, Literal("abc123def456")) in g, True)
    check("inline threadState resolved", (inode, KG.threadState, Literal("resolved")) in g, True)
    check("inline about pnode", (inode, KG.about, pnode) in g, True)
    check("inline wasDerivedFrom pnode", (inode, PROV.wasDerivedFrom, pnode) in g, True)

    rnode = iris.pr_review_body(REVIEW_REPO, REVIEW_PR, 200001)
    check("receipt IRI suffix", str(rnode).endswith(
        f"pr/owner%2Ftest-repo/{REVIEW_PR}/review-body/200001"), True)
    check("receipt type Verification", (rnode, RDF.type, KG.Verification) in g, True)
    check("receipt reviewerKind codex", (rnode, KG.reviewerKind, Literal("codex")) in g, True)

    print("PASS: test_codex_inline_and_receipt")


def test_claude_gha_inline_and_body():
    """Claude GHA inline comment and review body are kept and classified."""
    g, run, pnode = _make_review_graph()

    inline = {
        "id": 100002,
        "body": (
            "<!-- claude-review-verdict blocking -->\n"
            "## \U0001f534 Blocking: Missing error boundary\n"
            "This component does not handle fetch errors; users see a blank screen."
        ),
        "user": {"login": "github-actions[bot]"},
        "path": "src/components/DataLoader.tsx",
        "line": 42,
        "original_line": 42,
        "commit_id": "def789",
        "original_commit_id": "def789",
        "created_at": "2026-10-02T09:00:00Z",
    }
    body_review = {
        "id": 200002,
        "body": (
            "<!-- claude-review-verdict -->\n"
            "## \U0001f534 Blocking findings\n"
            "One blocking issue found in the data loading component."
        ),
        "user": {"login": "github-actions[bot]"},
        "state": "CHANGES_REQUESTED",
        "submitted_at": "2026-10-02T09:01:00Z",
        "commit_id": "def789",
    }

    stats = {}
    count = _add_pr_review_content(
        g, run, REVIEW_REPO, REVIEW_PR, pnode,
        [inline], [body_review], {}, stats,
    )

    check("claude count == 2", count, 2)
    inode = iris.pr_review_comment(REVIEW_REPO, REVIEW_PR, 100002)
    check("claude inline kind", (inode, KG.reviewerKind, Literal("claude")) in g, True)
    check("claude inline severity blocking", (inode, KG.severity, Literal("blocking")) in g, True)
    check("claude inline threadState unknown", (inode, KG.threadState, Literal("unknown")) in g, True)

    rnode = iris.pr_review_body(REVIEW_REPO, REVIEW_PR, 200002)
    check("claude body kind", (rnode, KG.reviewerKind, Literal("claude")) in g, True)
    check("claude body reviewState", (rnode, KG.reviewState, Literal("CHANGES_REQUESTED")) in g, True)

    print("PASS: test_claude_gha_inline_and_body")


def test_aii_inline_finding():
    """AI-Implement post-push inline finding is kept with kind ai-implement."""
    g, run, pnode = _make_review_graph()

    inline = {
        "id": 100003,
        "body": (
            "<!-- ai-implement review-finding:v1 -->\n"
            "**[blocking]** This change breaks the existing snapshot contract.\n"
            "The snapshot delta check will fail on the next run."
        ),
        "user": {"login": "ai-implement[bot]"},
        "path": "kg_ingest/spine.py",
        "line": 55,
        "original_line": 55,
        "commit_id": "fff000",
        "original_commit_id": "fff000",
        "created_at": "2026-10-03T08:00:00Z",
    }

    stats = {}
    count = _add_pr_review_content(
        g, run, REVIEW_REPO, REVIEW_PR, pnode,
        [inline], [], {}, stats,
    )

    check("aii count == 1", count, 1)
    inode = iris.pr_review_comment(REVIEW_REPO, REVIEW_PR, 100003)
    check("aii kind", (inode, KG.reviewerKind, Literal("ai-implement")) in g, True)
    check("aii severity", (inode, KG.severity, Literal("blocking")) in g, True)
    check("aii path", (inode, KG.reviewFilePath, Literal("kg_ingest/spine.py")) in g, True)

    print("PASS: test_aii_inline_finding")


def test_human_comment_kept_lgtm_dropped():
    """Person's 60-char comment is kept; 'LGTM' review body is dropped."""
    g, run, pnode = _make_review_graph()

    human_inline = {
        "id": 100004,
        "body": "x " * 30,  # 60 chars, human -> should be kept
        "user": {"login": "alice"},
        "path": "README.md",
        "line": 1,
        "original_line": 1,
        "commit_id": "abc001",
        "original_commit_id": "abc001",
        "created_at": "2026-10-04T07:00:00Z",
    }
    lgtm_review = {
        "id": 200004,
        "body": "LGTM",
        "user": {"login": "bob"},
        "state": "APPROVED",
        "submitted_at": "2026-10-04T07:01:00Z",
        "commit_id": "abc001",
    }

    stats = {}
    count = _add_pr_review_content(
        g, run, REVIEW_REPO, REVIEW_PR, pnode,
        [human_inline], [lgtm_review], {}, stats,
    )

    check("human count == 1 (lgtm dropped)", count, 1)
    inode = iris.pr_review_comment(REVIEW_REPO, REVIEW_PR, 100004)
    check("human kind", (inode, KG.reviewerKind, Literal("human")) in g, True)
    check("human severity unknown", (inode, KG.severity, Literal("unknown")) in g, True)

    lgtm_node = iris.pr_review_body(REVIEW_REPO, REVIEW_PR, 200004)
    check("lgtm node absent", len(list(g.triples((lgtm_node, None, None)))), 0)

    print("PASS: test_human_comment_kept_lgtm_dropped")


def test_thread_states():
    """Resolved/outdated thread states are recorded; missing state is 'unknown'."""
    g, run, pnode = _make_review_graph()

    resolved_comment = {
        "id": 100010,
        "body": "This issue needs to be addressed before merging the feature branch.",
        "user": {"login": "alice"},
        "path": "src/app.py",
        "line": 10,
        "original_line": 10,
        "commit_id": "ccc001",
        "original_commit_id": "ccc001",
        "created_at": "2026-10-05T08:00:00Z",
    }
    outdated_comment = {
        "id": 100011,
        "body": "The type annotation here does not match the return value of the function.",
        "user": {"login": "alice"},
        "path": "src/app.py",
        "line": 20,
        "original_line": 20,
        "commit_id": "ccc001",
        "original_commit_id": "ccc001",
        "created_at": "2026-10-05T08:01:00Z",
    }
    unknown_comment = {
        "id": 100012,
        "body": "Consider extracting this repeated pattern into a shared helper function.",
        "user": {"login": "alice"},
        "path": "src/app.py",
        "line": 30,
        "original_line": 30,
        "commit_id": "ccc001",
        "original_commit_id": "ccc001",
        "created_at": "2026-10-05T08:02:00Z",
    }
    # 100012 not in thread_states → unknown
    thread_states = {100010: "resolved", 100011: "outdated"}

    stats = {}
    count = _add_pr_review_content(
        g, run, REVIEW_REPO, REVIEW_PR, pnode,
        [resolved_comment, outdated_comment, unknown_comment], [], thread_states, stats,
    )

    check("thread count == 3", count, 3)
    n10 = iris.pr_review_comment(REVIEW_REPO, REVIEW_PR, 100010)
    n11 = iris.pr_review_comment(REVIEW_REPO, REVIEW_PR, 100011)
    n12 = iris.pr_review_comment(REVIEW_REPO, REVIEW_PR, 100012)
    check("resolved", (n10, KG.threadState, Literal("resolved")) in g, True)
    check("outdated", (n11, KG.threadState, Literal("outdated")) in g, True)
    check("unknown", (n12, KG.threadState, Literal("unknown")) in g, True)

    print("PASS: test_thread_states")


def test_changes_requested_short_body_kept():
    """CHANGES_REQUESTED review with short body text is kept regardless of length."""
    g, run, pnode = _make_review_graph()

    short_review = {
        "id": 200010,
        "body": "Needs work.",  # 11 chars, below 20-char floor
        "user": {"login": "reviewer"},
        "state": "CHANGES_REQUESTED",
        "submitted_at": "2026-10-06T09:00:00Z",
        "commit_id": "ddd001",
    }

    stats = {}
    count = _add_pr_review_content(
        g, run, REVIEW_REPO, REVIEW_PR, pnode,
        [], [short_review], {}, stats,
    )

    check("changes_requested short body kept", count, 1)
    rnode = iris.pr_review_body(REVIEW_REPO, REVIEW_PR, 200010)
    check("human severity changes_requested",
          (rnode, KG.severity, Literal("changes_requested")) in g, True)

    print("PASS: test_changes_requested_short_body_kept")


def test_changes_requested_lgtm_body_dropped():
    """CHANGES_REQUESTED review whose body is approval-only ('LGTM') is dropped.

    The approval-drop rule must take precedence over the CHANGES_REQUESTED keep
    rule in should_keep_review_item — an approval is meaningless as a finding
    regardless of the formal review state.
    """
    g, run, pnode = _make_review_graph()

    lgtm_changes_requested = {
        "id": 200011,
        "body": "LGTM",
        "user": {"login": "carol"},
        "state": "CHANGES_REQUESTED",
        "submitted_at": "2026-10-07T10:00:00Z",
        "commit_id": "eee001",
    }

    stats = {}
    count = _add_pr_review_content(
        g, run, REVIEW_REPO, REVIEW_PR, pnode,
        [], [lgtm_changes_requested], {}, stats,
    )

    check("changes_requested lgtm dropped count == 0", count, 0)
    rnode = iris.pr_review_body(REVIEW_REPO, REVIEW_PR, 200011)
    check("lgtm changes_requested node absent",
          len(list(g.triples((rnode, None, None)))), 0)

    print("PASS: test_changes_requested_lgtm_body_dropped")


def test_conversation_comments_unchanged():
    """The existing _add_pr_comments path is unchanged — 3 nodes, same types."""
    g = Graph()
    g.bind("kg", KG)
    g.bind("kgr", KGR)
    run_id = iris.stable_run_id("0.1.0", REPO_SLUG + ":pr-comments")
    run = _pr_comment_run_node(g, run_id, "0.1.0")
    pnode = iris.pr(REPO_SLUG, PR_NUMBER)

    stats = {"pr_comments": 0}
    count = _add_pr_comments(g, run, REPO_SLUG, PR_NUMBER, pnode, FIXTURE_COMMENTS, stats)

    check("conversation count unchanged == 3", count, 3)
    check("stats pr_comments unchanged == 3", stats["pr_comments"], 3)
    c1 = iris.pr_comment(REPO_SLUG, PR_NUMBER, 1001)
    c2 = iris.pr_comment(REPO_SLUG, PR_NUMBER, 1002)
    c3 = iris.pr_comment(REPO_SLUG, PR_NUMBER, 1003)
    check("c1 Learning", (c1, RDF.type, KG.Learning) in g, True)
    check("c2 Verification", (c2, RDF.type, KG.Verification) in g, True)
    check("c3 Decision", (c3, RDF.type, KG.Decision) in g, True)

    print("PASS: test_conversation_comments_unchanged")


if __name__ == "__main__":
    test_pr_comments()
    test_codex_inline_and_receipt()
    test_claude_gha_inline_and_body()
    test_aii_inline_finding()
    test_human_comment_kept_lgtm_dropped()
    test_thread_states()
    test_changes_requested_short_body_kept()
    test_changes_requested_lgtm_body_dropped()
    test_conversation_comments_unchanged()

    result = subprocess.run(
        ["git", "status", "--porcelain", "snapshot/"],
        capture_output=True, text=True,
    )
    assert result.stdout.strip() == "", (
        f"snapshot/ has unexpected changes after tests:\n{result.stdout}"
    )
    print("\nall pr_comments + review tests passed")
