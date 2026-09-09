"""Spine ingester — deterministic, GitHub/git-derivable, zero LLM (recon §1.1/§5.3).

Reads local git (repo, files, commits, people) and optionally GitHub PRs via `gh`,
emitting RDF into the spine named graph. Re-derivable: same repo state -> same triples.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD, Namespace

from . import iris, normalize
from .tracker import _classify_pr_comment, _first_line

KG = iris.KG
PROV = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")

DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt"}

_MAX_PR_COMMENTS = 50

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
    l = login.lower()
    return l.endswith("[bot]") or l.endswith("-bot") or "bot]" in l


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


def _bind(g: Graph) -> None:
    g.bind("kg", KG); g.bind("kgr", iris.KGR); g.bind("prov", PROV)
    g.bind("dcterms", DCTERMS); g.bind("foaf", FOAF)


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
              pipeline_ver: str = "0.1.0") -> dict:
    """Populate `g` with spine triples. Returns a stats dict (with any caps applied)."""
    _bind(g)
    repo_path = Path(repo_path)
    stats = {"files": 0, "commits": 0, "people": 0, "prs": 0, "pr_comments": 0,
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

    # --- files ---
    for rel in _git(repo_path, "ls-files").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        suffix = Path(rel).suffix.lower()
        is_doc = suffix in DOC_SUFFIXES
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

    # --- pull requests (via gh, capped) ---
    if max_prs:
        _pr_run_id = iris.stable_run_id(pipeline_ver, repo_slug + ":pr-comments")
        _pr_run = _pr_comment_run_node(g, _pr_run_id, pipeline_ver)
        try:
            raw = subprocess.run(
                ["gh", "pr", "list", "--repo", repo_slug, "--state", "all",
                 "--limit", str(max_prs), "--json",
                 "number,title,author,state,mergedAt,body"],
                capture_output=True, text=True, check=True, cwd=str(repo_path),
            ).stdout
            for pr in json.loads(raw):
                pnode = iris.pr(repo_slug, pr["number"])
                g.add((pnode, RDF.type, KG.PullRequest))
                g.add((pnode, KG.number, Literal(pr["number"], datatype=XSD.integer)))
                g.add((pnode, DCTERMS.title, Literal(pr.get("title") or "")))
                g.add((pnode, KG.state, Literal(pr.get("state") or "")))
                g.add((pnode, KG.partOf, repo_iri))
                login = (pr.get("author") or {}).get("login") or "unknown"
                p = iris.person(login)
                g.add((p, RDF.type, KG.BotAgent if _is_bot(login) else KG.Person))
                g.add((p, KG.login, Literal(login)))
                g.add((pnode, PROV.wasAttributedTo, p))
                _link_tracker(g, pnode, f"{pr.get('title') or ''} {pr.get('body') or ''}", stats)
                people.add(login)
                stats["prs"] += 1
                try:
                    raw_c = subprocess.run(
                        ["gh", "api", "--paginate",
                         f"repos/{repo_slug}/issues/{pr['number']}/comments?per_page=50"],
                        capture_output=True, text=True, check=True, cwd=str(repo_path),
                    ).stdout
                    comments = json.loads(raw_c)[:_MAX_PR_COMMENTS]
                except (subprocess.CalledProcessError, json.JSONDecodeError,
                        FileNotFoundError) as ce:
                    comments = []
                    stats.setdefault("pr_comment_errors", []).append(str(ce)[:200])
                _add_pr_comments(g, _pr_run, repo_slug, pr["number"], pnode, comments, stats)
        except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError) as e:
            stats["pr_error"] = str(e)[:200]

    stats["people"] = len(people)
    return stats
