"""PR comment fetch: retry on rate-limit, stats always printed, non-zero exit on threshold.

KGB-22: a rate-limited comment fetch is retried up to 3 times (1 initial + 3
retries = 4 total calls); stats always include `pr_comments`; when error rate
exceeds 10 % or any rate-limit error occurs, add_spine raises PRCommentsIncomplete
and cli.main() exits 1 printing KG_PR_COMMENTS_INCOMPLETE.

Run: PYTHONPATH=. ./.venv/bin/python tests/test_spine_pr_comments.py
Dual-namespace gate (KGB-6):
  KG_NAMESPACE=https://kg.acme.test/ PYTHONPATH=. ./.venv/bin/python tests/test_spine_pr_comments.py
"""
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Patch retry delays to zero before any spine call so tests run instantly.
import kg_ingest.snapshot as _snapshot_mod
import kg_ingest.spine as _spine_mod
_spine_mod._RETRY_DELAYS = (0, 0, 0)

from kg_ingest import iris  # noqa: E402 -- must follow the module-level retry-delay patch above
from kg_ingest.spine import PRCommentsIncomplete, add_spine  # noqa: E402 -- same


def _make_fixture_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"],
                   check=True, capture_output=True)
    (repo / "README.md").write_text("# Test\nContent.")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"],
                   check=True, capture_output=True)
    return repo


def _write_fake_gh(fake_dir: Path, call_log: Path, rate_limit_pr: int = 3) -> None:
    """Write a fake `gh` executable into fake_dir."""
    script = fake_dir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, os, re\n"
        "\n"
        f"call_log = {str(call_log)!r}\n"
        "with open(call_log, 'a') as _f:\n"
        "    _f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "\n"
        "args = sys.argv[1:]\n"
        "# Default branch query: gh api repos/<slug> --jq .default_branch\n"
        "if args[:1] == ['api'] and '--paginate' not in args and '--jq' in args:\n"
        "    jq_idx = args.index('--jq')\n"
        "    if jq_idx + 1 < len(args) and args[jq_idx + 1] == '.default_branch':\n"
        "        print('main')\n"
        "        sys.exit(0)\n"
        "\n"
        "# GraphQL: return empty thread list\n"
        "if args[:2] == ['api', 'graphql']:\n"
        "    print(json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': []}}}}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
        "    # Inline review comments and review bodies — check before generic /pulls\n"
        "    if re.search(r'pulls/\\d+/comments', path):\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "    if re.search(r'pulls/\\d+/reviews', path):\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "    # PR list fetch\n"
        "    if re.search(r'/pulls', path):\n"
        "        print(json.dumps([\n"
        "            {'number': 1, 'title': 'PR 1', 'user': {'login': 'alice'},"
        " 'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',"
        " 'merge_commit_sha': 'aaa111', 'body': '', 'base': {'ref': 'main'}},\n"
        "            {'number': 2, 'title': 'PR 2', 'user': {'login': 'bob'},"
        " 'state': 'closed', 'merged_at': '2026-01-02T00:00:00Z',"
        " 'merge_commit_sha': 'bbb222', 'body': '', 'base': {'ref': 'main'}},\n"
        "            {'number': 3, 'title': 'PR 3', 'user': {'login': 'carol'},"
        " 'state': 'open', 'merged_at': None,"
        " 'merge_commit_sha': None, 'body': '', 'base': {'ref': 'main'}},\n"
        "        ]))\n"
        "        sys.exit(0)\n"
        "    # Conversation comments fetch\n"
        "    m = re.search(r'issues/(\\d+)/comments', path)\n"
        "    if m:\n"
        f"        if int(m.group(1)) == {rate_limit_pr}:\n"
        "            print('API rate limit exceeded for this resource.', file=sys.stderr)\n"
        "            sys.exit(1)\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "\n"
        "print('fake gh: unhandled: ' + ' '.join(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)


def _write_fake_gh_clean(fake_dir: Path, call_log: Path) -> None:
    """Write a fake `gh` that succeeds for all PRs (no rate limiting)."""
    script = fake_dir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, re\n"
        "\n"
        f"call_log = {str(call_log)!r}\n"
        "with open(call_log, 'a') as _f:\n"
        "    _f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "\n"
        "args = sys.argv[1:]\n"
        "# Default branch query\n"
        "if args[:1] == ['api'] and '--paginate' not in args and '--jq' in args:\n"
        "    jq_idx = args.index('--jq')\n"
        "    if jq_idx + 1 < len(args) and args[jq_idx + 1] == '.default_branch':\n"
        "        print('main')\n"
        "        sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', 'graphql']:\n"
        "    print(json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': []}}}}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
        "    if re.search(r'pulls/\\d+/comments', path):\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "    if re.search(r'pulls/\\d+/reviews', path):\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "    if re.search(r'/pulls', path):\n"
        "        print(json.dumps([\n"
        "            {'number': 1, 'title': 'PR 1', 'user': {'login': 'alice'},"
        " 'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',"
        " 'merge_commit_sha': 'aaa111', 'body': '', 'base': {'ref': 'main'}},\n"
        "            {'number': 2, 'title': 'PR 2', 'user': {'login': 'bob'},"
        " 'state': 'closed', 'merged_at': '2026-01-02T00:00:00Z',"
        " 'merge_commit_sha': 'bbb222', 'body': '', 'base': {'ref': 'main'}},\n"
        "        ]))\n"
        "        sys.exit(0)\n"
        "    print(json.dumps([]))\n"
        "    sys.exit(0)\n"
        "\n"
        "print('fake gh: unhandled: ' + ' '.join(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)


