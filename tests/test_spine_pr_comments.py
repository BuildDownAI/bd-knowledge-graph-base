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
import re
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


def _write_fake_gh(fake_dir: Path, call_log: Path, rate_limit_batch: bool = True) -> None:
    """Write a fake `gh` executable into fake_dir.

    When rate_limit_batch is True (default), the first api graphql call rate-limits
    (simulating all 3 test PRs being in one failed batch).
    """
    script = fake_dir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, os, re\n"
        "\n"
        f"call_log = {str(call_log)!r}\n"
        f"rate_limit_batch = {rate_limit_batch!r}\n"
        "try:\n"
        "    with open(call_log) as _f:\n"
        "        prev_lines = _f.read().splitlines()\n"
        "except Exception:\n"
        "    prev_lines = []\n"
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
        "# GraphQL batched PR details\n"
        "if args[:2] == ['api', 'graphql']:\n"
        "    if rate_limit_batch:\n"
        "        print('API rate limit exceeded for this resource.', file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "    query_str = ''\n"
        "    for i, a in enumerate(args):\n"
        "        if a == '-f' and i + 1 < len(args) and args[i+1].startswith('query='):\n"
        "            query_str = args[i+1][6:]\n"
        "            break\n"
        "    pr_nums = [int(m.group(1)) for m in re.finditer(r'pr(\\d+): pullRequest', query_str)]\n"
        "    repo_data = {}\n"
        "    for n in pr_nums:\n"
        "        repo_data[f'pr{n}'] = {\n"
        "            'comments': {'totalCount': 0, 'nodes': []},\n"
        "            'reviews': {'totalCount': 0, 'nodes': []},\n"
        "            'reviewThreads': {'totalCount': 0, 'nodes': []},\n"
        "        }\n"
        "    print(json.dumps({'data': {'repository': repo_data}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
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
        "    query_str = ''\n"
        "    for i, a in enumerate(args):\n"
        "        if a == '-f' and i + 1 < len(args) and args[i+1].startswith('query='):\n"
        "            query_str = args[i+1][6:]\n"
        "            break\n"
        "    pr_nums = [int(m.group(1)) for m in re.finditer(r'pr(\\d+): pullRequest', query_str)]\n"
        "    repo_data = {}\n"
        "    for n in pr_nums:\n"
        "        repo_data[f'pr{n}'] = {\n"
        "            'comments': {'totalCount': 0, 'nodes': []},\n"
        "            'reviews': {'totalCount': 0, 'nodes': []},\n"
        "            'reviewThreads': {'totalCount': 0, 'nodes': []},\n"
        "        }\n"
        "    print(json.dumps({'data': {'repository': repo_data}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
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
        "    query_str = ''\n"
        "    for i, a in enumerate(args):\n"
        "        if a == '-f' and i + 1 < len(args) and args[i+1].startswith('query='):\n"
        "            query_str = args[i+1][6:]\n"
        "            break\n"
        "    pr_nums = [int(m.group(1)) for m in re.finditer(r'pr(\\d+): pullRequest', query_str)]\n"
        "    repo_data = {}\n"
        "    for n in pr_nums:\n"
        "        repo_data[f'pr{n}'] = {\n"
        "            'comments': {'totalCount': 0, 'nodes': []},\n"
        "            'reviews': {'totalCount': 0, 'nodes': []},\n"
        "            'reviewThreads': {'totalCount': 0, 'nodes': []},\n"
        "        }\n"
        "    print(json.dumps({'data': {'repository': repo_data}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
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
    """Batch GraphQL always rate-limits: 4 attempts made; PRCommentsIncomplete is raised."""
    from rdflib import Graph

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")
        # rate_limit_batch=True: every api graphql call rate-limits (never recovers)
        _write_fake_gh(fake_dir, call_log, rate_limit_batch=True)

        with _with_fake_gh(fake_dir):
            g = Graph()
            exc_caught = None
            try:
                add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=3)
            except PRCommentsIncomplete as e:
                exc_caught = e

        assert exc_caught is not None, "Expected PRCommentsIncomplete to be raised"
        print("PASS: PRCommentsIncomplete raised when rate-limit errors exceed threshold")

        # All 3 PRs are in one batch; that batch retries 4 times (1 initial + 3 retries)
        calls = call_log.read_text().splitlines()
        graphql_calls = [c for c in calls if "api graphql" in c]
        assert len(graphql_calls) >= 4, (
            f"Expected >= 4 api graphql calls (1 initial + 3 retries), "
            f"got {len(graphql_calls)}: {graphql_calls}"
        )
        print(f"PASS: api graphql called {len(graphql_calls)} times (>= 4)")

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
    """A fetch error on the batched GraphQL call counts toward KG_PR_COMMENTS_INCOMPLETE."""
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
            "# GraphQL: always rate-limit (never recovers)\n"
            "if args[:2] == ['api', 'graphql']:\n"
            "    print('API rate limit exceeded.', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "\n"
            "if args[:2] == ['api', '--paginate']:\n"
            "    path = args[2] if len(args) > 2 else ''\n"
            "    if re.search(r'/pulls', path):\n"
            "        print(json.dumps([\n"
            "            {'number': 1, 'title': 'PR 1', 'user': {'login': 'alice'},\n"
            "             'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',\n"
            "             'merge_commit_sha': 'aaa111', 'body': '', 'base': {'ref': 'main'}},\n"
            "        ]))\n"
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
        "# GraphQL batched PR details\n"
        "if args[:2] == ['api', 'graphql']:\n"
        "    query_str = ''\n"
        "    for i, a in enumerate(args):\n"
        "        if a == '-f' and i + 1 < len(args) and args[i+1].startswith('query='):\n"
        "            query_str = args[i+1][6:]\n"
        "            break\n"
        "    pr_nums = [int(m.group(1)) for m in re.finditer(r'pr(\\d+): pullRequest', query_str)]\n"
        "    repo_data = {}\n"
        "    for n in pr_nums:\n"
        "        repo_data[f'pr{n}'] = {\n"
        "            'comments': {'totalCount': 0, 'nodes': []},\n"
        "            'reviews': {'totalCount': 0, 'nodes': []},\n"
        "            'reviewThreads': {'totalCount': 0, 'nodes': []},\n"
        "        }\n"
        "    print(json.dumps({'data': {'repository': repo_data}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
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
        "# GraphQL batched PR details\n"
        "if args[:2] == ['api', 'graphql']:\n"
        "    query_str = ''\n"
        "    for i, a in enumerate(args):\n"
        "        if a == '-f' and i + 1 < len(args) and args[i+1].startswith('query='):\n"
        "            query_str = args[i+1][6:]\n"
        "            break\n"
        "    pr_nums = [int(m.group(1)) for m in re.finditer(r'pr(\\d+): pullRequest', query_str)]\n"
        "    repo_data = {}\n"
        "    for n in pr_nums:\n"
        "        repo_data[f'pr{n}'] = {\n"
        "            'comments': {'totalCount': 0, 'nodes': []},\n"
        "            'reviews': {'totalCount': 0, 'nodes': []},\n"
        "            'reviewThreads': {'totalCount': 0, 'nodes': []},\n"
        "        }\n"
        "    print(json.dumps({'data': {'repository': repo_data}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
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
        "\n"
        "print('fake gh: unhandled: ' + ' '.join(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)


