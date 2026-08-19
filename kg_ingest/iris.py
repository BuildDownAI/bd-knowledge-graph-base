"""Deterministic IRI minting for the knowledge graph.

Every entity gets a stable, namespaced IRI so re-ingest is idempotent and
learning-id collisions across repos/stores are impossible (recon §2.3/§5.7).
"""
from __future__ import annotations

import hashlib
from urllib.parse import quote

from rdflib import Namespace, URIRef

from . import sources

# The IRI namespace is per-deployment configuration (sources.yml `namespace:`),
# so a KG's identifiers carry its owner's domain, not this template's. The
# neutral default keeps a fresh clone fully functional before configuration.
DEFAULT_NAMESPACE = "https://example.org/kg/"


def _namespace() -> str:
    try:
        ns = str(sources.load().get("namespace") or DEFAULT_NAMESPACE)
    except FileNotFoundError:
        ns = DEFAULT_NAMESPACE
    return ns if ns.endswith("/") else ns + "/"


NAMESPACE = _namespace()
KG = Namespace(f"{NAMESPACE}onto#")
KGR = Namespace(f"{NAMESPACE}resource/")

# graph (context) IRIs
G_SPINE = URIRef(f"{NAMESPACE}resource/graph/spine")


def _seg(s: str) -> str:
    """URL-safe path segment (keeps IRIs readable but valid)."""
    return quote(str(s), safe="")


def run_graph(run_id: str) -> URIRef:
    return URIRef(f"{KGR}graph/run/{_seg(run_id)}")


def repo(slug: str) -> URIRef:                    # slug = "owner/name"
    return URIRef(f"{KGR}repo/{_seg(slug)}")


def commit(repo_slug: str, sha: str) -> URIRef:
    return URIRef(f"{KGR}commit/{_seg(repo_slug)}/{sha}")


def file(repo_slug: str, path: str) -> URIRef:
    return URIRef(f"{KGR}file/{_seg(repo_slug)}/{_seg(path)}")


def doc(repo_slug: str, path: str) -> URIRef:
    return URIRef(f"{KGR}doc/{_seg(repo_slug)}/{_seg(path)}")


def pr(repo_slug: str, number: int | str) -> URIRef:
    return URIRef(f"{KGR}pr/{_seg(repo_slug)}/{number}")


def issue(repo_slug: str, number: int | str) -> URIRef:
    # GitHub-style numeric issue (repo-scoped).
    return URIRef(f"{KGR}issue/{_seg(repo_slug)}/{number}")


def tracker_issue(identifier: str) -> URIRef:
    # Tracker issue keyed by its stable global identifier (e.g. "PROJ-123"). Repo-independent so the spine (PR/commit references) and the
    # tracker ingester converge on the SAME node — this is how the graph
    # "closes the loop to code" (§2.7).
    return URIRef(f"{KGR}issue/{_seg(identifier)}")


def comment(issue_identifier: str, ordinal: int) -> URIRef:
    return URIRef(f"{KGR}comment/{_seg(issue_identifier)}/{ordinal}")


def project(name: str) -> URIRef:
    return URIRef(f"{KGR}project/{_seg(name)}")


def person(login: str) -> URIRef:
    return URIRef(f"{KGR}person/{_seg(login)}")


def learning(repo_slug: str, store: str, lid: str) -> URIRef:
    # namespaced per repo AND per store -> the L-0300-in-two-stores collision
    # (recon §2.3: solutions L-0300 != auto-run L-0300) cannot happen.
    return URIRef(f"{KGR}learning/{_seg(repo_slug)}/{_seg(store)}/{_seg(lid)}")


def topic(slug: str) -> URIRef:
    return URIRef(f"{KGR}topic/{_seg(slug)}")


def status_update(learning_iri: URIRef, ordinal: int) -> URIRef:
    return URIRef(f"{learning_iri}/status/{ordinal}")


def chunk(node_iri: URIRef, ordinal: int) -> URIRef:
    return URIRef(f"{node_iri}/chunk/{ordinal}")


def doc_site(root_url: str) -> URIRef:
    return URIRef(f"{KGR}docsite/{_seg(root_url)}")


def doc_page(url: str) -> URIRef:
    return URIRef(f"{KGR}docpage/{_seg(url)}")


def doc_section(url: str, anchor: str) -> "URIRef | None":
    """Return the DocSection IRI for url#anchor, or None for empty anchor (preamble)."""
    if not anchor:
        return None
    return URIRef(f"{KGR}docpage/{_seg(url)}#{anchor}")


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_run_id(pipeline_ver: str, corpus_fingerprint: str) -> str:
    """Deterministic run id: same code + same inputs -> same run graph IRI."""
    h = hashlib.sha256(f"{pipeline_ver}\n{corpus_fingerprint}".encode()).hexdigest()
    return h[:16]