def _write_fake_gh_multipage(fake_dir: Path, call_log: Path, total_prs: int = 210) -> None:
    """Write a fake `gh` that returns `total_prs` PRs split across two JSON arrays (pages)."""
    page1 = [
        {"number": i, "title": f"PR {i}", "user": {"login": "user"},
         "state": "closed", "merged_at": "2026-01-01T00:00:00Z",
         "merge_commit_sha": f"sha{i:04x}", "body": "", "base": {"ref": "main"}}
        for i in range(1, 201)
    ]
    page2 = [
        {"number": i, "title": f"PR {i}", "user": {"login": "user"},
         "state": "closed", "merged_at": "2026-01-02T00:00:00Z",
         "merge_commit_sha": f"sha{i:04x}", "body": "", "base": {"ref": "main"}}
        for i in range(201, total_prs + 1)
    ]
    script = fake_dir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, re\n"
        "\n"
        f"call_log = {str(call_log)!r}\n"
        "with open(call_log, 'a') as _f:\n"
        "    _f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['api'] and '--paginate' not in args and '--jq' in args:\n"
        "    jq_idx = args.index('--jq')\n"
        "    if jq_idx + 1 < len(args) and args[jq_idx + 1] == '.default_branch':\n"
        "        print('main')\n"
        "        sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', 'graphql']:\n"
        "    print(json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': []}}}}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
        "    if re.search(r'pulls/\\d+/comments', path):\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "    if re.search(r'pulls/\\d+/reviews', path):\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "    if re.search(r'/pulls', path):\n"
        f"        page1 = {json.dumps(page1)}\n"
        f"        page2 = {json.dumps(page2)}\n"
        "        print(json.dumps(page1))\n"
        "        print(json.dumps(page2))\n"
        "        sys.exit(0)\n"
        "    print(json.dumps([]))\n"
        "    sys.exit(0)\n"
        "\n"
        "print('fake gh: unhandled: ' + ' '.join(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)


def _with_fake_gh(fake_dir: Path):
    """Context manager that prepends fake_dir to PATH."""
    class _Ctx:
        def __enter__(self):
            self._old = os.environ.get("PATH", "")
            os.environ["PATH"] = str(fake_dir) + ":" + self._old
            return self
        def __exit__(self, *_):
            os.environ["PATH"] = self._old
    return _Ctx()


@pytest.fixture
def repo(tmp_path):
    return _make_fixture_repo(tmp_path)


def check(name: str, got, want) -> None:
    assert got == want, f"FAIL {name}: got {got!r}, want {want!r}"
    print(f"PASS: {name}")


def test_retry_and_incomplete(repo: Path) -> None:
    """PR 3 always rate-limits: 4 attempts are made; PRCommentsIncomplete is raised."""
    from rdflib import Graph

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")
        _write_fake_gh(fake_dir, call_log, rate_limit_pr=3)

        with _with_fake_gh(fake_dir):
            g = Graph()
            exc_caught = None
            try:
                add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=3)
            except PRCommentsIncomplete as e:
                exc_caught = e

        assert exc_caught is not None, "Expected PRCommentsIncomplete to be raised"
        print("PASS: PRCommentsIncomplete raised when rate-limit errors exceed threshold")

        # Count calls for PR 3's comment endpoint (read log before tempdir cleanup)
        calls = call_log.read_text().splitlines()
        pr3_comment_calls = [c for c in calls if "issues/3/comments" in c]
        assert len(pr3_comment_calls) >= 4, (
            f"Expected >= 4 calls for PR 3 comments (1 initial + 3 retries), "
            f"got {len(pr3_comment_calls)}: {pr3_comment_calls}"
        )
        print(f"PASS: PR 3 comment endpoint called {len(pr3_comment_calls)} times (>= 4)")

    # Stats must include pr_comments and pr_comment_errors
    stats = exc_caught.stats
    assert "pr_comments" in stats, f"pr_comments missing from stats: {stats}"
    assert "pr_comment_errors" in stats, f"pr_comment_errors missing from stats: {stats}"
    assert "rate limit" in stats["pr_comment_errors"].lower() or "1" in stats["pr_comment_errors"], (
        f"pr_comment_errors should mention error count or cause: {stats['pr_comment_errors']}"
    )
    print(f"PASS: stats contain pr_comments={stats['pr_comments']!r} and "
          f"pr_comment_errors={stats['pr_comment_errors']!r}")

    # Verify snapshot was not modified by the failed ingest
    result = subprocess.run(
        ["git", "status", "--porcelain", "snapshot/"],
        capture_output=True, text=True,
    )
    assert result.stdout.strip() == "", (
        f"snapshot/ has unexpected changes after failed ingest:\n{result.stdout}"
    )
    print("PASS: snapshot/ unchanged after PRCommentsIncomplete")


def test_clean_run_stats(repo: Path) -> None:
    """All PRs succeed: pr_comments is in stats, no exception raised."""
    from rdflib import Graph

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")
        _write_fake_gh_clean(fake_dir, call_log)

        with _with_fake_gh(fake_dir):
            g = Graph()
            stats = add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=2)

    check("pr_comments in stats", "pr_comments" in stats, True)
    check("no pr_comment_errors on clean run", "pr_comment_errors" in stats, False)
    check("prs == 2", stats["prs"], 2)
    print(f"PASS: clean run stats: pr_comments={stats['pr_comments']}, prs={stats['prs']}")