def test_rest_comment_rate_limit_reset_wait(repo: Path) -> None:
    """Rate limit on the batched GraphQL call: waits for graphql reset+5s, retries, completes."""
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
                fail_endpoint_re=r"api graphql",
                rl_core_remaining=100, rl_core_reset=9999,
                rl_graphql_remaining=0, rl_graphql_reset=reset_epoch,
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
        f"Expected sleep(605) for graphql reset wait, got sleep_calls={sleep_calls}"
    )
    print(f"PASS: batched GraphQL rate limit reset wait: sleep_calls={sleep_calls}, "
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
            # Always rate-limit the batched graphql call so we exhaust retries.
            _write_fake_gh_rate_limit_always(
                fake_dir, call_log,
                fail_endpoint_re=r"api graphql",
                rl_core_remaining=100, rl_core_reset=9999,
                rl_graphql_remaining=0, rl_graphql_reset=reset_epoch,
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
            # graphql remaining=5 means secondary limit, not hourly.
            _write_fake_gh_rate_limit_always(
                fake_dir, call_log,
                fail_endpoint_re=r"api graphql",
                rl_core_remaining=100, rl_core_reset=9999,
                rl_graphql_remaining=5, rl_graphql_reset=1600,
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
            # rate_limit_batch=True: api graphql always rate-limits so rate_limit gets checked.
            _write_fake_gh(fake_dir, call_log, rate_limit_batch=True)

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
            # Always fail the batched graphql call with retry-after; no rate_limit handler needed.
            _write_fake_gh_rate_limit_always(
                fake_dir, call_log,
                fail_endpoint_re=r"api graphql",
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


def _write_fake_gh_with_data(fake_dir: Path, call_log: Path, pr_data_map: dict) -> None:
    """Fake gh that returns specific GraphQL data for each PR number.

    pr_data_map: {pr_number: {'comments': [...], 'reviews': [...], 'reviewThreads': [...]}}
    Each comments/reviews/reviewThreads value is a list of raw GraphQL nodes.
    """
    pr_data_json = json.dumps(pr_data_map)
    script = fake_dir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json, re\n"
        "\n"
        f"call_log = {str(call_log)!r}\n"
        f"pr_data_map = json.loads({pr_data_json!r})\n"
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
        "    query_str = ''\n"
        "    for i, a in enumerate(args):\n"
        "        if a == '-f' and i + 1 < len(args) and args[i+1].startswith('query='):\n"
        "            query_str = args[i+1][6:]\n"
        "            break\n"
        "    pr_nums = [int(m.group(1)) for m in re.finditer(r'pr(\\d+): pullRequest', query_str)]\n"
        "    repo_data = {}\n"
        "    for n in pr_nums:\n"
        "        key = str(n)\n"
        "        d = pr_data_map.get(key, {})\n"
        "        repo_data[f'pr{n}'] = {\n"
        "            'comments': {'totalCount': len(d.get('comments', [])), 'nodes': d.get('comments', [])},\n"
        "            'reviews': {'totalCount': len(d.get('reviews', [])), 'nodes': d.get('reviews', [])},\n"
        "            'reviewThreads': {'totalCount': len(d.get('reviewThreads', [])), 'nodes': d.get('reviewThreads', [])},\n"
        "        }\n"
        "    print(json.dumps({'data': {'repository': repo_data}}))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
        "    if re.search(r'/pulls', path):\n"
        "        prs = [{'number': int(k), 'title': f'PR {k}', 'user': {'login': 'alice'},\n"
        "                'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',\n"
        "                'merge_commit_sha': f'sha{k}', 'body': '', 'base': {'ref': 'main'}}\n"
        "               for k in pr_data_map]\n"
        "        print(json.dumps(prs))\n"
        "        sys.exit(0)\n"
        "\n"
        "print('fake gh: unhandled: ' + ' '.join(args), file=sys.stderr)\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)


def test_batch_call_count_200_prs(repo: Path) -> None:
    """200 PRs → exactly 10 api graphql calls and no REST per-PR endpoint calls."""
    from rdflib import Graph

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")
        _write_fake_gh_multipage(fake_dir, call_log, total_prs=200)

        with _with_fake_gh(fake_dir):
            g = Graph()
            add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=200)

        calls = call_log.read_text().splitlines()
    graphql_calls = [c for c in calls if "api graphql" in c]
    assert len(graphql_calls) == 10, (
        f"Expected exactly 10 api graphql calls for 200 PRs, got {len(graphql_calls)}"
    )
    rest_pr_calls = [c for c in calls if
                     re.search(r"issues/\d+/comments|pulls/\d+/comments|pulls/\d+/reviews", c)]
    assert not rest_pr_calls, (
        f"Expected no per-PR REST calls, got: {rest_pr_calls[:5]}"
    )
    print(f"PASS: 200 PRs → {len(graphql_calls)} graphql calls, 0 REST PR calls")


def test_bot_author_adds_bracket_bot_suffix(repo: Path) -> None:
    """GraphQL Bot author gets [bot] suffix; reviewerKind is 'codex'; node is BotAgent."""
    from rdflib import Graph
    from kg_ingest import iris as _iris

    bot_comment = {
        "databaseId": 9001,
        "body": "codex-review-receipt:v1 looks good",
        "author": {"login": "chatgpt-codex-connector", "__typename": "Bot"},
        "createdAt": "2026-05-01T10:00:00Z",
        "commit": {"oid": "abc123"},
        "originalCommit": {"oid": "abc123"},
        "path": "src/foo.py",
        "line": 42,
        "originalLine": None,
    }
    thread = {
        "isResolved": False, "isOutdated": False,
        "comments": {"totalCount": 1, "nodes": [bot_comment]},
    }

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")
        _write_fake_gh_with_data(fake_dir, call_log, {"1": {"reviewThreads": [thread]}})

        with _with_fake_gh(fake_dir):
            g = Graph()
            add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=1)

    from rdflib.namespace import RDF
    KG = _iris.KG
    login_lit = "chatgpt-codex-connector[bot]"
    nodes_with_login = list(g.subjects(_iris.KG.reviewerLogin,
                                        __import__("rdflib").Literal(login_lit)))
    assert nodes_with_login, (
        f"Expected a node with reviewerLogin {login_lit!r}; triples={list(g)[:5]}"
    )
    cnode = nodes_with_login[0]
    kind = str(next(g.objects(cnode, KG.reviewerKind)))
    assert kind == "codex", f"Expected reviewerKind 'codex', got {kind!r}"

    person_node = _iris.person(login_lit)
    ptype = str(next(g.objects(person_node, RDF.type), ""))
    assert "BotAgent" in ptype, f"Expected BotAgent person node, got {ptype!r}"
    print(f"PASS: bot author: login={login_lit!r}, kind={kind!r}, person type={ptype!r}")


def test_human_author_no_bot_suffix(repo: Path) -> None:
    """GraphQL User author has no [bot] suffix; reviewerKind is 'human'."""
    from rdflib import Graph
    from kg_ingest import iris as _iris

    human_comment = {
        "databaseId": 9002,
        "body": "This should be changed because it violates the invariant",
        "author": {"login": "alice", "__typename": "User"},
        "createdAt": "2026-05-01T10:00:00Z",
        "commit": {"oid": "abc123"},
        "originalCommit": {"oid": "abc123"},
        "path": "src/bar.py",
        "line": 10,
        "originalLine": None,
    }
    thread = {
        "isResolved": False, "isOutdated": False,
        "comments": {"totalCount": 1, "nodes": [human_comment]},
    }

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")
        _write_fake_gh_with_data(fake_dir, call_log, {"1": {"reviewThreads": [thread]}})

        with _with_fake_gh(fake_dir):
            g = Graph()
            add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=1)

    KG = _iris.KG
    login_lit = "alice"
    nodes_with_login = list(g.subjects(KG.reviewerLogin,
                                        __import__("rdflib").Literal(login_lit)))
    assert nodes_with_login, "Expected node with reviewerLogin 'alice'"
    cnode = nodes_with_login[0]
    kind = str(next(g.objects(cnode, KG.reviewerKind)))
    assert kind == "human", f"Expected reviewerKind 'human', got {kind!r}"
    print(f"PASS: human author: login={login_lit!r}, kind={kind!r}")


def test_mapping_table_rows(repo: Path) -> None:
    """Every mapping-table row emits the expected triple."""
    from rdflib import Graph, Literal, XSD
    from rdflib.namespace import DCTERMS
    from kg_ingest import iris as _iris

    KG = _iris.KG

    # Inline review comment: line=None → falls back to originalLine
    rc_node = {
        "databaseId": 7202388102,
        "body": "This variable name is misleading and should be renamed",
        "author": {"login": "reviewer1", "__typename": "User"},
        "createdAt": "2026-06-01T12:00:00Z",
        "commit": {"oid": "deadbeef"},
        "originalCommit": {"oid": "cafebabe"},
        "path": "src/module.py",
        "line": None,
        "originalLine": 55,
    }
    # Second comment: line is non-null → kg:reviewLine comes directly from line
    rc_node2 = {
        "databaseId": 7202388103,
        "body": "This function signature needs updating as well",
        "author": {"login": "reviewer1", "__typename": "User"},
        "createdAt": "2026-06-01T12:01:00Z",
        "commit": {"oid": "deadbeef"},
        "originalCommit": {"oid": "cafebabe"},
        "path": "src/module.py",
        "line": 42,
        "originalLine": 77,
    }
    thread = {
        "isResolved": True, "isOutdated": False,
        "comments": {"totalCount": 2, "nodes": [rc_node, rc_node2]},
    }
    # Review body
    review_node = {
        "databaseId": 6080016160,
        "body": "Overall the approach is correct but needs some cleanup before merge",
        "author": {"login": "reviewer2", "__typename": "User"},
        "submittedAt": "2026-06-01T13:00:00Z",
        "state": "CHANGES_REQUESTED",
        "commit": {"oid": "deadbeef"},
    }

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")
        _write_fake_gh_with_data(fake_dir, call_log, {
            "1": {"reviewThreads": [thread], "reviews": [review_node]}
        })

        with _with_fake_gh(fake_dir):
            g = Graph()
            add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=1)

    # --- inline review comment assertions ---
    cnode = _iris.pr_review_comment("test-org/repo", 1, 7202388102)
    assert (cnode, KG.commitId, Literal("deadbeef")) in g, "commitId not set"
    assert (cnode, KG.originalCommitId, Literal("cafebabe")) in g, "originalCommitId not set"
    assert (cnode, KG.reviewFilePath, Literal("src/module.py")) in g, "reviewFilePath not set"
    assert (cnode, KG.reviewLine, Literal(55, datatype=XSD.integer)) in g, (
        "reviewLine from originalLine not set"
    )
    assert (cnode, DCTERMS.created, Literal("2026-06-01T12:00:00Z", datatype=XSD.dateTime)) in g, (
        "dcterms:created not set"
    )
    assert (cnode, KG.threadState, Literal("resolved")) in g, "threadState not set"
    # IRI must contain the databaseId
    assert "7202388102" in str(cnode), f"node IRI {cnode!r} should contain databaseId"

    # second comment: line is non-null → kg:reviewLine must come directly from line, not originalLine
    cnode2 = _iris.pr_review_comment("test-org/repo", 1, 7202388103)
    assert (cnode2, KG.reviewLine, Literal(42, datatype=XSD.integer)) in g, (
        "reviewLine from non-null line not set (should be 42, not 77)"
    )

    # --- review body assertions ---
    rnode = _iris.pr_review_body("test-org/repo", 1, 6080016160)
    assert (rnode, KG.reviewState, Literal("CHANGES_REQUESTED")) in g, "reviewState not set"
    assert (rnode, KG.commitId, Literal("deadbeef")) in g, "review commitId not set"
    assert (rnode, DCTERMS.created, Literal("2026-06-01T13:00:00Z", datatype=XSD.dateTime)) in g, (
        "review dcterms:created (submittedAt) not set"
    )
    assert "6080016160" in str(rnode), f"review IRI {rnode!r} should contain databaseId"
    print("PASS: all mapping table rows verified")


