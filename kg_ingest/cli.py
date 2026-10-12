"""Build the graph, serialize named graphs (TriG), SHACL-validate, demo SPARQL.

Usage:
  python -m kg_ingest.cli --repo /path/to/your-project [--max-commits 0] [--max-prs 200]
                          [--tracker]   # also ingest Linear issues (needs LINEAR_API_KEY)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from datetime import datetime, timezone

from rdflib import Dataset, Graph, Literal
from rdflib.namespace import DCTERMS, RDF, XSD
from pyshacl import validate

from . import iris, ontology, spine, semantic, snapshot

ONTO_DIR = Path(__file__).resolve().parent.parent / "ontology"
OUT_DIR = Path(__file__).resolve().parent.parent / "out"
SNAP_DIR = Path(__file__).resolve().parent.parent / "snapshot"


def _heading_anchor(heading: str) -> str:
    """Convert a heading string to a URL-safe anchor slug."""
    s = heading.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s.strip())
    return re.sub(r"-+", "-", s).strip("-")


def _ingest_docs_sites(
    spine_g: Graph,
    docs_sites_cfg: list,
    prior_g: Graph | None = None,
) -> dict:
    """Crawl docs_sites entries and emit DocSite/DocPage/DocSection triples into spine_g.

    prior_g: prior spine graph used for incremental refresh.  Pages whose
    content_hash is unchanged have their DocSection triples copied from prior_g
    instead of being re-extracted (skip re-chunking).  Reports pages fetched and
    changed count per site.
    """
    from .docsite import crawl_site, CrawlConfig

    KG = iris.KG
    total_pages = 0
    total_sections = 0

    for entry in docs_sites_cfg:
        root_url = (entry.get("url") or "").rstrip("/")
        if not root_url:
            continue

        site_iri = iris.doc_site(root_url)

        # Build prior hash map {page_url: content_hash} for incremental refresh
        prior_hashes: dict[str, str] | None = None
        if prior_g is not None:
            prior_hashes = {}
            for page_iri_p, _, hash_lit in prior_g.triples((None, KG.contentHash, None)):
                if (page_iri_p, KG.partOf, site_iri) in prior_g:
                    url_lit = prior_g.value(page_iri_p, KG.url)
                    if url_lit:
                        prior_hashes[str(url_lit)] = str(hash_lit)

        config = CrawlConfig(
            root_url=root_url,
            max_depth=entry.get("max_depth", 4),
            max_pages=entry.get("max_pages", 300),
            include=list(entry.get("include") or []),
            exclude=list(entry.get("exclude") or []),
            delay=entry.get("delay", 0.5),
            user_agent=entry.get("user_agent", "KGB-DocBot/1.0"),
        )

        print(f"== docsite crawl: {root_url} ==")
        pages = crawl_site(config, prior_hashes=prior_hashes)
        changed_count = sum(
            1 for p in pages
            if prior_hashes is None or prior_hashes.get(p.url) != p.content_hash
        )

        spine_g.add((site_iri, RDF.type, KG.DocSite))
        spine_g.add((site_iri, KG.rootUrl, Literal(root_url)))
        spine_g.add((site_iri, DCTERMS.title, Literal(root_url)))
        documents_branch = entry.get("documents_branch")
        if documents_branch:
            spine_g.add((site_iri, KG.documentsBranch, Literal(str(documents_branch))))

        repo_slug = entry.get("repo")
        if repo_slug:
            repo_iri = iris.repo(repo_slug)
            spine_g.add((repo_iri, KG.docsUrl, Literal(root_url)))

        fetched_ats: list[str] = []
        for page in pages:
            is_unchanged = (
                prior_hashes is not None
                and prior_hashes.get(page.url) == page.content_hash
            )

            # Anchor slugs must be unique per page: repeated headings ("Required",
            # "Required") would otherwise collide onto ONE DocSection IRI, merging
            # two sections' triples and double-emitting cards. Site generators
            # (Mintlify, GitHub) dedupe the same way: -2, -3, ... suffixes.
            _seen_anchors: dict[str, int] = {}
            page_iri = iris.doc_page(page.url)
            spine_g.add((page_iri, RDF.type, KG.DocPage))
            spine_g.add((page_iri, KG.url, Literal(page.url)))
            spine_g.add((page_iri, KG.contentHash, Literal(page.content_hash)))
            spine_g.add((page_iri, DCTERMS.modified,
                         Literal(page.fetched_at, datatype=XSD.dateTime)))
            spine_g.add((page_iri, KG.partOf, site_iri))
            fetched_ats.append(page.fetched_at)

            if is_unchanged and prior_g is not None:
                # Unchanged page: restore title and sections from prior graph
                prior_title = prior_g.value(page_iri, DCTERMS.title)
                spine_g.add((page_iri, DCTERMS.title, prior_title or Literal(page.url)))
                prior_detail = prior_g.value(page_iri, KG.detail)
                if prior_detail:
                    spine_g.add((page_iri, KG.detail, prior_detail))
                # Copy DocSection triples (skip re-chunking)
                for sec_iri, _, _ in prior_g.triples((None, KG.partOf, page_iri)):
                    for triple in prior_g.triples((sec_iri, None, None)):
                        spine_g.add(triple)
                    total_sections += 1
            else:
                # New or changed page: extract and emit sections
                spine_g.add((page_iri, DCTERMS.title, Literal(page.title)))
                preamble_parts: list[str] = []
                for section in page.sections:
                    if section.level == 0:
                        if section.text:
                            preamble_parts.append(section.text)
                        continue

                    anchor = _heading_anchor(section.heading)
                    if anchor:
                        n = _seen_anchors.get(anchor, 0) + 1
                        _seen_anchors[anchor] = n
                        if n > 1:
                            anchor = f"{anchor}-{n}"
                    sec_iri = iris.doc_section(page.url, anchor)
                    if sec_iri is None:
                        continue

                    spine_g.add((sec_iri, RDF.type, KG.DocSection))
                    spine_g.add((sec_iri, KG.heading, Literal(section.heading)))
                    spine_g.add((sec_iri, KG.level,
                                 Literal(section.level, datatype=XSD.integer)))
                    spine_g.add((sec_iri, KG.anchor, Literal(anchor)))
                    spine_g.add((sec_iri, KG.text, Literal(section.text)))
                    spine_g.add((sec_iri, KG.partOf, page_iri))
                    total_sections += 1

                if preamble_parts:
                    spine_g.add((page_iri, KG.detail, Literal(" ".join(preamble_parts))))

            total_pages += 1

        site_fetched = (max(fetched_ats) if fetched_ats
                        else datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        spine_g.add((site_iri, DCTERMS.modified,
                     Literal(site_fetched, datatype=XSD.dateTime)))
        print(f"   pages: {total_pages} fetched, {changed_count} changed")

    return {"docsite_pages": total_pages, "docsite_sections": total_sections}


def _ingest_mcp_sources(spine_g: Graph, mcp_sources_cfg: list) -> dict:
    """Ingest mcp_sources entries and emit McpSource/DocPage/DocSection triples."""
    from .mcp_source import McpSourceConfig, fetch_resources

    KG = iris.KG
    total_pages = 0
    total_sections = 0

    for entry in mcp_sources_cfg:
        name = (entry.get("name") or "").strip()
        command = list(entry.get("command") or [])
        max_resources = int(entry.get("max_resources", 200))

        if not name:
            continue

        config = McpSourceConfig(
            name=name,
            command=command or None,
            max_resources=max_resources,
        )

        print(f"== mcp_source ingest: {name} ==")
        pages = fetch_resources(config)

        source_iri = iris.mcp_source(name)
        spine_g.add((source_iri, RDF.type, KG.McpSource))
        spine_g.add((source_iri, KG.mcpServer, Literal(name)))
        spine_g.add((source_iri, DCTERMS.title, Literal(name)))

        fetched_ats: list[str] = []
        for page in pages:
            _seen_anchors: dict[str, int] = {}
            page_iri = iris.mcp_page(name, page.uri)
            spine_g.add((page_iri, RDF.type, KG.DocPage))
            spine_g.add((page_iri, KG.url, Literal(page.uri)))
            spine_g.add((page_iri, DCTERMS.title, Literal(page.title)))
            spine_g.add((page_iri, KG.contentHash, Literal(page.content_hash)))
            spine_g.add((page_iri, DCTERMS.modified,
                         Literal(page.fetched_at, datatype=XSD.dateTime)))
            spine_g.add((page_iri, KG.partOf, source_iri))
            fetched_ats.append(page.fetched_at)

            preamble_parts: list[str] = []
            for section in page.sections:
                if section.level == 0:
                    if section.text:
                        preamble_parts.append(section.text)
                    continue

                anchor = _heading_anchor(section.heading)
                if anchor:
                    n = _seen_anchors.get(anchor, 0) + 1
                    _seen_anchors[anchor] = n
                    if n > 1:
                        anchor = f"{anchor}-{n}"
                sec_iri = iris.mcp_section(name, page.uri, anchor)
                if sec_iri is None:
                    continue

                spine_g.add((sec_iri, RDF.type, KG.DocSection))
                spine_g.add((sec_iri, KG.heading, Literal(section.heading)))
                spine_g.add((sec_iri, KG.level,
                             Literal(section.level, datatype=XSD.integer)))
                spine_g.add((sec_iri, KG.anchor, Literal(anchor)))
                spine_g.add((sec_iri, KG.text, Literal(section.text)))
                spine_g.add((sec_iri, KG.partOf, page_iri))
                total_sections += 1

            if preamble_parts:
                spine_g.add((page_iri, KG.detail, Literal(" ".join(preamble_parts))))

            total_pages += 1

        source_fetched = (max(fetched_ats) if fetched_ats
                          else datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        spine_g.add((source_iri, DCTERMS.modified,
                     Literal(source_fetched, datatype=XSD.dateTime)))
        print(f"   pages: {total_pages}, sections: {total_sections}")

    return {"mcp_pages": total_pages, "mcp_sections": total_sections}


def secondary_repo_path(entry: dict, repos_root: str | None) -> Path:
    """Where a sources.yml secondary_repos entry is cloned.

    With --repos-root, the clone lives at <root>/<basename(slug)> — the layout the
    AI-Implement kg-refresh pipeline produces. Without it, the entry's own path,
    relative to the KG repo root (this repo).
    """
    if repos_root:
        return (Path(repos_root) / Path(entry["slug"]).name).resolve()
    p = Path(entry["path"])
    return p if p.is_absolute() else (OUT_DIR.parent / p).resolve()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="path to the source repo to ingest")
    ap.add_argument("--repo-slug", default=None,
                    help="owner/name for the source repo (default: sources.yml code_repo.slug)")
    ap.add_argument("--max-commits", type=int, default=0,
                    help="cap commit ingest (0 = all)")
    ap.add_argument("--max-prs", type=int, default=200,
                    help="cap PR ingest (0 = skip PRs)")
    ap.add_argument("--tracker", action="store_true",
                    help="also ingest tracker (Linear) issues from sources.yml "
                         "(requires LINEAR_API_KEY in the environment)")
    ap.add_argument("--tracker-data", default=None, metavar="PATH",
                    help="path to a pipeline-fetched tracker-data.json (JSON array of "
                         "issues); implies --tracker, no LINEAR_API_KEY needed; "
                         "if LINEAR_API_KEY is also set the file wins")
    ap.add_argument("--repos-root", default=None, metavar="DIR",
                    help="directory holding clones of sources.yml secondary_repos as "
                         "<DIR>/<basename(slug)> (the AI-Implement kg-refresh layout); "
                         "implies --secondary and replaces each entry's relative path")
    ap.add_argument("--secondary", action="store_true",
                    help="also ingest secondary_repos from sources.yml (e.g. skills) "
                         "into the same graph")
    # Rebuilding the semantic embeddings sidecar is part of every refresh by
    # default (so it never goes stale relative to the graph). It is skipped
    # gracefully when fastembed isn't installed, so the core ingest stays
    # dependency-light. --no-embed opts out; --embed is kept as a no-op alias.
    ap.add_argument("--embed", dest="embed", action="store_true", default=True,
                    help="rebuild the semantic-search embedding sidecar (the default)")
    ap.add_argument("--no-embed", dest="embed", action="store_false",
                    help="skip rebuilding the embedding sidecar "
                         "(e.g. fastembed not installed / offline)")
    ap.add_argument("--no-docs-sites", dest="docs_sites", action="store_false", default=True,
                    help="skip the docs_sites crawl (e.g. offline / CI unit tests)")
    ap.add_argument("--pipeline-ver", default="0.1.0")
    args = ap.parse_args(argv)
    from . import sources as _sources
    _src_cfg = _sources.load()
    if not args.repo_slug:
        args.repo_slug = (_src_cfg.get("code_repo") or {}).get("slug") or "local/repo"

    repo_path = Path(args.repo).resolve()
    OUT_DIR.mkdir(exist_ok=True)

    ds = Dataset()
    spine_g = ds.graph(iris.G_SPINE)
    max_commits = None if args.max_commits == 0 else args.max_commits

    _code_repo_cfg = _src_cfg.get("code_repo") or {}
    print("== spine ingest ==")
    try:
        s_stats = spine.add_spine(spine_g, repo_path, args.repo_slug,
                                  max_commits=max_commits, max_prs=args.max_prs,
                                  docs_url=_code_repo_cfg.get("docs_url"),
                                  doc_exclude=_code_repo_cfg.get("doc_exclude"))
        for k, v in s_stats.items():
            print(f"   {k}: {v}")
    except spine.PRCommentsIncomplete as exc:
        for k, v in exc.stats.items():
            print(f"   {k}: {v}")
        print("KG_PR_COMMENTS_INCOMPLETE")
        return 1

    # ---- docs_sites: crawl published documentation into the spine graph ----
    # Load prior spine graph for incremental refresh (skip re-chunking unchanged pages)
    _prior_spine_g: Graph | None = None
    _prior_trig = OUT_DIR / "graph.trig"
    if _prior_trig.exists():
        try:
            _prior_ds = Dataset()
            _prior_ds.parse(str(_prior_trig), format="trig")
            _prior_spine_g = _prior_ds.graph(iris.G_SPINE)
        except Exception:
            pass

    _docs_sites = _src_cfg.get("docs_sites") or []
    if not args.docs_sites:
        print("docs_sites: skipped (--no-docs-sites)")
    elif _docs_sites:
        print("== docs_sites ingest ==")
        ds_stats = _ingest_docs_sites(spine_g, _docs_sites, prior_g=_prior_spine_g)
        for k, v in ds_stats.items():
            print(f"   {k}: {v}")

    # ---- mcp_sources: ingest MCP-served resources into the spine graph ----
    _mcp_sources = _src_cfg.get("mcp_sources") or []
    if _mcp_sources:
        print("== mcp_sources ingest ==")
        mcp_stats = _ingest_mcp_sources(spine_g, _mcp_sources)
        for k, v in mcp_stats.items():
            print(f"   {k}: {v}")

    # deterministic run id from pipeline ver + corpus fingerprint
    fingerprint = iris.content_hash(
        str(sorted(p.name for p in (repo_path / "docs" / "solutions").rglob("*.md")))
    )
    run_id = iris.stable_run_id(args.pipeline_ver, fingerprint)
    run_g = ds.graph(iris.run_graph(run_id))

    print(f"== semantic ingest (run {run_id}) ==")
    sem_stats = semantic.add_semantic(run_g, repo_path, args.repo_slug, run_id,
                                      pipeline_ver=args.pipeline_ver)
    for k, v in sem_stats.items():
        print(f"   {k}: {v}")

    # ---- tracker ingest (opt-in; Linear issues + comment learnings) ----
    if args.tracker or args.tracker_data:
        from . import tracker
        tr_run_id = f"tracker-{run_id}"
        tr_run_g = ds.graph(iris.run_graph(tr_run_id))
        print(f"== tracker ingest (run {tr_run_id}) ==")
        issues_override = None
        if args.tracker_data:
            with open(args.tracker_data, encoding="utf-8") as _f:
                issues_override = json.load(_f)
            if os.environ.get("LINEAR_API_KEY"):
                print("tracker: LINEAR_API_KEY set but --tracker-data given — file wins")
        tr_stats = tracker.add_tracker(spine_g, tr_run_g, args.repo_slug, tr_run_id,
                                       pipeline_ver=args.pipeline_ver,
                                       issues_override=issues_override)
        for k, v in tr_stats.items():
            print(f"   {k}: {v}")

    # ---- secondary repos (skills, …) into the SAME graph, cross-linked ----
    if args.secondary or args.repos_root:
        for entry in (_src_cfg.get("secondary_repos") or []):
            sec_path = secondary_repo_path(entry, args.repos_root)
            if not (sec_path / ".git").exists():
                where = "not under --repos-root" if args.repos_root else "not a clone"
                print(f"== secondary repo SKIPPED ({where}): {sec_path} ==")
                continue
            sec_slug = entry["slug"]
            print(f"== secondary spine ingest: {sec_slug} ({sec_path}) ==")
            try:
                ss = spine.add_spine(spine_g, sec_path, sec_slug,
                                     max_commits=max_commits, max_prs=args.max_prs,
                                     docs_url=entry.get("docs_url"),
                                     doc_exclude=entry.get("doc_exclude"))
                for k, v in ss.items():
                    print(f"   {k}: {v}")
            except spine.PRCommentsIncomplete as exc:
                for k, v in exc.stats.items():
                    print(f"   {k}: {v}")
                print("KG_PR_COMMENTS_INCOMPLETE")
                return 1
            sec_fp = iris.content_hash(sec_slug)
            sec_run_id = iris.stable_run_id(args.pipeline_ver, sec_fp)
            sec_run_g = ds.graph(iris.run_graph(sec_run_id))
            sem = semantic.add_semantic(sec_run_g, sec_path, sec_slug, sec_run_id,
                                        pipeline_ver=args.pipeline_ver)
            print("   semantic: " + ", ".join(f"{k}={v}" for k, v in sem.items()))

    # ---- self-ingestion: the KG repo itself as a source (sources.yml self_ingest) ----
    # Lets the graph answer questions about its OWN internals (ingest design,
    # query tools, past changes) — the KG knows itself. Off by default.
    from . import sources as _sources_mod
    _cfg = _sources_mod.load()
    if _cfg.get("self_ingest"):
        self_root = OUT_DIR.parent
        try:
            origin = subprocess.run(["git", "-C", str(self_root), "remote", "get-url", "origin"],
                                    capture_output=True, text=True, check=True).stdout.strip()
            self_slug = "/".join(origin.removesuffix(".git").split("/")[-2:])
        except Exception:
            self_slug = "local/knowledge-graph"
        print(f"== self-ingest: {self_slug} ({self_root}) ==")
        try:
            ss = spine.add_spine(spine_g, self_root, self_slug,
                                 max_commits=max_commits, max_prs=args.max_prs)
            for k, v in ss.items():
                print(f"   {k}: {v}")
        except spine.PRCommentsIncomplete as exc:
            for k, v in exc.stats.items():
                print(f"   {k}: {v}")
            print("KG_PR_COMMENTS_INCOMPLETE")
            return 1
        self_fp = iris.content_hash(f"self:{self_slug}")
        self_run_id = iris.stable_run_id(args.pipeline_ver, self_fp)
        self_run_g = ds.graph(iris.run_graph(self_run_id))
        sem_self = semantic.add_semantic(self_run_g, self_root, self_slug, self_run_id,
                                         pipeline_ver=args.pipeline_ver)
        print("   semantic: " + ", ".join(f"{k}={v}" for k, v in sem_self.items()))

    # ---- graph age stamp: when was this data current? ----
    # Stamped at INGEST (not materialize) so the date lands in the committed
    # snapshot parts and survives snapshot -> materialize — a deployed graph
    # answers its own age via existing tools (kg_neighbors on the spine IRI).
    # `set` keeps exactly one stamp; a stamp at materialize time would lie
    # after every no-op redeploy.
    stamp = Literal(datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    datatype=XSD.dateTime)
    spine_g.set((iris.G_SPINE, DCTERMS.modified, stamp))
    print(f"== graph age stamp: {stamp} ==")

    # ---- serialize named graphs (TriG preserves the spine/run split) ----
    trig_path = OUT_DIR / "graph.trig"
    ds.serialize(destination=str(trig_path), format="trig")
    total = sum(1 for _ in ds.quads((None, None, None, None)))
    print(f"== serialized {total} quads -> {trig_path} ==")

    # ---- SHACL validation over the union ----
    union = Graph()
    for s, p, o, _ in ds.quads((None, None, None, None)):
        union.add((s, p, o))
    shapes = ontology.load("shapes.ttl")
    onto = ontology.load("kg.ttl")
    union += onto  # class hierarchy available to validation
    print("== SHACL validation ==")
    conforms, _rg, rtext = validate(
        union, shacl_graph=shapes, inference="rdfs",
        abort_on_first=False, meta_shacl=False,
    )
    print(f"   conforms: {conforms}")
    if not conforms:
        print(rtext[:4000])

    # ---- demo SPARQL: property-path traversal (the Stardog-native win) ----
    print("== demo queries (union graph) ==")
    _demo_queries(union)

    # ---- rebuild the semantic embeddings sidecar (BEFORE the snapshot, so the
    # digest's Semantic-index line reflects THIS run's vectors). Default on;
    # skipped gracefully if fastembed isn't installed so core ingest stays light.
    if args.embed:
        try:
            from . import embed as embed_mod
            from kg_query.store import RdflibStore
            e = embed_mod.build_embeddings(RdflibStore(trig_path),
                                              snapshot_dir=SNAP_DIR)
            if e.get("skipped"):
                print(f"== embeddings skipped: {e['skipped']} ==")
            else:
                print(f"== embeddings -> {e['count']} cards in {e.get('batch_count', '?')} batches ({e['model']}, dim {e['dim']}) ==")
        except ImportError as exc:
            print(f"== embeddings SKIPPED (fastembed not installed: {exc}); "
                  f"semantic/hybrid search will be stale — `pip install fastembed` "
                  f"or pass --no-embed to silence ==")

    # ---- committed, git-diffable snapshot (compact digest + per-type parts) ----
    snap = snapshot.write_snapshot(union, SNAP_DIR, ds)
    print(f"== snapshot -> {SNAP_DIR.name}/digest.md + {snap['part_files']} parts ==")

    return 0 if conforms else 1


def _refresh_stats(duration_sec: float) -> dict:
    """Build stats dict from output files after a refresh run."""
    parts: dict[str, int] = {}
    parts_dir = SNAP_DIR / "parts"
    if parts_dir.exists():
        for p in sorted(parts_dir.glob("*.nt")):
            try:
                count = sum(
                    1 for line in p.open(encoding="utf-8")
                    if line.strip() and not line.startswith("#")
                )
            except Exception:
                count = 0
            parts[p.name] = count

    vectors = 0
    meta_path = OUT_DIR / "embeddings.meta.json"
    if meta_path.exists():
        try:
            with open(meta_path, encoding="utf-8") as f:
                vectors = json.load(f).get("count", 0)
        except Exception:
            pass

    return {
        "quads": sum(parts.values()),
        "vectors": vectors,
        "docPages": parts.get("docpage.nt", 0),
        "durationSec": duration_sec,
        "parts": parts,
    }


def refresh(argv=None) -> int:
    """One-command ingest: reads sources.yml, takes two inputs.

    Usage: python -m kg_ingest refresh --code-repo <path> --tracker-data <file> [--no-embed]

    Resolves --repo-slug from sources.yml automatically. --tracker-data implies
    tracker ingest with no LINEAR_API_KEY required; if LINEAR_API_KEY is also set
    the file wins. Prints one JSON stats line as the last stdout line.
    """
    import time
    ap = argparse.ArgumentParser(
        prog="kg-ingest refresh",
        description=(
            "Full ingest from sources.yml with two inputs: the code-repo path and "
            "a pipeline-fetched tracker-data file. Resolves repo-slug, tracker config, "
            "and doc globs from sources.yml automatically."
        ),
    )
    ap.add_argument("--code-repo", required=True, metavar="PATH",
                    help="path to the code repo to ingest (spine + semantic layer)")
    ap.add_argument("--tracker-data", default=None, metavar="FILE",
                    help="path to a pipeline-fetched tracker-data.json; "
                         "no LINEAR_API_KEY needed when given")
    ap.add_argument("--repos-root", default=None, metavar="DIR",
                    help="directory holding clones of sources.yml secondary_repos as "
                         "<DIR>/<basename(slug)>; entries absent there are skipped")
    ap.add_argument("--no-embed", dest="embed", action="store_false", default=True,
                    help="skip the embedding sidecar rebuild")
    ap.add_argument("--no-docs-sites", dest="docs_sites", action="store_false", default=True,
                    help="skip the docs_sites crawl (e.g. offline / CI unit tests)")
    ap.add_argument("--pipeline-ver", default="0.1.0", help=argparse.SUPPRESS)
    ap.add_argument("--max-prs", type=int, default=200, help=argparse.SUPPRESS)
    ap.add_argument("--max-commits", type=int, default=0, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    from . import sources as _src_mod
    _src_cfg = _src_mod.load()
    repo_slug = (_src_cfg.get("code_repo") or {}).get("slug") or "local/repo"

    build_argv = [
        "--repo", args.code_repo,
        "--repo-slug", repo_slug,
        "--pipeline-ver", args.pipeline_ver,
        "--max-prs", str(args.max_prs),
        "--max-commits", str(args.max_commits),
    ]
    if args.tracker_data:
        build_argv += ["--tracker-data", args.tracker_data]
    if args.repos_root:
        build_argv += ["--repos-root", args.repos_root]
    if not args.embed:
        build_argv.append("--no-embed")
    if not args.docs_sites:
        build_argv.append("--no-docs-sites")

    t0 = time.monotonic()
    rc = main(build_argv)
    duration = round(time.monotonic() - t0, 2)

    print(json.dumps(_refresh_stats(duration)))
    return rc


def _demo_queries(g: Graph) -> None:
    kgp = f"PREFIX kg: <{iris.KG}>"
    q_counts = kgp + """
      SELECT ?cls (COUNT(?s) AS ?n) WHERE { ?s a ?cls } GROUP BY ?cls ORDER BY DESC(?n)
    """
    print("  node counts by type:")
    for row in g.query(q_counts):
        print(f"     {row.n:>5}  {row.cls.split('#')[-1]}")

    # provenance property-path: learnings reachable to their source doc
    q_prov = kgp + """
      PREFIX prov: <http://www.w3.org/ns/prov#>
      SELECT (COUNT(DISTINCT ?l) AS ?n) WHERE {
        ?l a kg:Learning ; prov:wasDerivedFrom+ ?src . ?src a kg:Doc .
      }
    """
    for row in g.query(q_prov):
        print(f"  learnings with a provenance path to a Doc: {row.n}")

    # most-used topics (promoted tags)
    q_topics = kgp + """
      SELECT ?t (COUNT(?l) AS ?n) WHERE { ?l kg:tagged ?t } GROUP BY ?t ORDER BY DESC(?n) LIMIT 8
    """
    print("  top topics:")
    for row in g.query(q_topics):
        print(f"     {row.n:>4}  {row.t.split('/')[-1]}")


if __name__ == "__main__":
    sys.exit(main())