def test_help_lists_no_docs_sites() -> None:
    """--help output includes --no-docs-sites."""
    import io
    from kg_ingest import cli as cli_mod

    buf = io.StringIO()
    try:
        with patch("sys.stdout", buf):
            cli_mod.main(argv=["--help"])
    except SystemExit as exc:
        assert exc.code == 0, f"--help exited with non-zero code: {exc.code}"

    out = buf.getvalue()
    assert "--no-docs-sites" in out, (
        f"Expected '--no-docs-sites' in --help output, got:\n{out}"
    )
    print("PASS: --help lists --no-docs-sites")


def test_no_docs_sites_skips_crawl(repo: Path) -> None:
    """--no-docs-sites prints skip line and does not call _ingest_docs_sites."""
    import io
    from kg_ingest import cli as cli_mod
    import kg_ingest.cli as _cli_mod
    import kg_ingest.sources as _sources_mod
    import kg_ingest.semantic as _semantic_mod
    import kg_ingest.snapshot as _snapshot_mod
    import kg_ingest.ontology as _ontology_mod
    from rdflib import Graph

    sources_cfg = {
        "code_repo": {"slug": "test-org/repo"},
        "docs_sites": [{"url": "https://docs.example.com"}],
    }

    crawl_called = []

    def fake_ingest_docs_sites(spine_g, cfg):
        crawl_called.append(cfg)
        return {}

    buf = io.StringIO()
    with patch.object(_sources_mod, "load", return_value=sources_cfg), \
         patch.object(_spine_mod, "add_spine",
                      return_value={"files": 1, "commits": 1, "people": 1,
                                    "prs": 0, "pr_comments": 0}), \
         patch.object(_cli_mod, "_ingest_docs_sites", side_effect=fake_ingest_docs_sites), \
         patch.object(_semantic_mod, "add_semantic", return_value={}), \
         patch.object(_ontology_mod, "load", return_value=Graph()), \
         patch("pyshacl.validate", return_value=(True, None, "")), \
         patch.object(_snapshot_mod, "write_snapshot",
                      return_value={"part_files": 0}), \
         patch("sys.stdout", buf):
        cli_mod.main(argv=["--repo", str(repo), "--no-docs-sites", "--max-prs", "0",
                           "--no-embed"])

    assert len(crawl_called) == 0, (
        f"_ingest_docs_sites should not have been called, got: {crawl_called}"
    )
    out = buf.getvalue()
    assert "docs_sites: skipped (--no-docs-sites)" in out, (
        f"Expected skip line in output, got:\n{out}"
    )
    print("PASS: --no-docs-sites skips crawl and prints skip line")


def test_cli_exits_nonzero(repo: Path) -> None:
    """cli.main() returns 1 and prints KG_PR_COMMENTS_INCOMPLETE when PRCommentsIncomplete raised."""
    from kg_ingest import cli as cli_mod

    fake_stats = {
        "files": 1, "commits": 1, "people": 1, "prs": 3, "pr_comments": 0,
        "pr_comment_errors": "1 (first: API rate limit exceeded for this resource.)",
        "commit_cap": None, "pr_cap": 3,
    }
    fake_exc = PRCommentsIncomplete(fake_stats)

    with patch.object(_spine_mod, "add_spine", side_effect=fake_exc), \
         patch.object(_snapshot_mod, "write_snapshot", return_value={"part_files": 0}):
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            ret = cli_mod.main(argv=["--repo", str(repo), "--max-prs", "3"])

    check("cli.main() returns 1", ret, 1)
    out = buf.getvalue()
    assert "KG_PR_COMMENTS_INCOMPLETE" in out, (
        f"Expected 'KG_PR_COMMENTS_INCOMPLETE' in stdout, got:\n{out}"
    )
    assert "pr_comments" in out, f"Expected pr_comments in stdout, got:\n{out}"
    print("PASS: cli.main() exits 1 and prints KG_PR_COMMENTS_INCOMPLETE")


def test_pagination(repo: Path) -> None:
    """A fake gh returning 210 PRs across 2 pages gives prs > 200 and pr_pages_fetched >= 2."""
    from rdflib import Graph

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")
        _write_fake_gh_multipage(fake_dir, call_log, total_prs=210)

        with _with_fake_gh(fake_dir):
            g = Graph()
            stats = add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=999)

    assert stats["prs"] > 200, f"Expected prs > 200, got {stats['prs']}"
    assert stats.get("pr_pages_fetched", 0) >= 2, (
        f"Expected pr_pages_fetched >= 2, got {stats.get('pr_pages_fetched')}"
    )
    print(f"PASS: pagination: prs={stats['prs']}, pr_pages_fetched={stats['pr_pages_fetched']}")