def test_thread_state_first_and_second_comment(repo: Path) -> None:
    """First comment of resolved thread → 'resolved'; second → 'unknown'."""
    from rdflib import Graph
    from kg_ingest import iris as _iris

    KG = _iris.KG

    def _make_rc(db_id: int, body: str) -> dict:
        return {
            "databaseId": db_id,
            "body": body,
            "author": {"login": "reviewer", "__typename": "User"},
            "createdAt": "2026-06-01T12:00:00Z",
            "commit": {"oid": "abc"},
            "originalCommit": {"oid": "abc"},
            "path": "src/x.py",
            "line": 1,
            "originalLine": None,
        }

    thread = {
        "isResolved": True, "isOutdated": False,
        "comments": {"totalCount": 2, "nodes": [
            _make_rc(5001, "This needs fixing before merge can proceed"),
            _make_rc(5002, "Fixed in the latest commit as requested by reviewer"),
        ]},
    }

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")
        _write_fake_gh_with_data(fake_dir, call_log, {"1": {"reviewThreads": [thread]}})

        with _with_fake_gh(fake_dir):
            g = Graph()
            add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=1)

    cnode1 = _iris.pr_review_comment("test-org/repo", 1, 5001)
    cnode2 = _iris.pr_review_comment("test-org/repo", 1, 5002)

    state1 = str(next(g.objects(cnode1, KG.threadState), ""))
    state2 = str(next(g.objects(cnode2, KG.threadState), ""))

    assert state1 == "resolved", f"First comment: expected 'resolved', got {state1!r}"
    assert state2 == "unknown", f"Second comment: expected 'unknown', got {state2!r}"
    print(f"PASS: thread states: first={state1!r}, second={state2!r}")


