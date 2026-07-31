"""Tracker ingester — Linear issues as a THIRD spine source (runbook §2.7).

A company's tracker holds work for many repos; we ingest only the team(s) bound
to this repo in sources.yml. Two layers, same as the rest of the graph:

  * SPINE  (deterministic, from the Linear API) -> kg:Issue / kg:Project nodes,
    their status/labels/branch, and the issue<->issue graph (parent, blocks,
    related). Idempotent on Linear's stable identifiers (e.g. PROJ-123).
  * SEMANTIC (provenanced) -> a Learning or Decision node for each substantive
    comment (the "build-up / build-down learnings" and human decision rationale
    are the gold here), each wasDerivedFrom its Issue and wasGeneratedBy the run.

The loop to code is closed on BOTH ends: the spine reference-scanner already
emits `<pr|commit> kg:references <issue/PROJ-NNN>` when a PR/commit mentions the
key; here we flesh out that same node and add its tracker edges.

Auth: LINEAR_API_KEY in the environment (a Linear personal API key). No key ->
the ingester raises, so a tracker run never silently produces an empty graph.
"""
from __future__ import annotations

import os
import re

import requests
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, SKOS, XSD, Namespace

from . import iris, normalize, sources

KG = iris.KG
PROV = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")

LINEAR_URL = "https://api.linear.app/graphql"

# A comment is treated as a Learning when it carries a build-up/down learnings
# marker; as a Decision when it is substantial human rationale; else skipped.
_LEARNING_MARKERS = (
    "ai-implement-build-up-learnings",
    "ai-implement-build-down-learnings",
    "build-down-learnings",
    "build-up-learnings",
)
_DECISION_MIN_CHARS = 400

# Linear workflow-STATE labels the orchestrator uses to drive the pipeline
# (the AI-Implement pipeline lifecycle: AI-Implement -> AI-Planning -> Plan-Complete -> AI-Working ->
# Ready for Review). These are process state, not subject matter — they swamp
# the real topics, so we keep them as kg:label literals but do NOT promote them
# to kg:tagged topics. Edit this set to tune what counts as "process noise".
_LIFECYCLE_LABELS = {
    "ai-implement", "ai-planning", "ai-working",
    "plan-complete", "ready for review",
}

_ISSUES_QUERY = """
query($team: String!, $after: String) {
  issues(first: 100, after: $after,
         filter: { team: { key: { eq: $team } } }) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier title description branchName
      state { name type }
      labels { nodes { name } }
      project { name }
      parent { identifier }
      comments(first: 50) { nodes { body user { name } } }
      relations { nodes { type relatedIssue { identifier } } }
    }
  }
}
"""


def _bind(g: Graph) -> None:
    g.bind("kg", KG); g.bind("kgr", iris.KGR); g.bind("prov", PROV)
    g.bind("dcterms", DCTERMS); g.bind("skos", SKOS)


def _run_node(g: Graph, run_id: str, pipeline_ver: str) -> URIRef:
    r = URIRef(f"{iris.KGR}run/{run_id}")
    g.add((r, RDF.type, KG.ExtractionRun))
    g.add((r, KG.pipelineVer, Literal(pipeline_ver)))
    g.add((r, KG.model, Literal("deterministic-tracker")))
    g.add((r, KG.promptHash, Literal("n/a-deterministic")))
    g.add((r, KG.notes, Literal("Phase-1 deterministic Linear ingest (§2.7)")))
    return r


def _post(query: str, variables: dict, api_key: str) -> dict:
    resp = requests.post(
        LINEAR_URL, json={"query": query, "variables": variables},
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        timeout=45,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"Linear GraphQL error: {payload['errors']}")
    return payload["data"]