def test_dedup_slug(repo: Path) -> None:
    """sources.yml that lists code_repo slug again under secondary_repos gives one ingest."""
    import io
    from kg_ingest import cli as cli_mod
    import kg_ingest.sources as _sources_mod
    import kg_ingest.semantic as _semantic_mod
    import kg_ingest.snapshot as _snapshot_mod
    import kg_ingest.ontology as _ontology_mod
    from rdflib import Graph

    code_slug = "test-org/repo"
    sources_cfg = {
        "code_repo": {"slug": code_slug},
        "secondary_repos": [
            {"slug": code_slug, "path": str(repo)},
        ],
    }

    spine_call_slugs: list[str] = []

    def fake_add_spine(g, path, slug, **kw):
        spine_call_slugs.append(slug)
        return {"files": 1, "commits": 1, "people": 1,
                "prs": 0, "pr_comments": 0, "history_branch": "main",
                "is_default_branch": False}

    buf = io.StringIO()
    _ai_out = cli_mod.OUT_DIR.parent / "ai-output"
    _ai_out.mkdir(exist_ok=True)

    with patch.object(_sources_mod, "load", return_value=sources_cfg), \
         patch.object(_spine_mod, "add_spine", side_effect=fake_add_spine), \
         patch.object(_semantic_mod, "add_semantic", return_value={}), \
         patch.object(_ontology_mod, "load", return_value=Graph()), \
         patch("pyshacl.validate", return_value=(True, None, "")), \
         patch.object(_snapshot_mod, "write_snapshot", return_value={"part_files": 0}), \
         patch("sys.stdout", buf):
        cli_mod.main(argv=["--repo", str(repo), "--secondary", "--max-prs", "0",
                           "--no-embed"])

    # spine should only be called once for code_repo; secondary duplicate is skipped
    assert spine_call_slugs.count(code_slug) == 1, (
        f"Expected add_spine called once for {code_slug!r}, got calls: {spine_call_slugs}"
    )

    # source-results.json must have a skipped entry for the duplicate
    results_path = cli_mod.OUT_DIR.parent / "ai-output" / "source-results.json"
    results = json.loads(results_path.read_text())
    skipped = [r for r in results if r.get("status") == "skipped"
               and r.get("source") == f"secondary_repo:{code_slug}"]
    assert skipped, f"Expected a skipped entry for duplicate slug, got: {results}"
    assert skipped[0]["reason"].startswith("duplicate of "), (
        f"Expected reason to start with 'duplicate of ', got: {skipped[0]['reason']!r}"
    )
    print(f"PASS: dedup_slug: spine called {len(spine_call_slugs)} time(s), "
          f"skipped entry reason={skipped[0]['reason']!r}")


def test_review_fetch_error_counts_toward_incomplete(repo: Path) -> None:
    """A fetch error on pulls/{n}/comments counts toward KG_PR_COMMENTS_INCOMPLETE."""
    from rdflib import Graph

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")

        script = fake_dir / "gh"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, json, re\n"
            "\n"
            f"call_log = {str(call_log)!r}\n"
            "with open(call_log, 'a') as _f:\n"
            "    _f.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "\n"
            "args = sys.argv[1:]\n"
            "if args[:1] == ['api'] and '--paginate' not in args and '--jq' in args:\n"
            "    if '.default_branch' in args:\n"
            "        print('main')\n"
            "        sys.exit(0)\n"
            "\n"
            "if args[:2] == ['api', 'graphql']:\n"
            "    print(json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': []}}}}}))\n"
            "    sys.exit(0)\n"
            "\n"
            "if args[:2] == ['api', '--paginate']:\n"
            "    path = args[2] if len(args) > 2 else ''\n"
            "    # Inline review comments: always fail with rate-limit error\n"
            "    if re.search(r'pulls/\\d+/comments', path):\n"
            "        print('API rate limit exceeded.', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    if re.search(r'pulls/\\d+/reviews', path):\n"
            "        print(json.dumps([]))\n"
            "        sys.exit(0)\n"
            "    if re.search(r'/pulls', path):\n"
            "        print(json.dumps([\n"
            "            {'number': 1, 'title': 'PR 1', 'user': {'login': 'alice'},\n"
            "             'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',\n"
            "             'merge_commit_sha': 'aaa111', 'body': '', 'base': {'ref': 'main'}},\n"
            "        ]))\n"
            "        sys.exit(0)\n"
            "    m = re.search(r'issues/(\\d+)/comments', path)\n"
            "    if m:\n"
            "        print(json.dumps([]))\n"
            "        sys.exit(0)\n"
            "\n"
            "print('fake gh: unhandled: ' + ' '.join(args), file=sys.stderr)\n"
            "sys.exit(1)\n"
        )
        script.chmod(0o755)

        with _with_fake_gh(fake_dir):
            g = Graph()
            exc_caught = None
            try:
                add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=1)
            except PRCommentsIncomplete as e:
                exc_caught = e

    assert exc_caught is not None, (
        "Expected PRCommentsIncomplete when inline review comments fetch rate-limits"
    )
    stats = exc_caught.stats
    assert "pr_comment_errors" in stats, (
        f"Expected pr_comment_errors in stats: {stats}"
    )
    print(f"PASS: review fetch error counted toward incomplete: "
          f"pr_comment_errors={stats['pr_comment_errors']!r}")

    result = subprocess.run(
        ["git", "status", "--porcelain", "snapshot/"],
        capture_output=True, text=True,
    )
    assert result.stdout.strip() == "", (
        f"snapshot/ has unexpected changes after failed ingest:\n{result.stdout}"
    )
    print("PASS: snapshot/ unchanged after PRCommentsIncomplete")


