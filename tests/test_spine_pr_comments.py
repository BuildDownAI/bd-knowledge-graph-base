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
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Patch retry delays to zero before any spine call so tests run instantly.
import kg_ingest.spine as _spine_mod
_spine_mod._RETRY_DELAYS = (0, 0, 0)

from kg_ingest import iris
from kg_ingest.spine import PRCommentsIncomplete, add_spine


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
        "if args[:2] == ['pr', 'list']:\n"
        "    print(json.dumps([\n"
        "        {'number': 1, 'title': 'PR 1', 'author': {'login': 'alice'},"
        " 'state': 'MERGED', 'mergedAt': '2026-01-01T00:00:00Z', 'body': ''},\n"
        "        {'number': 2, 'title': 'PR 2', 'author': {'login': 'bob'},"
        " 'state': 'MERGED', 'mergedAt': '2026-01-02T00:00:00Z', 'body': ''},\n"
        "        {'number': 3, 'title': 'PR 3', 'author': {'login': 'carol'},"
        " 'state': 'OPEN', 'mergedAt': None, 'body': ''},\n"
        "    ]))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
        "    path = args[2] if len(args) > 2 else ''\n"
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
        "import sys, json\n"
        "\n"
        f"call_log = {str(call_log)!r}\n"
        "with open(call_log, 'a') as _f:\n"
        "    _f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['pr', 'list']:\n"
        "    print(json.dumps([\n"
        "        {'number': 1, 'title': 'PR 1', 'author': {'login': 'alice'},"
        " 'state': 'MERGED', 'mergedAt': '2026-01-01T00:00:00Z', 'body': ''},\n"
        "        {'number': 2, 'title': 'PR 2', 'author': {'login': 'bob'},"
        " 'state': 'MERGED', 'mergedAt': '2026-01-02T00:00:00Z', 'body': ''},\n"
        "    ]))\n"
        "    sys.exit(0)\n"
        "\n"
        "if args[:2] == ['api', '--paginate']:\n"
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


def test_cli_exits_nonzero(repo: Path) -> None:
    """cli.main() returns 1 and prints KG_PR_COMMENTS_INCOMPLETE when PRCommentsIncomplete raised."""
    from kg_ingest import cli as cli_mod

    fake_stats = {
        "files": 1, "commits": 1, "people": 1, "prs": 3, "pr_comments": 0,
        "pr_comment_errors": "1 (first: API rate limit exceeded for this resource.)",
        "commit_cap": None, "pr_cap": 3,
    }
    fake_exc = PRCommentsIncomplete(fake_stats)

    with patch.object(_spine_mod, "add_spine", side_effect=fake_exc):
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


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        repo = _make_fixture_repo(tmp)

        test_retry_and_incomplete(repo)
        test_clean_run_stats(repo)
        test_cli_exits_nonzero(repo)

    print(f"\n[OK] all tests passed — namespace={iris.NAMESPACE}")


if __name__ == "__main__":
    main()