def _fetch_team_issues(team: str, api_key: str) -> list[dict]:
    out, after = [], None
    while True:
        data = _post(_ISSUES_QUERY, {"team": team, "after": after}, api_key)
        conn = data["issues"]
        out.extend(conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            return out
        after = conn["pageInfo"]["endCursor"]


def _topic(g: Graph, tag: str) -> URIRef:
    t = iris.topic(tag)
    g.add((t, RDF.type, KG.Topic))
    g.add((t, SKOS.prefLabel, Literal(tag)))
    return t


def _first_line(body: str) -> str:
    for line in body.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s
    return ""


def _classify(body: str) -> str | None:
    # A learning marker only counts on the FIRST line — the bd-build-up/down
    # skills lead the comment with `# ai-implement-build-{up,down}-learnings`.
    # Matching the marker anywhere in the body over-catches planning/summary
    # comments that merely mention it (keeps _classify aligned with _comment_title).
    first = _first_line(body).lower()
    if any(m in first for m in _LEARNING_MARKERS):
        return "learning"
    if len(body.strip()) >= _DECISION_MIN_CHARS:
        return "decision"
    return None


def _comment_title(ident: str, issue_title: str, body: str, kind: str) -> str:
    """Readable, key-prefixed title so kg_search surfaces the right comment.

    Build-up/down learning comments lead with a marker heading that makes a poor
    title on its own; replace it with "<KEY> build-{up,down} learnings — <issue>".
    """
    fl = _first_line(body)
    low = fl.lower()
    if kind == "learning" and any(m in low for m in _LEARNING_MARKERS):
        phase = ("build-down learnings" if "build-down" in low
                 else "build-up learnings" if "build-up" in low
                 else "learnings")
        tail = f" — {issue_title}" if issue_title else ""
        return f"{ident} {phase}{tail}"
    return f"{ident}: {fl}" if fl else f"{ident} note"


def _add_issue(spine_g: Graph, run_g: Graph, run: URIRef, team: str,
               iss: dict, stats: dict) -> None:
    ident = iss["identifier"]
    node = iris.tracker_issue(ident)
    spine_g.add((node, RDF.type, KG.Issue))
    spine_g.add((node, KG.trackerKey, Literal(ident)))
    spine_g.add((node, KG.trackerTeam, Literal(team)))
    spine_g.add((node, KG.sourceSystem, Literal("linear")))
    spine_g.add((node, DCTERMS.title, Literal((iss.get("title") or ident)[:400])))
    spine_g.add((node, RDFS.label, Literal(ident)))
    if iss.get("description"):
        spine_g.add((node, KG.description, Literal(iss["description"][:4000])))
    if iss.get("branchName"):
        spine_g.add((node, KG.branchName, Literal(iss["branchName"])))
    st = (iss.get("state") or {})
    if st.get("name"):
        spine_g.add((node, KG.state, Literal(st["name"])))
        spine_g.add((node, KG.status, Literal(normalize.normalize_status(st["name"])
                                              or st["name"].lower())))
    # labels -> raw literal always; topic only for non-lifecycle labels
    # (so kg_neighbors(topic) finds related issues by subject, not by pipeline state)
    for lbl in (iss.get("labels") or {}).get("nodes", []):
        name = lbl.get("name")
        if name:
            spine_g.add((node, KG.label, Literal(name)))
            if name.lower() not in _LIFECYCLE_LABELS:
                spine_g.add((node, KG.tagged, _topic(spine_g, name.lower())))
    # project
    proj = (iss.get("project") or {})
    if proj.get("name"):
        pnode = iris.project(proj["name"])
        spine_g.add((pnode, RDF.type, KG.Project))
        spine_g.add((pnode, DCTERMS.title, Literal(proj["name"])))
        spine_g.add((node, KG.inProject, pnode))
    # parent -> partOf
    parent = (iss.get("parent") or {})
    if parent.get("identifier"):
        pi = iris.tracker_issue(parent["identifier"])
        spine_g.add((pi, RDF.type, KG.Issue))
        spine_g.add((node, KG.partOf, pi))
    # relations: blocks -> dependsOn (reverse), duplicate -> duplicateOf, else relatedTo
    for rel in (iss.get("relations") or {}).get("nodes", []):
        other = (rel.get("relatedIssue") or {}).get("identifier")
        if not other:
            continue
        oi = iris.tracker_issue(other)
        spine_g.add((oi, RDF.type, KG.Issue))
        rtype = (rel.get("type") or "").lower()
        if rtype == "blocks":
            spine_g.add((oi, KG.dependsOn, node))      # other depends on this
        elif rtype == "duplicate":
            spine_g.add((node, KG.duplicateOf, oi))
        else:
            spine_g.add((node, KG.relatedTo, oi))
    stats["issues"] += 1

    # ---- comments -> semantic layer (Learning / Decision) ----
    label_tags = [l["name"].lower() for l in (iss.get("labels") or {}).get("nodes", [])
                  if l.get("name") and l["name"].lower() not in _LIFECYCLE_LABELS]
    for i, cm in enumerate(((iss.get("comments") or {}).get("nodes", []))):
        body = (cm.get("body") or "").strip()
        kind = _classify(body)
        if not kind:
            continue
        cnode = iris.comment(ident, i)
        cls = KG.Learning if kind == "learning" else KG.Decision
        run_g.add((cnode, RDF.type, cls))
        title = _comment_title(ident, iss.get("title") or "", body, kind)
        run_g.add((cnode, DCTERMS.title, Literal(title[:300], datatype=XSD.string)))
        run_g.add((cnode, KG.detail, Literal(body[:2000])))
        if kind == "learning":
            run_g.add((cnode, KG.fix, Literal(body[:400])))  # snippet for kg_search
        # mandatory provenance: derived from the Issue (spine), generated by run
        run_g.add((cnode, PROV.wasDerivedFrom, node))
        run_g.add((cnode, PROV.wasGeneratedBy, run))
        author = (cm.get("user") or {}).get("name")
        if author:
            person = iris.person(author)
            run_g.add((person, RDF.type, KG.Person))
            run_g.add((person, KG.login, Literal(author)))
            run_g.add((cnode, PROV.wasAttributedTo, person))
        for tag in label_tags:
            run_g.add((cnode, KG.tagged, _topic(run_g, tag)))
        stats["comment_learnings" if kind == "learning" else "comment_decisions"] += 1


def add_tracker(spine_g: Graph, run_g: Graph, repo_slug: str, run_id: str,
                pipeline_ver: str = "0.1.0", cfg: dict | None = None,
                include_secondary: bool = True) -> dict:
    """Ingest configured Linear teams into spine_g (issues) + run_g (learnings).

    Reads which teams to pull from sources.yml. Primary teams always run;
    secondary teams run when include_secondary is True.
    """
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        raise RuntimeError(
            "LINEAR_API_KEY not set — tracker ingest needs a Linear API key "
            "(the graph would otherwise be silently empty). `source .env` first.")
    _bind(spine_g); _bind(run_g)
    run = _run_node(run_g, run_id, pipeline_ver)
    stats = {"teams": [], "issues": 0, "comment_learnings": 0,
             "comment_decisions": 0}
    for entry in sources.trackers(cfg):
        if entry.get("kind") != "linear":
            continue
        tier = entry.get("tier") or "primary"
        if tier == "secondary" and not include_secondary:
            continue
        team = entry["team"]
        issues = _fetch_team_issues(team, api_key)
        for iss in issues:
            _add_issue(spine_g, run_g, run, team, iss, stats)
        stats["teams"].append(f"{team}({tier}):{len(issues)}")
    return stats
