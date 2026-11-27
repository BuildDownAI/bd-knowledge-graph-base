"""Spine ingester — deterministic, GitHub/git-derivable, zero LLM (recon §1.1/§5.3).

Reads local git (repo, files, commits, people) and optionally GitHub PRs via `gh`,
emitting RDF into the spine named graph. Re-derivable: same repo state -> same triples.
"""
from __future__ import annotations

import fnmatch
from datetime import datetime
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD, Namespace

from . import iris, normalize
from .tracker import _classify_pr_comment, _first_line, should_keep_review_item

KG = iris.KG
PROV = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")

DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt"}

_MAX_PR_COMMENTS = 50
_MAX_PR_REVIEW_ITEMS = 100

# Retry delays (seconds) for rate-limited PR comment fetches.
# Injectable for tests: monkeypatch spine._RETRY_DELAYS = (0, 0, 0).
_RETRY_DELAYS: tuple[int, ...] = (5, 15, 45)

# Total seconds this process may spend waiting for a GitHub hourly limit reset.
# Sized to fit one full reset; the orchestrator ends a refresh after 4 hours
# (KG_REFRESH_TOTAL_DEADLINE_MS in BuildDownAI/AI-Implement).
_RATE_LIMIT_WAIT_BUDGET_S: float = 75 * 60

# Mutable runtime budget; decremented on each reset wait (shared across all repos in the run).
# Injectable for tests: monkeypatch _rate_limit_budget_s = _RATE_LIMIT_WAIT_BUDGET_S.
_rate_limit_budget_s: float = _RATE_LIMIT_WAIT_BUDGET_S

# Injectable for tests: monkeypatch to avoid real waits.
_sleep = time.sleep
_now = time.time

_PR_DETAIL_BATCH = 20


def _parse_paginated_json(text: str) -> tuple[list[dict], int]:
    """Parse concatenated JSON arrays from `gh api --paginate`; returns (items, page_count).

    gh api --paginate outputs each page's JSON array separately (not combined),
    so the stdout may be multiple valid JSON arrays concatenated.
    """
    result: list[dict] = []
    pages = 0
    decoder = json.JSONDecoder()
    pos = 0
    text = text.strip()
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        try:
            obj, new_pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            break
        if isinstance(obj, list):
            result.extend(obj)
            pages += 1
        pos = new_pos
    return result, pages


class PRCommentsIncomplete(Exception):
    """Raised by add_spine when PR comment fetch errors exceed the safe threshold.

    Carries the partially-built stats dict (pr_comments and pr_comment_errors
    already formatted) so cli.py can print them before exiting non-zero.
    """
    def __init__(self, stats: dict) -> None:
        self.stats = stats
        super().__init__(
            f"KG_PR_COMMENTS_INCOMPLETE: {stats.get('pr_comment_errors', 'N/A')}"
        )

# Doc pages get a real title + snippet at spine time (AII-345) so they can be
# embedded as search cards — a Doc node that is only a path is invisible to
# search. Read cap keeps ingest fast; extraction is best-effort and never fatal.
_DOC_READ_CAP = 4096
_DOC_SNIPPET_CHARS = 400


def doc_meta(text: str, fallback_title: str) -> tuple[str, str]:
    """Extract (title, snippet) from a doc page's leading text.

    Title precedence: frontmatter `title:` -> first markdown H1 -> fallback
    (filename). Snippet: the first non-heading body characters after
    frontmatter, capped. Pure function, exported for tests.
    """
    lines = text.splitlines()
    title = ""
    body_start = 0
    if lines and lines[0].strip() == "---":          # frontmatter block
        for i in range(1, len(lines)):
            s = lines[i].strip()
            if s == "---":
                body_start = i + 1
                break
            if s.lower().startswith("title:") and not title:
                title = s[6:].strip().strip("'\"")
    body: list[str] = []
    for line in lines[body_start:]:
        s = line.strip()
        if not title and s.startswith("# "):
            title = s[2:].strip()
            continue
        if s and not s.startswith("#"):
            body.append(s)
        if sum(len(b) for b in body) > _DOC_SNIPPET_CHARS:
            break
    return (title or fallback_title), " ".join(body)[:_DOC_SNIPPET_CHARS]