def test_inline_comment_cap_logs_and_truncates(repo: Path) -> None:
    """PR with 120 inline review comments keeps exactly 100 and prints KG_PR_REVIEW_ITEMS_CAP."""
    from rdflib import Graph
    from rdflib.namespace import RDF
    from kg_ingest import iris as _iris

    def _make_rc(i: int) -> dict:
        return {
            "databaseId": 10000 + i,
            "body": f"This comment is substantive enough to be kept in the graph review {i}",
            "author": {"login": "reviewer", "__typename": "User"},
            "createdAt": "2026-06-01T12:00:00Z",
            "commit": {"oid": "abc"},
            "originalCommit": {"oid": "abc"},
            "path": "src/x.py",
            "line": i + 1,
            "originalLine": None,
        }

    # 120 single-comment threads → 120 inline review comments
    threads = [
        {"isResolved": False, "isOutdated": False,
         "comments": {"totalCount": 1, "nodes": [_make_rc(i)]}}
        for i in range(120)
    ]

    import io
    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")
        _write_fake_gh_with_data(fake_dir, call_log, {"1": {"reviewThreads": threads}})

        from unittest.mock import patch
        with _with_fake_gh(fake_dir), patch("sys.stdout", buf):
            g = Graph()
            add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=1)

    out = buf.getvalue()
    assert "KG_PR_REVIEW_ITEMS_CAP" in out, (
        f"Expected KG_PR_REVIEW_ITEMS_CAP in stdout, got:\n{out}"
    )
    review_comment_nodes = list(g.subjects(RDF.type, _iris.KG.Verification))
    assert len(review_comment_nodes) == 100, (
        f"Expected exactly 100 Verification nodes, got {len(review_comment_nodes)}"
    )
    print("PASS: cap: 120 inline comments → 100 kept, KG_PR_REVIEW_ITEMS_CAP logged")


