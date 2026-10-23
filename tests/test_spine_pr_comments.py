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
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
        "    # PR list fetch (new paginated REST API)\n"
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
        "    # PR comments fetch\n"
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

    print(f"\n[OK] all tests passed — namespace={iris.NAMESPACE}")


if __name__ == "__main__":
    main()