def _git(repo_path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _is_bot(login: str) -> bool:
    login_lower = login.lower()
    return login_lower.endswith("[bot]") or login_lower.endswith("-bot") or "bot]" in login_lower


def _author_login(author: dict | None) -> str:
    """Convert a GraphQL author node to a REST-style login string."""
    if author is None:
        return "unknown"
    login = author.get("login") or "unknown"
    if author.get("__typename") == "Bot" and not login.lower().endswith("[bot]"):
        login = f"{login}[bot]"
    return login


def _pr_comment_run_node(g: Graph, run_id: str, pipeline_ver: str) -> URIRef:
    r = URIRef(f"{iris.KGR}run/{run_id}")
    g.add((r, RDF.type, KG.ExtractionRun))
    g.add((r, KG.pipelineVer, Literal(pipeline_ver)))
    g.add((r, KG.model, Literal("deterministic-spine")))
    g.add((r, KG.promptHash, Literal("n/a-deterministic")))
    g.add((r, KG.notes, Literal("Phase-1 deterministic PR-comment ingest")))
    return r


def _add_pr_comments(g: Graph, run: URIRef, repo_slug: str, pr_number: int,
                     pnode: URIRef, comments: list[dict], stats: dict) -> int:
    """Classify and emit nodes for PR comments. Returns count of nodes added."""
    count = 0
    for cm in comments:
        body = (cm.get("body") or "").strip()
        login = (cm.get("user") or {}).get("login") or "unknown"
        kind = _classify_pr_comment(body, login)
        if not kind:
            continue
        cid = cm["id"]
        cnode = iris.pr_comment(repo_slug, pr_number, cid)
        cls = (KG.Learning if kind == "learning"
               else KG.Verification if kind == "verification"
               else KG.Decision)
        g.add((cnode, RDF.type, cls))
        title = _first_line(body)[:400] or str(cid)
        g.add((cnode, DCTERMS.title, Literal(title, datatype=XSD.string)))
        g.add((cnode, KG.fix, Literal(body[:400])))
        created_at = cm.get("created_at") or ""
        if created_at:
            g.add((cnode, DCTERMS.created, Literal(created_at, datatype=XSD.dateTime)))
        p = iris.person(login)
        g.add((p, RDF.type, KG.BotAgent if _is_bot(login) else KG.Person))
        g.add((p, KG.login, Literal(login)))
        g.add((cnode, PROV.wasAttributedTo, p))
        g.add((cnode, KG.about, pnode))
        g.add((cnode, PROV.wasDerivedFrom, pnode))
        g.add((cnode, PROV.wasGeneratedBy, run))
        count += 1
    stats["pr_comments"] = stats.get("pr_comments", 0) + count
    return count


def _strip_badges(text: str) -> str:
    """Remove markdown badge markup like ![P2](...), keeping alt text."""
    return re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)


def _gh_api(cmd_args: list[str], cwd: str | None = None) -> str:
    """Run `gh <cmd_args>` and return stdout. Owns the full retry and rate-limit rule.

    Retry rules, applied in order on each CalledProcessError with a rate-limit message:
    - retry-after in error text: wait that many seconds, then retry.
    - Hourly limit (remaining==0): run `gh api rate_limit`, wait until reset+5s (up to
      _rate_limit_budget_s), log KG_RATE_LIMIT_WAIT:, then retry.
    - Secondary limit (remaining>0), rate_limit check fails, or budget exhausted: use
      _RETRY_DELAYS.
    Raises after len(_RETRY_DELAYS)+1 total attempts.

    is_graphql: cmd_args starts with ["api", "graphql"] — stable for current call sites
    and the planned KGB-61 batched-GraphQL calls; determines which rate-limit resource to
    read ("graphql" vs "core").
    """
    global _rate_limit_budget_s
    is_graphql = len(cmd_args) >= 2 and cmd_args[0] == "api" and cmd_args[1] == "graphql"
    limit_key = "graphql" if is_graphql else "core"

    for _attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            return subprocess.run(
                ["gh", *cmd_args],
                capture_output=True, text=True, check=True, cwd=cwd,
            ).stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            stderr = getattr(exc, "stderr", None) or ""
            err_text = stderr or str(exc)
            is_rate_limit = "rate limit" in err_text.lower() or "abuse" in err_text.lower()
            if not is_rate_limit or _attempt >= len(_RETRY_DELAYS):
                raise
            m = re.search(r"retry.?after[:\s]+(\d+)", err_text, re.IGNORECASE)
            if m:
                _sleep(int(m.group(1)))
                continue
            # Hourly-limit path: check the reset time via gh api rate_limit.
            wait: float | None = None
            try:
                rl_raw = subprocess.run(
                    ["gh", "api", "rate_limit"],
                    capture_output=True, text=True, check=True, cwd=cwd,
                ).stdout
                rl = json.loads(rl_raw)
                resource = rl["resources"][limit_key]
                if resource["remaining"] == 0:
                    reset = resource["reset"]
                    if isinstance(reset, (int, float)):
                        candidate = max(0.0, float(reset) - _now()) + 5
                        if candidate <= _rate_limit_budget_s:
                            wait = candidate
                            _rate_limit_budget_s -= wait
            except Exception:
                pass
            if wait is not None:
                print(f"KG_RATE_LIMIT_WAIT: {int(wait)} s ({limit_key})")
                _sleep(wait)
            else:
                _sleep(_RETRY_DELAYS[_attempt])
    raise RuntimeError("unreachable")  # loop always returns or raises