def _write_fake_gh_rate_limit_once(
    fake_dir: Path,
    call_log: Path,
    fail_endpoint_re: str,
    rl_core_remaining: int,
    rl_core_reset: int,
    rl_graphql_remaining: int,
    rl_graphql_reset: int,
    retry_after: int | None = None,
) -> None:
    """Fake gh that rate-limits the matching endpoint on the first call, then succeeds.

    Handles `gh api rate_limit` with configurable core/graphql remaining and reset values.
    If retry_after is set, includes it in the rate-limit error stderr instead of consulting
    rate_limit.
    """
    err_line = "gh: API rate limit exceeded for installation ID 123."
    if retry_after is not None:
        err_line = f"gh: API rate limit exceeded. retry-after: {retry_after}"

    rl_response = json.dumps({
        "resources": {
            "core": {"remaining": rl_core_remaining, "reset": rl_core_reset},
            "graphql": {"remaining": rl_graphql_remaining, "reset": rl_graphql_reset},
        }
    })

    script = fake_dir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, re\n"
        "\n"
        f"call_log = {str(call_log)!r}\n"
        f"fail_ep_re = {fail_endpoint_re!r}\n"
        f"err_line = {err_line!r}\n"
        f"rl_response = {rl_response!r}\n"
        "\n"
        "args = sys.argv[1:]\n"
        "\n"
        "# Read prior calls BEFORE writing this one (for counting)\n"
        "try:\n"
        "    with open(call_log) as _f:\n"
        "        prev_lines = _f.read().splitlines()\n"
        "except Exception:\n"
        "    prev_lines = []\n"
        "\n"
        "with open(call_log, 'a') as _f:\n"
        "    _f.write(' '.join(args) + '\\n')\n"
        "\n"
        "# Rate limit check endpoint\n"
        "if args == ['api', 'rate_limit']:\n"
        "    print(rl_response)\n"
        "    sys.exit(0)\n"
        "\n"
        "# Default branch query\n"
        "if args[:1] == ['api'] and '--paginate' not in args and '--jq' in args:\n"
        "    jq_idx = args.index('--jq')\n"
        "    if jq_idx + 1 < len(args) and args[jq_idx + 1] == '.default_branch':\n"
        "        print('main')\n"
        "        sys.exit(0)\n"
        "\n"
        "# Check if this call matches the failing endpoint\n"
        "call_str = ' '.join(args)\n"
        "is_failing_ep = bool(re.search(fail_ep_re, call_str))\n"
        "prev_fail_count = sum(1 for l in prev_lines if re.search(fail_ep_re, l))\n"
        "\n"
        "if is_failing_ep and prev_fail_count == 0:\n"
        "    print(err_line, file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "\n"
        "# GraphQL thread states\n"
        "if args[:2] == ['api', 'graphql']:\n"
        "    print(json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': []}}}}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
        "    if re.search(r'pulls/\\d+/comments', path):\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "    if re.search(r'pulls/\\d+/reviews', path):\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "    if re.search(r'/pulls', path):\n"
        "        print(json.dumps([\n"
        "            {'number': 1, 'title': 'PR 1', 'user': {'login': 'alice'},\n"
        "             'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',\n"
        "             'merge_commit_sha': 'aaa111', 'body': '', 'base': {'ref': 'main'}},\n"
        "            {'number': 2, 'title': 'PR 2', 'user': {'login': 'bob'},\n"
        "             'state': 'closed', 'merged_at': '2026-01-02T00:00:00Z',\n"
        "             'merge_commit_sha': 'bbb222', 'body': '', 'base': {'ref': 'main'}},\n"
        "            {'number': 3, 'title': 'PR 3', 'user': {'login': 'carol'},\n"
        "             'state': 'open', 'merged_at': None,\n"
        "             'merge_commit_sha': None, 'body': '', 'base': {'ref': 'main'}},\n"
        "        ]))\n"
        "        sys.exit(0)\n"
        "    m = re.search(r'issues/(\\d+)/comments', path)\n"
        "    if m:\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "\n"
        "print('fake gh: unhandled: ' + ' '.join(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)


def _write_fake_gh_rate_limit_always(
    fake_dir: Path,
    call_log: Path,
    fail_endpoint_re: str,
    rl_core_remaining: int,
    rl_core_reset: int,
    rl_graphql_remaining: int,
    rl_graphql_reset: int,
) -> None:
    """Fake gh that always rate-limits the matching endpoint (never recovers)."""
    rl_response = json.dumps({
        "resources": {
            "core": {"remaining": rl_core_remaining, "reset": rl_core_reset},
            "graphql": {"remaining": rl_graphql_remaining, "reset": rl_graphql_reset},
        }
    })

    script = fake_dir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, re\n"
        "\n"
        f"call_log = {str(call_log)!r}\n"
        f"fail_ep_re = {fail_endpoint_re!r}\n"
        f"rl_response = {rl_response!r}\n"
        "\n"
        "args = sys.argv[1:]\n"
        "with open(call_log, 'a') as _f:\n"
        "    _f.write(' '.join(args) + '\\n')\n"
        "\n"
        "if args == ['api', 'rate_limit']:\n"
        "    print(rl_response)\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:1] == ['api'] and '--paginate' not in args and '--jq' in args:\n"
        "    jq_idx = args.index('--jq')\n"
        "    if jq_idx + 1 < len(args) and args[jq_idx + 1] == '.default_branch':\n"
        "        print('main')\n"
        "        sys.exit(0)\n"
        "\n"
        "call_str = ' '.join(args)\n"
        "if re.search(fail_ep_re, call_str):\n"
        "    print('gh: API rate limit exceeded for installation ID 123.', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "\n"
        "if args[:2] == ['api', 'graphql']:\n"
        "    print(json.dumps({'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': []}}}}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
        "    if re.search(r'pulls/\\d+/comments', path):\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "    if re.search(r'pulls/\\d+/reviews', path):\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "    if re.search(r'/pulls', path):\n"
        "        print(json.dumps([\n"
        "            {'number': 1, 'title': 'PR 1', 'user': {'login': 'alice'},\n"
        "             'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',\n"
        "             'merge_commit_sha': 'aaa111', 'body': '', 'base': {'ref': 'main'}},\n"
        "            {'number': 2, 'title': 'PR 2', 'user': {'login': 'bob'},\n"
        "             'state': 'closed', 'merged_at': '2026-01-02T00:00:00Z',\n"
        "             'merge_commit_sha': 'bbb222', 'body': '', 'base': {'ref': 'main'}},\n"
        "            {'number': 3, 'title': 'PR 3', 'user': {'login': 'carol'},\n"
        "             'state': 'open', 'merged_at': None,\n"
        "             'merge_commit_sha': None, 'body': '', 'base': {'ref': 'main'}},\n"
        "        ]))\n"
        "        sys.exit(0)\n"
        "    m = re.search(r'issues/(\\d+)/comments', path)\n"
        "    if m:\n"
        "        print(json.dumps([]))\n"
        "        sys.exit(0)\n"
        "\n"
        "print('fake gh: unhandled: ' + ' '.join(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)