def test_failed_batch_adds_errors_per_pr(repo: Path) -> None:
    """One failed batch of 20 PRs in a 200-PR run adds 20 entries to pr_comment_errors."""
    from rdflib import Graph

    with tempfile.TemporaryDirectory() as d:
        fake_dir = Path(d) / "bin"
        fake_dir.mkdir()
        call_log = Path(d) / "calls.log"
        call_log.write_text("")

        # Fail only the first batch (PRs 1-20), succeed for all others
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
            "    # Fail batch 1 (query contains 'pr1: pullRequest') always, succeed for others\n"
            "    is_batch1 = any('pr1: pullRequest' in a for a in args)\n"
            "    if is_batch1:\n"
            "        print('API rate limit exceeded for installation ID 123.', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    query_str = ''\n"
            "    for i, a in enumerate(args):\n"
            "        if a == '-f' and i + 1 < len(args) and args[i+1].startswith('query='):\n"
            "            query_str = args[i+1][6:]\n"
            "            break\n"
            "    pr_nums = [int(m.group(1)) for m in re.finditer(r'pr(\\d+): pullRequest', query_str)]\n"
            "    repo_data = {}\n"
            "    for n in pr_nums:\n"
            "        repo_data[f'pr{n}'] = {\n"
            "            'comments': {'totalCount': 0, 'nodes': []},\n"
            "            'reviews': {'totalCount': 0, 'nodes': []},\n"
            "            'reviewThreads': {'totalCount': 0, 'nodes': []},\n"
            "        }\n"
            "    print(json.dumps({'data': {'repository': repo_data}}))\n"
            "    sys.exit(0)\n"
            "\n"
            "if args[:2] == ['api', '--paginate']:\n"
            "    path = args[2] if len(args) > 2 else ''\n"
            "    if re.search(r'/pulls', path):\n"
            "        prs = [{'number': i, 'title': f'PR {i}', 'user': {'login': 'alice'},\n"
            "                'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',\n"
            "                'merge_commit_sha': f'sha{i}', 'body': '', 'base': {'ref': 'main'}}\n"
            "               for i in range(1, 201)]\n"
            "        print(json.dumps(prs))\n"
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
                add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=200)
            except PRCommentsIncomplete as e:
                exc_caught = e

    assert exc_caught is not None, "Expected PRCommentsIncomplete for 1 failed batch"
    stats = exc_caught.stats
    # 1 failed batch of 20 → 20 error entries; _RETRY_DELAYS exhausted (not rate-limit wait)
    # pr_comment_errors format: "N (first: ...)"
    err_str = stats.get("pr_comment_errors", "")
    err_count = int(err_str.split(" ")[0]) if err_str else 0
    assert err_count == 20, (
        f"Expected 20 error entries for 1 failed batch of 20, got: {err_str!r}"
    )
    print(f"PASS: failed batch of 20 → 20 error entries: {err_str!r}")