def _build_pr_graphql_field(n: int) -> str:
    return (
        f"pr{n}: pullRequest(number: {n}) {{"
        " comments(first: 50) { totalCount nodes { databaseId body"
        " author { login __typename } createdAt } }"
        " reviews(first: 100) { totalCount nodes { databaseId body"
        " author { login __typename } submittedAt state commit { oid } } }"
        " reviewThreads(first: 100) { totalCount nodes { isResolved isOutdated"
        " comments(first: 100) { totalCount nodes { databaseId body"
        " author { login __typename } createdAt commit { oid }"
        " originalCommit { oid } path line originalLine } } } }"
        " }"
    )


def _fetch_pr_details_batch(
    repo_path: Path, repo_slug: str, pr_numbers: list[int],
    _pr_errors: list[str],
) -> dict[int, dict]:
    """Fetch comments, reviews, and review threads for up to _PR_DETAIL_BATCH PRs per GraphQL call.

    Returns {pr_number: {comments, review_comments, reviews, thread_states}}.
    Failed batches or null aliases add one entry per PR to _pr_errors and return empty data.
    """
    owner, repo_name = repo_slug.split("/", 1)
    result: dict[int, dict] = {}
    _empty: dict = {"comments": [], "review_comments": [], "reviews": [], "thread_states": {}}

    for batch_start in range(0, len(pr_numbers), _PR_DETAIL_BATCH):
        batch = pr_numbers[batch_start : batch_start + _PR_DETAIL_BATCH]
        pr_fields = " ".join(_build_pr_graphql_field(n) for n in batch)
        query = (
            "query($owner: String!, $name: String!) {"
            f" repository(owner: $owner, name: $name) {{ {pr_fields} }}"
            " }"
        )
        try:
            stdout = _gh_api(
                ["api", "graphql",
                 "-f", f"query={query}",
                 "-F", f"owner={owner}",
                 "-F", f"name={repo_name}"],
                cwd=str(repo_path),
            )
            data = json.loads(stdout)
        except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as exc:
            err_text = (getattr(exc, "stderr", None) or str(exc))[:300]
            for n in batch:
                _pr_errors.append(err_text)
                result[n] = dict(_empty)
            continue

        repo_data = (data.get("data") or {}).get("repository") or {}

        for n in batch:
            pr_node = repo_data.get(f"pr{n}")
            if pr_node is None:
                _pr_errors.append(f"null alias pr{n} in GraphQL response")
                result[n] = dict(_empty)
                continue

            comments = [
                {
                    "id": cm["databaseId"],
                    "body": cm.get("body") or "",
                    "user": {"login": _author_login(cm.get("author"))},
                    "created_at": cm.get("createdAt") or "",
                }
                for cm in ((pr_node.get("comments") or {}).get("nodes") or [])
                if cm
            ]

            reviews_obj = pr_node.get("reviews") or {}
            reviews_total_count = reviews_obj.get("totalCount") or 0
            reviews_raw = reviews_obj.get("nodes") or []
            reviews = [
                {
                    "id": rv["databaseId"],
                    "body": rv.get("body") or "",
                    "user": {"login": _author_login(rv.get("author"))},
                    "submitted_at": rv.get("submittedAt") or "",
                    "state": rv.get("state") or "",
                    "commit_id": (rv.get("commit") or {}).get("oid") or "",
                }
                for rv in reviews_raw
                if rv
            ]

            threads_obj = pr_node.get("reviewThreads") or {}
            threads_total_count = threads_obj.get("totalCount") or 0
            threads = threads_obj.get("nodes") or []
            review_comments: list[dict] = []
            thread_states: dict[int, str] = {}

            for thread in threads:
                if not thread:
                    continue
                state = (
                    "resolved" if thread.get("isResolved")
                    else "outdated" if thread.get("isOutdated")
                    else "open"
                )
                for idx, tc in enumerate((thread.get("comments") or {}).get("nodes") or []):
                    if not tc:
                        continue
                    cid = tc["databaseId"]
                    review_comments.append({
                        "id": cid,
                        "body": tc.get("body") or "",
                        "user": {"login": _author_login(tc.get("author"))},
                        "created_at": tc.get("createdAt") or "",
                        "commit_id": (tc.get("commit") or {}).get("oid") or "",
                        "original_commit_id": (tc.get("originalCommit") or {}).get("oid") or "",
                        "path": tc.get("path") or "",
                        "line": tc.get("line"),
                        "original_line": tc.get("originalLine"),
                    })
                    if idx == 0:
                        thread_states[int(cid)] = state

            rc_cap_count = max(len(review_comments), threads_total_count)
            if rc_cap_count > _MAX_PR_REVIEW_ITEMS:
                print(
                    f"KG_PR_REVIEW_ITEMS_CAP: PR {n} has "
                    f"{rc_cap_count} inline review comments; "
                    f"capped at {_MAX_PR_REVIEW_ITEMS}"
                )
                review_comments = review_comments[:_MAX_PR_REVIEW_ITEMS]

            rv_cap_count = max(len(reviews), reviews_total_count)
            if rv_cap_count > _MAX_PR_REVIEW_ITEMS:
                print(
                    f"KG_PR_REVIEW_ITEMS_CAP: PR {n} has "
                    f"{rv_cap_count} review bodies; "
                    f"capped at {_MAX_PR_REVIEW_ITEMS}"
                )
                reviews = reviews[:_MAX_PR_REVIEW_ITEMS]

            result[n] = {
                "comments": comments[:_MAX_PR_COMMENTS],
                "review_comments": review_comments,
                "reviews": reviews,
                "thread_states": thread_states,
            }

    return result