def test_rest_comment_rate_limit_reset_wait(repo: Path) -> None:
    """Rate limit on issues/N/comments: waits for core reset+5s, retries, completes cleanly."""
    from rdflib import Graph

    mock_now = 1000.0
    reset_epoch = 1600  # 600 s in the future → candidate = 605

    sleep_calls: list[float] = []
    old_sleep = _spine_mod._sleep
    old_now = _spine_mod._now
    old_budget = _spine_mod._rate_limit_budget_s
    _spine_mod._sleep = lambda s: sleep_calls.append(s)
    _spine_mod._now = lambda: mock_now
    _spine_mod._rate_limit_budget_s = _spine_mod._RATE_LIMIT_WAIT_BUDGET_S

    try:
        with tempfile.TemporaryDirectory() as d:
            fake_dir = Path(d) / "bin"
            fake_dir.mkdir()
            call_log = Path(d) / "calls.log"
            call_log.write_text("")
            _write_fake_gh_rate_limit_once(
                fake_dir, call_log,
                fail_endpoint_re=r"issues/3/comments",
                rl_core_remaining=0, rl_core_reset=reset_epoch,
                rl_graphql_remaining=100, rl_graphql_reset=9999,
            )
            with _with_fake_gh(fake_dir):
                g = Graph()
                stats = add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=3)
    finally:
        _spine_mod._sleep = old_sleep
        _spine_mod._now = old_now
        _spine_mod._rate_limit_budget_s = old_budget

    assert stats.get("pr_comment_errors") is None, (
        f"Expected no pr_comment_errors, got: {stats.get('pr_comment_errors')}"
    )
    assert 605 in sleep_calls or 605.0 in sleep_calls, (
        f"Expected sleep(605) for core reset wait, got sleep_calls={sleep_calls}"
    )
    print(f"PASS: REST comment rate limit reset wait: sleep_calls={sleep_calls}, "
          f"pr_comment_errors={stats.get('pr_comment_errors')!r}")


def test_graphql_rate_limit_reset_uses_graphql_key(repo: Path) -> None:
    """GraphQL rate limit reads graphql.reset, not core.reset."""
    from rdflib import Graph

    mock_now = 1000.0
    core_reset = 1400    # would give 405 s if wrongly used
    graphql_reset = 1600  # correct key → 605 s

    sleep_calls: list[float] = []
    old_sleep = _spine_mod._sleep
    old_now = _spine_mod._now
    old_budget = _spine_mod._rate_limit_budget_s
    _spine_mod._sleep = lambda s: sleep_calls.append(s)
    _spine_mod._now = lambda: mock_now
    _spine_mod._rate_limit_budget_s = _spine_mod._RATE_LIMIT_WAIT_BUDGET_S

    try:
        with tempfile.TemporaryDirectory() as d:
            fake_dir = Path(d) / "bin"
            fake_dir.mkdir()
            call_log = Path(d) / "calls.log"
            call_log.write_text("")
            # Fail the first GraphQL thread-states call with a rate limit error.
            _write_fake_gh_rate_limit_once(
                fake_dir, call_log,
                fail_endpoint_re=r"api graphql",
                rl_core_remaining=100, rl_core_reset=core_reset,
                rl_graphql_remaining=0, rl_graphql_reset=graphql_reset,
            )
            with _with_fake_gh(fake_dir):
                g = Graph()
                stats = add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=3)
    finally:
        _spine_mod._sleep = old_sleep
        _spine_mod._now = old_now
        _spine_mod._rate_limit_budget_s = old_budget

    assert stats.get("pr_comment_errors") is None, (
        f"Expected no pr_comment_errors, got: {stats.get('pr_comment_errors')!r}"
    )
    assert 605 in sleep_calls or 605.0 in sleep_calls, (
        f"Expected sleep(605) from graphql.reset={graphql_reset}, "
        f"not 405 from core.reset={core_reset}. Got: {sleep_calls}"
    )
    assert 405 not in sleep_calls and 405.0 not in sleep_calls, (
        f"core.reset was incorrectly used: sleep_calls={sleep_calls}"
    )
    print(f"PASS: GraphQL rate limit reads graphql.reset: sleep_calls={sleep_calls}")


