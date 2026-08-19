"""Build the graph, serialize named graphs (TriG), SHACL-validate, demo SPARQL.

Usage:
  python -m kg_ingest.cli --repo /path/to/your-project [--max-commits 0] [--max-prs 200]
                          [--tracker]   # also ingest Linear issues (needs LINEAR_API_KEY)
"""
from __future__ import annotations

import argparse
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


def _ingest_docs_sites(spine_g: Graph, docs_sites_cfg: list) -> dict:
    """Crawl docs_sites entries and emit DocSite/DocPage/DocSection triples into spine_g."""
    from .docsite import crawl_site, CrawlConfig

    KG = iris.KG
    total_pages = 0
    total_sections = 0

    for entry in docs_sites_cfg:
        root_url = (entry.get("url") or "").rstrip("/")
        if not root_url:
            continue

        config = CrawlConfig(
            root_url=root_url,
            max_depth=entry.get("max_depth", 4),
            max_pages=entry.get("max_pages", 300),
            include=list(entry.get("include") or []),
            exclude=list(entry.get("exclude") or []),
        )

        print(f"== docsite crawl: {root_url} ==")
        pages = crawl_site(config)

        site_iri = iris.doc_site(root_url)
        spine_g.add((site_iri, RDF.type, KG.DocSite))
        spine_g.add((site_iri, KG.rootUrl, Literal(root_url)))
        spine_g.add((site_iri, DCTERMS.title, Literal(root_url)))

        repo_slug = entry.get("repo")
        if repo_slug:
            repo_iri = iris.repo(repo_slug)
            spine_g.add((repo_iri, KG.docsUrl, Literal(root_url)))

        fetched_ats: list[str] = []
        for page in pages:
            # Anchor slugs must be unique per page: repeated headings ("Required",
            # "Required") would otherwise collide onto ONE DocSection IRI, merging
            # two sections' triples and double-emitting cards. Site generators
            # (Mintlify, GitHub) dedupe the same way: -2, -3, ... suffixes.
            _seen_anchors: dict[str, int] = {}
            page_iri = iris.doc_page(page.url)
            spine_g.add((page_iri, RDF.type, KG.DocPage))
            spine_g.add((page_iri, KG.url, Literal(page.url)))
            spine_g.add((page_iri, DCTERMS.title, Literal(page.title)))
            spine_g.add((page_iri, KG.contentHash, Literal(page.content_hash)))
            spine_g.add((page_iri, DCTERMS.modified,
                         Literal(page.fetched_at, datatype=XSD.dateTime)))
            spine_g.add((page_iri, KG.partOf, site_iri))
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
        print(f"   pages: {total_pages}, sections: {total_sections}")

    return {"docsite_pages": total_pages, "docsite_sections": total_sections}


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
    s_stats = spine.add_spine(spine_g, repo_path, args.repo_slug,
                              max_commits=max_commits, max_prs=args.max_prs,
                              docs_url=_code_repo_cfg.get("docs_url"))
    for k, v in s_stats.items():
        print(f"   {k}: {v}")

    # ---- docs_sites: crawl published documentation into the spine graph ----
    _docs_sites = _src_cfg.get("docs_sites") or []
    if _docs_sites:
        print("== docs_sites ingest ==")
        ds_stats = _ingest_docs_sites(spine_g, _docs_sites)
        for k, v in ds_stats.items():
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
    if args.tracker:
        from . import tracker
        tr_run_id = f"tracker-{run_id}"
        tr_run_g = ds.graph(iris.run_graph(tr_run_id))
        print(f"== tracker ingest (run {tr_run_id}) ==")
        tr_stats = tracker.add_tracker(spine_g, tr_run_g, args.repo_slug, tr_run_id,
                                       pipeline_ver=args.pipeline_ver)
        for k, v in tr_stats.items():
            print(f"   {k}: {v}")

    # ---- secondary repos (skills, …) into the SAME graph, cross-linked ----
    if args.secondary:
        for entry in (_src_cfg.get("secondary_repos") or []):
            # paths in sources.yml are relative to the KG repo root (this repo)
            p = Path(entry["path"])
            sec_path = p if p.is_absolute() else (OUT_DIR.parent / p).resolve()
            if not (sec_path / ".git").exists():
                print(f"== secondary repo SKIPPED (not a clone): {sec_path} ==")
                continue
            sec_slug = entry["slug"]
            print(f"== secondary spine ingest: {sec_slug} ({sec_path}) ==")
            ss = spine.add_spine(spine_g, sec_path, sec_slug,
                                 max_commits=max_commits, max_prs=args.max_prs,
                                 docs_url=entry.get("docs_url"))
            for k, v in ss.items():
                print(f"   {k}: {v}")
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
        ss = spine.add_spine(spine_g, self_root, self_slug,
                             max_commits=max_commits, max_prs=args.max_prs)
        for k, v in ss.items():
            print(f"   {k}: {v}")
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
            e = embed_mod.build_embeddings(RdflibStore(trig_path))
            if e.get("skipped"):
                print(f"== embeddings skipped: {e['skipped']} ==")
            else:
                print(f"== embeddings -> {e['count']} cards ({e['model']}, dim {e['dim']}) ==")
        except ImportError as exc:
            print(f"== embeddings SKIPPED (fastembed not installed: {exc}); "
                  f"semantic/hybrid search will be stale — `pip install fastembed` "
                  f"or pass --no-embed to silence ==")

    # ---- committed, git-diffable snapshot (compact digest + per-type parts) ----
    snap = snapshot.write_snapshot(union, SNAP_DIR, ds)
    print(f"== snapshot -> {SNAP_DIR.name}/digest.md + {snap['part_files']} parts ==")

    return 0 if conforms else 1


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