def _add_pr_review_content(
    g: Graph, run: URIRef, repo_slug: str, pr_number: int,
    pnode: URIRef, review_comments: list[dict],
    reviews: list[dict], thread_states: dict[int, str],
    stats: dict,
) -> int:
    """Classify and emit kg:Verification nodes for PR review content. Returns count added."""
    count = 0

    for rc in review_comments:
        body = (rc.get("body") or "").strip()
        login = (rc.get("user") or {}).get("login") or "unknown"
        keep, reviewer_kind, severity = should_keep_review_item(body, login, is_review_body=False)
        if not keep:
            continue
        cid = rc["id"]
        cnode = iris.pr_review_comment(repo_slug, pr_number, cid)
        g.add((cnode, RDF.type, KG.Verification))
        title_text = _first_line(_strip_badges(body))[:400] or str(cid)
        g.add((cnode, DCTERMS.title, Literal(title_text, datatype=XSD.string)))
        g.add((cnode, KG.fix, Literal(body[:400])))
        created_at = rc.get("created_at") or ""
        if created_at:
            g.add((cnode, DCTERMS.created, Literal(created_at, datatype=XSD.dateTime)))
        p = iris.person(login)
        g.add((p, RDF.type, KG.BotAgent if _is_bot(login) else KG.Person))
        g.add((p, KG.login, Literal(login)))
        g.add((cnode, PROV.wasAttributedTo, p))
        g.add((cnode, KG.about, pnode))
        g.add((cnode, PROV.wasDerivedFrom, pnode))
        g.add((cnode, PROV.wasGeneratedBy, run))
        g.add((cnode, KG.reviewerLogin, Literal(login)))
        g.add((cnode, KG.reviewerKind, Literal(reviewer_kind)))
        g.add((cnode, KG.severity, Literal(severity)))
        path = rc.get("path") or ""
        if path:
            g.add((cnode, KG.reviewFilePath, Literal(path)))
        line = rc.get("line") if rc.get("line") is not None else rc.get("original_line")
        if line is not None:
            g.add((cnode, KG.reviewLine, Literal(int(line), datatype=XSD.integer)))
        commit_id = rc.get("commit_id") or ""
        if commit_id:
            g.add((cnode, KG.commitId, Literal(commit_id)))
        original_commit_id = rc.get("original_commit_id") or ""
        if original_commit_id:
            g.add((cnode, KG.originalCommitId, Literal(original_commit_id)))
        thread_state = thread_states.get(cid, "unknown")
        g.add((cnode, KG.threadState, Literal(thread_state)))
        count += 1

    for rev in reviews:
        body = (rev.get("body") or "").strip()
        login = (rev.get("user") or {}).get("login") or "unknown"
        review_state = rev.get("state") or ""
        keep, reviewer_kind, severity = should_keep_review_item(
            body, login, is_review_body=True, review_state=review_state
        )
        if not keep:
            continue
        rev_id = rev["id"]
        rnode = iris.pr_review_body(repo_slug, pr_number, rev_id)
        g.add((rnode, RDF.type, KG.Verification))
        title_text = _first_line(_strip_badges(body))[:400] or str(rev_id)
        g.add((rnode, DCTERMS.title, Literal(title_text, datatype=XSD.string)))
        g.add((rnode, KG.fix, Literal(body[:400])))
        submitted_at = rev.get("submitted_at") or ""
        if submitted_at:
            g.add((rnode, DCTERMS.created, Literal(submitted_at, datatype=XSD.dateTime)))
        p = iris.person(login)
        g.add((p, RDF.type, KG.BotAgent if _is_bot(login) else KG.Person))
        g.add((p, KG.login, Literal(login)))
        g.add((rnode, PROV.wasAttributedTo, p))
        g.add((rnode, KG.about, pnode))
        g.add((rnode, PROV.wasDerivedFrom, pnode))
        g.add((rnode, PROV.wasGeneratedBy, run))
        g.add((rnode, KG.reviewerLogin, Literal(login)))
        g.add((rnode, KG.reviewerKind, Literal(reviewer_kind)))
        g.add((rnode, KG.severity, Literal(severity)))
        g.add((rnode, KG.reviewState, Literal(review_state)))
        commit_id = rev.get("commit_id") or ""
        if commit_id:
            g.add((rnode, KG.commitId, Literal(commit_id)))
        count += 1

    stats["pr_review_items"] = stats.get("pr_review_items", 0) + count
    return count