def test_pr_list_rate_limit_reset_wait(repo: Path) -> None:
    """Rate limit on the PR list fetch: waits for reset, retries, pr_error not set."""
    from rdflib import Graph

    mock_now = 1000.0
    reset_epoch = 1600

    sleep_calls: list[float] = []
    old_sleep = _spine_mod._sleep
    old_now = _spine_mod._now
    old_budget = _spine_mod._rate_limit_budget_s
    _spine_mod._sleep = lambda s: sleep_calls.append(s)
    _spine_mod._now = lambda: mock_now
    _spine_mod._rate_limit_budget_s = _spine_mod._RATE_LIMIT_WAIT_BUDGET_S

    try:
        with tempfile.TemporaryDirectory() as d:
            fake_dir = Path(d) / "bin"
            fake_dir.mkdir()
            call_log = Path(d) / "calls.log"
            call_log.write_text("")
            # Match only the PR list endpoint (not issues/N/comments which also contains "pulls")
            _write_fake_gh_rate_limit_once(
                fake_dir, call_log,
                fail_endpoint_re=r"--paginate repos/[^/]+/[^/]+/pulls\?",
                rl_core_remaining=0, rl_core_reset=reset_epoch,
                rl_graphql_remaining=100, rl_graphql_reset=9999,
            )
            with _with_fake_gh(fake_dir):
                g = Graph()
                stats = add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=3)
    finally:
        _spine_mod._sleep = old_sleep
        _spine_mod._now = old_now
        _spine_mod._rate_limit_budget_s = old_budget

    assert "pr_error" not in stats, (
        f"Expected no pr_error after PR list retry, got: {stats.get('pr_error')!r}"
    )
    assert 605 in sleep_calls or 605.0 in sleep_calls, (
        f"Expected sleep(605) for core reset wait, got: {sleep_calls}"
    )
    assert stats["prs"] == 3, f"Expected 3 PRs ingested, got: {stats['prs']}"
    print(f"PASS: PR list rate limit reset wait: sleep_calls={sleep_calls}, prs={stats['prs']}")


def test_rate_limit_budget_exhausted(repo: Path) -> None:
    """When reset wait > remaining budget, no reset wait occurs; PRCommentsIncomplete raised."""
    from rdflib import Graph

    mock_now = 1000.0
    reset_epoch = 1600  # candidate = 605 s, but budget = 100 s

    sleep_calls: list[float] = []
    old_sleep = _spine_mod._sleep
    old_now = _spine_mod._now
    old_budget = _spine_mod._rate_limit_budget_s
    _spine_mod._sleep = lambda s: sleep_calls.append(s)
    _spine_mod._now = lambda: mock_now
    _spine_mod._rate_limit_budget_s = 100.0  # too small for 605 s reset

    try:
        with tempfile.TemporaryDirectory() as d:
            fake_dir = Path(d) / "bin"
            fake_dir.mkdir()
            call_log = Path(d) / "calls.log"
            call_log.write_text("")
            # Always rate-limit issues/3/comments so we exhaust retries.
            _write_fake_gh_rate_limit_always(
                fake_dir, call_log,
                fail_endpoint_re=r"issues/3/comments",
                rl_core_remaining=0, rl_core_reset=reset_epoch,
                rl_graphql_remaining=100, rl_graphql_reset=9999,
            )
            import io
            buf = io.StringIO()
            with _with_fake_gh(fake_dir):
                g = Graph()
                exc_caught = None
                try:
                    from unittest.mock import patch
                    with patch("sys.stdout", buf):
                        add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=3)
                except PRCommentsIncomplete as e:
                    exc_caught = e
    finally:
        _spine_mod._sleep = old_sleep
        _spine_mod._now = old_now
        _spine_mod._rate_limit_budget_s = old_budget

    assert exc_caught is not None, "Expected PRCommentsIncomplete (budget exhausted)"
    out = buf.getvalue()
    assert "KG_RATE_LIMIT_WAIT:" not in out, (
        f"Expected no KG_RATE_LIMIT_WAIT: when budget exhausted, got:\n{out}"
    )
    # All sleeps should be _RETRY_DELAYS values (0s) not the 605s reset wait.
    assert all(s == 0 for s in sleep_calls), (
        f"Expected only 0-second sleeps (from _RETRY_DELAYS), got: {sleep_calls}"
    )
    print(f"PASS: budget exhausted: no reset wait, PRCommentsIncomplete raised, "
          f"sleep_calls={sleep_calls}")


def test_secondary_rate_limit_uses_retry_delays(repo: Path) -> None:
    """Remaining > 0 means secondary limit: gh api rate_limit is called but _RETRY_DELAYS used."""
    from rdflib import Graph

    mock_now = 1000.0

    sleep_calls: list[float] = []
    old_sleep = _spine_mod._sleep
    old_now = _spine_mod._now
    old_budget = _spine_mod._rate_limit_budget_s
    _spine_mod._sleep = lambda s: sleep_calls.append(s)
    _spine_mod._now = lambda: mock_now
    _spine_mod._rate_limit_budget_s = _spine_mod._RATE_LIMIT_WAIT_BUDGET_S

    try:
        with tempfile.TemporaryDirectory() as d:
            fake_dir = Path(d) / "bin"
            fake_dir.mkdir()
            call_log = Path(d) / "calls.log"
            call_log.write_text("")
            # remaining=5 means secondary limit, not hourly.
            _write_fake_gh_rate_limit_always(
                fake_dir, call_log,
                fail_endpoint_re=r"issues/3/comments",
                rl_core_remaining=5, rl_core_reset=1600,
                rl_graphql_remaining=100, rl_graphql_reset=9999,
            )
            import io
            buf = io.StringIO()
            with _with_fake_gh(fake_dir):
                g = Graph()
                exc_caught = None
                try:
                    from unittest.mock import patch
                    with patch("sys.stdout", buf):
                        add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=3)
                except PRCommentsIncomplete as e:
                    exc_caught = e
            calls = call_log.read_text().splitlines()
    finally:
        _spine_mod._sleep = old_sleep
        _spine_mod._now = old_now
        _spine_mod._rate_limit_budget_s = old_budget

    assert exc_caught is not None, "Expected PRCommentsIncomplete for secondary rate limit"
    assert any("rate_limit" in c for c in calls), (
        f"Expected gh api rate_limit to be called for secondary limit check, calls={calls}"
    )
    out = buf.getvalue()
    assert "KG_RATE_LIMIT_WAIT:" not in out, (
        f"Expected no KG_RATE_LIMIT_WAIT: for secondary limit, got:\n{out}"
    )
    # Sleeps are all _RETRY_DELAYS values (0s), not the reset duration.
    assert all(s == 0 for s in sleep_calls), (
        f"Expected _RETRY_DELAYS sleeps (0s each), got: {sleep_calls}"
    )
    print(f"PASS: secondary rate limit uses _RETRY_DELAYS, "
          f"rate_limit called, no reset wait, sleep_calls={sleep_calls}")