def test_graphql_rate_limit_batch_waits_and_completes(repo: Path) -> None:
    """Rate limit on first batch GraphQL call: waits for graphql reset, retries, no errors."""
    from rdflib import Graph

    mock_now = 1000.0
    graphql_reset = 1600  # 600 s in the future → candidate = 605

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
                fail_endpoint_re=r"api graphql",
                rl_core_remaining=100, rl_core_reset=9999,
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
        f"Expected no pr_comment_errors after retry, got: {stats.get('pr_comment_errors')}"
    )
    assert 605 in sleep_calls or 605.0 in sleep_calls, (
        f"Expected sleep(605) for graphql reset wait, got: {sleep_calls}"
    )
    print(f"PASS: graphql batch rate limit → wait {sleep_calls}, no errors")


def test_totalcount_cap_graphql_truncation(repo: Path) -> None:
    """Cap fires when totalCount > _MAX but first:100 returns exactly _MAX nodes."""
    from rdflib import Graph
    from rdflib.namespace import RDF
    from kg_ingest import iris as _iris

    def _make_rc(i: int) -> dict:
        return {
            "databaseId": 20000 + i,
            "body": f"This comment body is long enough to be substantive for item {i}",
            "author": {"login": "reviewer", "__typename": "User"},
            "createdAt": "2026-06-01T12:00:00Z",
            "commit": {"oid": "abc"},
            "originalCommit": {"oid": "abc"},
            "path": "src/x.py",
            "line": i + 1,
            "originalLine": None,
        }

    # Fake that returns totalCount=120 but only 100 thread nodes (mimics first:100 GraphQL limit)
    threads_100 = [
        {"isResolved": False, "isOutdated": False,
         "comments": {"totalCount": 1, "nodes": [_make_rc(i)]}}
        for i in range(100)
    ]
    pr_data_json = json.dumps({"1": {"reviewThreads": threads_100}})

    import io
    buf = io.StringIO()
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
            f"pr_data_map = json.loads({pr_data_json!r})\n"
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
            "    query_str = ''\n"
            "    for i, a in enumerate(args):\n"
            "        if a == '-f' and i + 1 < len(args) and args[i+1].startswith('query='):\n"
            "            query_str = args[i+1][6:]\n"
            "            break\n"
            "    pr_nums = [int(m.group(1)) for m in re.finditer(r'pr(\\d+): pullRequest', query_str)]\n"
            "    repo_data = {}\n"
            "    for n in pr_nums:\n"
            "        key = str(n)\n"
            "        d = pr_data_map.get(key, {})\n"
            "        threads = d.get('reviewThreads', [])\n"
            "        repo_data[f'pr{n}'] = {\n"
            "            'comments': {'totalCount': 0, 'nodes': []},\n"
            "            'reviews': {'totalCount': 0, 'nodes': []},\n"
            # totalCount=120 but only 100 nodes — simulates first:100 GraphQL page boundary
            "            'reviewThreads': {'totalCount': 120, 'nodes': threads},\n"
            "        }\n"
            "    print(json.dumps({'data': {'repository': repo_data}}))\n"
            "    sys.exit(0)\n"
            "\n"
            "if args[:2] == ['api', '--paginate']:\n"
            "    path = args[2] if len(args) > 2 else ''\n"
            "    if re.search(r'/pulls', path):\n"
            "        prs = [{'number': 1, 'title': 'PR 1', 'user': {'login': 'alice'},\n"
            "                'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',\n"
            "                'merge_commit_sha': 'sha1', 'body': '', 'base': {'ref': 'main'}}]\n"
            "        print(json.dumps(prs))\n"
            "        sys.exit(0)\n"
            "\n"
            "print('fake gh: unhandled: ' + ' '.join(args), file=sys.stderr)\n"
            "sys.exit(1)\n"
        )
        script.chmod(0o755)

        from unittest.mock import patch
        with _with_fake_gh(fake_dir), patch("sys.stdout", buf):
            g = Graph()
            add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=1)

    out = buf.getvalue()
    assert "KG_PR_REVIEW_ITEMS_CAP" in out, (
        f"Expected KG_PR_REVIEW_ITEMS_CAP when totalCount=120 > cap=100, got:\n{out}"
    )
    review_comment_nodes = list(g.subjects(RDF.type, _iris.KG.Verification))
    assert len(review_comment_nodes) == 100, (
        f"Expected exactly 100 Verification nodes, got {len(review_comment_nodes)}"
    )
    print("PASS: totalCount=120 with 100 nodes → cap fires, 100 Verification nodes kept")