def _bind(g: Graph) -> None:
    g.bind("kg", KG)
    g.bind("kgr", iris.KGR)
    g.bind("prov", PROV)
    g.bind("dcterms", DCTERMS)
    g.bind("foaf", FOAF)


def _link_tracker(g: Graph, node: URIRef, text: str, stats: dict) -> None:
    """Scan `text` for tracker keys (PROJ-123, …) and wire node <-> issue.

    Closes the loop to code (§2.7): a PR/commit that mentions an issue key gets
    a `references` edge, and the issue (a stub here; fleshed out by the tracker
    ingester) gets an `addressedBy` edge back to that PR/commit."""
    for key in normalize.parse_tracker_keys(text):
        iss = iris.tracker_issue(key)
        g.add((iss, RDF.type, KG.Issue))
        g.add((iss, KG.trackerKey, Literal(key)))
        g.add((node, KG.references, iss))
        g.add((iss, KG.addressedBy, node))
        stats["tracker_refs"] = stats.get("tracker_refs", 0) + 1


def add_spine(g: Graph, repo_path: Path, repo_slug: str,
              max_commits: int | None = 500, max_prs: int = 60,
              docs_url: str | None = None,
              doc_exclude: list[str] | None = None,
              pipeline_ver: str = "0.1.0",
              history_branch: str | None = None) -> dict:
    """Populate `g` with spine triples. Returns a stats dict (with any caps applied)."""
    _bind(g)
    repo_path = Path(repo_path)
    stats: dict[str, Any] = {"files": 0, "commits": 0, "people": 0, "prs": 0,
                              "pr_comments": 0, "pr_review_items": 0,
                              "commit_cap": max_commits, "pr_cap": max_prs}
    people: set[str] = set()

    # --- repo node ---
    repo_iri = iris.repo(repo_slug)
    g.add((repo_iri, RDF.type, KG.Repo))
    g.add((repo_iri, KG.sourceSystem, Literal("github")))
    g.add((repo_iri, KG.sourceId, Literal(repo_slug)))
    g.add((repo_iri, DCTERMS.title, Literal(repo_slug)))
    if docs_url:
        g.add((repo_iri, KG.docsUrl, Literal(docs_url)))

    # --- history branch, snapshot ref, default-branch flag ---
    if not history_branch:
        try:
            history_branch = _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD").strip()
        except Exception:
            history_branch = "HEAD"
    try:
        snapshot_ref = _git(repo_path, "rev-parse", "HEAD").strip()
    except Exception:
        snapshot_ref = ""
    is_default_branch = False
    try:
        default_branch = subprocess.run(
            ["gh", "api", f"repos/{repo_slug}", "--jq", ".default_branch"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        is_default_branch = (history_branch == default_branch)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    g.add((repo_iri, KG.historyBranch, Literal(history_branch)))
    g.add((repo_iri, KG.isDefaultBranch, Literal(is_default_branch, datatype=XSD.boolean)))
    if snapshot_ref:
        g.add((repo_iri, KG.snapshotRef, Literal(snapshot_ref)))
    stats["history_branch"] = history_branch
    stats["is_default_branch"] = is_default_branch

    # --- files ---
    for rel in _git(repo_path, "ls-files").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        suffix = Path(rel).suffix.lower()
        is_doc = suffix in DOC_SUFFIXES
        # fnmatch * matches any character including /, so "sub/**" and "*.mdx"
        # both work as expected — same contract as docsite._path_matches_any.
        if is_doc and doc_exclude and any(fnmatch.fnmatch(rel, p) for p in doc_exclude):
            stats["docs_excluded"] = stats.get("docs_excluded", 0) + 1
            continue
        node = iris.doc(repo_slug, rel) if is_doc else iris.file(repo_slug, rel)
        g.add((node, RDF.type, KG.Doc if is_doc else KG.File))
        g.add((node, KG.path, Literal(rel)))
        g.add((node, KG.partOf, repo_iri))
        if is_doc:
            try:
                text = (repo_path / rel).read_text(errors="ignore")[:_DOC_READ_CAP]
                title, snippet = doc_meta(text, Path(rel).stem)
            except OSError:
                title, snippet = Path(rel).stem, ""
            g.add((node, DCTERMS.title, Literal(title[:300])))
            if snippet:
                g.add((node, KG.detail, Literal(snippet)))
        stats["files"] += 1

    # --- commits (capped, logged) ---
    fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s"
    log_args = ["log", f"--pretty=format:{fmt}"]
    if max_commits is not None:
        log_args.insert(1, f"-n{max_commits}")
    for line in _git(repo_path, *log_args).splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, aname, aemail, adate, subject = parts
        c = iris.commit(repo_slug, sha)
        g.add((c, RDF.type, KG.Commit))
        g.add((c, KG.sourceId, Literal(sha)))
        g.add((c, DCTERMS.title, Literal(subject[:300])))
        g.add((c, DCTERMS.created, Literal(adate, datatype=XSD.dateTime)))
        g.add((c, KG.partOf, repo_iri))
        login = aname or aemail
        p = iris.person(login)
        g.add((p, RDF.type, KG.BotAgent if _is_bot(login) else KG.Person))
        g.add((p, KG.login, Literal(login)))
        g.add((p, RDFS.label, Literal(aname or login)))
        g.add((c, PROV.wasAttributedTo, p))
        _link_tracker(g, c, subject, stats)
        people.add(login)
        stats["commits"] += 1
        # History range for the refresh stats. Author dates carry their own offsets, so compare parsed values.
        try:
            when = datetime.fromisoformat(adate)
        except ValueError:
            when = None
        if when is not None:
            if "_first_commit" not in stats or when < stats["_first_commit"]:
                stats["_first_commit"] = when
                stats["first_commit_date"] = adate
            if "_last_commit" not in stats or when > stats["_last_commit"]:
                stats["_last_commit"] = when
                stats["last_commit_date"] = adate

    # --- pull requests (via gh REST API, paginated) ---
    if max_prs:
        _pr_run_id = iris.stable_run_id(pipeline_ver, repo_slug + ":pr-comments")
        _pr_run = _pr_comment_run_node(g, _pr_run_id, pipeline_ver)
        _pr_errors: list[str] = []
        try:
            raw = _gh_api(
                ["api", "--paginate",
                 f"repos/{repo_slug}/pulls?state=all&per_page=100"],
                cwd=str(repo_path),
            )
            all_prs, pr_pages = _parse_paginated_json(raw)
            stats["prs_total"] = len(all_prs)
            stats["pr_pages_fetched"] = pr_pages
            prs_to_ingest = all_prs[:max_prs]
            omitted = len(all_prs) - len(prs_to_ingest)
            if omitted > 0:
                stats["prs_omitted_reason"] = f"capped at {max_prs} (fetched {len(all_prs)} total)"

            pr_numbers_to_fetch = [pr["number"] for pr in prs_to_ingest]
            pr_details = _fetch_pr_details_batch(
                repo_path, repo_slug, pr_numbers_to_fetch, _pr_errors
            )

            _empty_detail: dict = {
                "comments": [], "review_comments": [], "reviews": [], "thread_states": {}
            }

            for pr in prs_to_ingest:
                pnode = iris.pr(repo_slug, pr["number"])
                g.add((pnode, RDF.type, KG.PullRequest))
                g.add((pnode, KG.number, Literal(pr["number"], datatype=XSD.integer)))
                g.add((pnode, DCTERMS.title, Literal(pr.get("title") or "")))

                # REST API: state "open"/"closed"; merged PRs are "closed" with merged_at set
                merged_at = pr.get("merged_at")
                if merged_at:
                    g.add((pnode, KG.state, Literal("merged")))
                    g.add((pnode, KG.mergedAt, Literal(merged_at, datatype=XSD.dateTime)))
                else:
                    g.add((pnode, KG.state, Literal(pr.get("state") or "")))

                # base branch (REST: base.ref)
                base_ref = (pr.get("base") or {}).get("ref") or ""
                if base_ref:
                    g.add((pnode, KG.baseRefName, Literal(base_ref)))

                # link merged PR -> merge commit when that commit is in the graph
                merge_sha = pr.get("merge_commit_sha")
                if merge_sha and merged_at:
                    commit_iri = iris.commit(repo_slug, merge_sha)
                    if next(g.triples((commit_iri, None, None)), None) is not None:
                        g.add((pnode, KG.mergeCommit, commit_iri))

                g.add((pnode, KG.partOf, repo_iri))
                login = (pr.get("user") or {}).get("login") or "unknown"
                p = iris.person(login)
                g.add((p, RDF.type, KG.BotAgent if _is_bot(login) else KG.Person))
                g.add((p, KG.login, Literal(login)))
                g.add((pnode, PROV.wasAttributedTo, p))
                _link_tracker(g, pnode, f"{pr.get('title') or ''} {pr.get('body') or ''}", stats)
                people.add(login)
                stats["prs"] += 1

                detail = pr_details.get(pr["number"], _empty_detail)
                _add_pr_comments(
                    g, _pr_run, repo_slug, pr["number"], pnode, detail["comments"], stats
                )
                _add_pr_review_content(
                    g, _pr_run, repo_slug, pr["number"], pnode,
                    detail["review_comments"], detail["reviews"], detail["thread_states"], stats,
                )

            stats["prs_ingested"] = stats["prs"]
            _has_rate_limit_error = any(
                "rate limit" in e.lower() or "abuse" in e.lower() for e in _pr_errors
            )
            if _pr_errors:
                stats["pr_comment_errors"] = (
                    f"{len(_pr_errors)} (first: {_pr_errors[0][:120]})"
                )
            if _pr_errors and stats["prs"] > 0:
                if _has_rate_limit_error or len(_pr_errors) / stats["prs"] > 0.10:
                    stats["people"] = len(people)
                    raise PRCommentsIncomplete(stats)

        except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as e:
            stats["pr_error"] = str(e)[:200]

    stats["people"] = len(people)
    stats.pop("_first_commit", None)
    stats.pop("_last_commit", None)
    return stats