def test_rate_limit_check_fails_uses_retry_delays(repo: Path) -> None:
    """When gh api rate_limit fails, _RETRY_DELAYS is used and PRCommentsIncomplete raised."""
    from rdflib import Graph

    sleep_calls: list[float] = []
    old_sleep = _spine_mod._sleep
    old_budget = _spine_mod._rate_limit_budget_s
    _spine_mod._sleep = lambda s: sleep_calls.append(s)
    _spine_mod._rate_limit_budget_s = _spine_mod._RATE_LIMIT_WAIT_BUDGET_S

    try:
        with tempfile.TemporaryDirectory() as d:
            fake_dir = Path(d) / "bin"
            fake_dir.mkdir()
            call_log = Path(d) / "calls.log"
            call_log.write_text("")
            # Use the standard fake gh which does NOT handle rate_limit (exits 1 unhandled).
            _write_fake_gh(fake_dir, call_log, rate_limit_pr=3)

            with _with_fake_gh(fake_dir):
                g = Graph()
                exc_caught = None
                try:
                    add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=3)
                except PRCommentsIncomplete as e:
                    exc_caught = e
    finally:
        _spine_mod._sleep = old_sleep
        _spine_mod._rate_limit_budget_s = old_budget

    assert exc_caught is not None, "Expected PRCommentsIncomplete when rate_limit check fails"
    # All sleeps should be 0s (_RETRY_DELAYS), not any reset-based value.
    assert all(s == 0 for s in sleep_calls), (
        f"Expected only 0-second sleeps from _RETRY_DELAYS, got: {sleep_calls}"
    )
    print(f"PASS: rate_limit check fails → _RETRY_DELAYS used, sleep_calls={sleep_calls}")


def test_retry_after_no_rate_limit_check(repo: Path) -> None:
    """retry-after in error text: sleeps that value, never calls gh api rate_limit."""
    from rdflib import Graph

    retry_after_val = 30
    sleep_calls: list[float] = []
    old_sleep = _spine_mod._sleep
    old_budget = _spine_mod._rate_limit_budget_s
    _spine_mod._sleep = lambda s: sleep_calls.append(s)
    _spine_mod._rate_limit_budget_s = _spine_mod._RATE_LIMIT_WAIT_BUDGET_S

    try:
        with tempfile.TemporaryDirectory() as d:
            fake_dir = Path(d) / "bin"
            fake_dir.mkdir()
            call_log = Path(d) / "calls.log"
            call_log.write_text("")
            # Always fail issues/3/comments with retry-after; no rate_limit handler needed.
            _write_fake_gh_rate_limit_always(
                fake_dir, call_log,
                fail_endpoint_re=r"issues/3/comments",
                rl_core_remaining=0, rl_core_reset=9999,
                rl_graphql_remaining=0, rl_graphql_reset=9999,
            )
            # Rewrite to embed retry-after in the stderr message.
            script = fake_dir / "gh"
            src = script.read_text()
            src = src.replace(
                "'gh: API rate limit exceeded for installation ID 123.'",
                f"'gh: API rate limit exceeded. retry-after: {retry_after_val}'",
            )
            script.write_text(src)

            with _with_fake_gh(fake_dir):
                g = Graph()
                exc_caught = None
                try:
                    add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=3)
                except PRCommentsIncomplete as e:
                    exc_caught = e

            calls = call_log.read_text().splitlines()
    finally:
        _spine_mod._sleep = old_sleep
        _spine_mod._rate_limit_budget_s = old_budget

    assert exc_caught is not None, "Expected PRCommentsIncomplete when retry-after always set"
    assert not any("rate_limit" in c for c in calls), (
        f"gh api rate_limit should NOT be called when retry-after is present, calls={calls}"
    )
    assert all(s == retry_after_val for s in sleep_calls), (
        f"Expected all sleeps to be {retry_after_val}s (from retry-after), got: {sleep_calls}"
    )
    assert len(sleep_calls) == len(_spine_mod._RETRY_DELAYS), (
        f"Expected {len(_spine_mod._RETRY_DELAYS)} sleeps, got: {sleep_calls}"
    )
    print(f"PASS: retry-after used, rate_limit not called, sleep_calls={sleep_calls}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        repo = _make_fixture_repo(tmp)

        test_retry_and_incomplete(repo)
        test_clean_run_stats(repo)
        test_cli_exits_nonzero(repo)
        test_help_lists_no_docs_sites()
        test_no_docs_sites_skips_crawl(repo)
        test_pagination(repo)
        test_dedup_slug(repo)
        test_review_fetch_error_counts_toward_incomplete(repo)
        test_rest_comment_rate_limit_reset_wait(repo)
        test_graphql_rate_limit_reset_uses_graphql_key(repo)
        test_pr_list_rate_limit_reset_wait(repo)
        test_rate_limit_budget_exhausted(repo)
        test_secondary_rate_limit_uses_retry_delays(repo)
        test_rate_limit_check_fails_uses_retry_delays(repo)
        test_retry_after_no_rate_limit_check(repo)

    print(f"\n[OK] all tests passed — namespace={iris.NAMESPACE}")


if __name__ == "__main__":
    main()