def test_partial_success_top_level_errors(repo: Path) -> None:
    """GraphQL partial success: errors + valid alias → valid PR processed, null alias errors."""
    from rdflib import Graph
    from rdflib.namespace import RDF
    from kg_ingest import iris as _iris

    rc_node = {
        "databaseId": 8001,
        "body": "This comment comes from a partially successful GraphQL response",
        "author": {"login": "alice", "__typename": "User"},
        "createdAt": "2026-06-01T12:00:00Z",
        "commit": {"oid": "abc"},
        "originalCommit": {"oid": "abc"},
        "path": "src/x.py",
        "line": 10,
        "originalLine": None,
    }
    thread = {
        "isResolved": False, "isOutdated": False,
        "comments": {"totalCount": 1, "nodes": [rc_node]},
    }
    pr1_data = {
        "comments": {"totalCount": 0, "nodes": []},
        "reviews": {"totalCount": 0, "nodes": []},
        "reviewThreads": {"totalCount": 1, "nodes": [thread]},
    }
    pr1_json = json.dumps(pr1_data)

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
            f"pr1_data = json.loads({pr1_json!r})\n"
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
            # Return errors + partial data: pr1 valid, pr2 null
            "    repo_data = {'pr1': pr1_data, 'pr2': None}\n"
            "    response = {\n"
            "        'errors': [{'message': 'Could not resolve pr2'}],\n"
            "        'data': {'repository': repo_data},\n"
            "    }\n"
            "    print(json.dumps(response))\n"
            "    sys.exit(0)\n"
            "\n"
            "if args[:2] == ['api', '--paginate']:\n"
            "    path = args[2] if len(args) > 2 else ''\n"
            "    if re.search(r'/pulls', path):\n"
            "        prs = [\n"
            "            {'number': 1, 'title': 'PR 1', 'user': {'login': 'alice'},\n"
            "             'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',\n"
            "             'merge_commit_sha': 'sha1', 'body': '', 'base': {'ref': 'main'}},\n"
            "            {'number': 2, 'title': 'PR 2', 'user': {'login': 'alice'},\n"
            "             'state': 'closed', 'merged_at': '2026-01-01T00:00:00Z',\n"
            "             'merge_commit_sha': 'sha2', 'body': '', 'base': {'ref': 'main'}},\n"
            "        ]\n"
            "        print(json.dumps(prs))\n"
            "        sys.exit(0)\n"
            "\n"
            "print('fake gh: unhandled: ' + ' '.join(args), file=sys.stderr)\n"
            "sys.exit(1)\n"
        )
        script.chmod(0o755)

        exc_caught = None
        with _with_fake_gh(fake_dir):
            g = Graph()
            try:
                add_spine(g, repo, "test-org/repo", max_commits=None, max_prs=2)
            except PRCommentsIncomplete as e:
                exc_caught = e
        stats = exc_caught.stats if exc_caught is not None else {}

    # PR 1 had valid data → its review comment node should be in the graph
    cnode = _iris.pr_review_comment("test-org/repo", 1, 8001)
    assert (cnode, RDF.type, _iris.KG.Verification) in g, (
        "PR 1 review comment missing despite valid partial GraphQL data"
    )
    # PR 2 had null data → should contribute one error entry
    err_str = stats.get("pr_comment_errors", "")
    err_count = int(err_str.split(" ")[0]) if err_str else 0
    assert err_count == 1, (
        f"Expected 1 error for null pr2 alias in partial-success response, got {err_str!r}"
    )
    print(f"PASS: partial success with top-level errors: PR 1 processed, PR 2 error counted")


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
        test_batch_call_count_200_prs(repo)
        test_bot_author_adds_bracket_bot_suffix(repo)
        test_human_author_no_bot_suffix(repo)
        test_mapping_table_rows(repo)
        test_thread_state_first_and_second_comment(repo)
        test_inline_comment_cap_logs_and_truncates(repo)
        test_failed_batch_adds_errors_per_pr(repo)
        test_graphql_rate_limit_batch_waits_and_completes(repo)
        test_totalcount_cap_graphql_truncation(repo)
        test_partial_success_top_level_errors(repo)

    print(f"\n[OK] all tests passed — namespace={iris.NAMESPACE}")


if __name__ == "__main__":
    main()
