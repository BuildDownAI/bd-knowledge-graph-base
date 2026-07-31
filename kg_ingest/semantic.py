"""Semantic ingester — DETERMINISTIC parse of the already-structured corpora
(recon §2.1/§2.2). No LLM pass (that stays deferred per the brief); this reads
frontmatter + L-NNNN headings, which are structured enough to parse directly.

Every semantic node carries mandatory provenance:
  prov:wasDerivedFrom -> the source Doc (spine)   [>=1, SHACL-enforced]
  prov:wasGeneratedBy -> the ExtractionRun         [exactly 1, SHACL-enforced]
"""
from __future__ import annotations

import re
from pathlib import Path

import frontmatter
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, SKOS, XSD, Namespace

from . import iris, normalize

KG = iris.KG
PROV = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")


def _bind(g: Graph) -> None:
    g.bind("kg", KG); g.bind("kgr", iris.KGR); g.bind("prov", PROV)
    g.bind("dcterms", DCTERMS); g.bind("skos", SKOS)


def _topic(g: Graph, tag: str) -> URIRef:
    t = iris.topic(tag)
    g.add((t, RDF.type, KG.Topic))
    g.add((t, SKOS.prefLabel, Literal(tag)))
    return t


def _run_node(g: Graph, run_id: str, pipeline_ver: str, model: str,
              prompt_hash: str, notes: str) -> URIRef:
    r = URIRef(f"{iris.KGR}run/{run_id}")
    g.add((r, RDF.type, KG.ExtractionRun))
    g.add((r, KG.pipelineVer, Literal(pipeline_ver)))
    g.add((r, KG.model, Literal(model)))
    g.add((r, KG.promptHash, Literal(prompt_hash)))
    if notes:
        g.add((r, KG.notes, Literal(notes)))
    return r


def add_semantic(g: Graph, repo_path: Path, repo_slug: str, run_id: str,
                 pipeline_ver: str = "0.1.0",
                 model: str = "deterministic-parser",
                 prompt_hash: str = "n/a-deterministic",
                 notes: str = "Phase-1 deterministic frontmatter/heading parse") -> dict:
    """Populate `g` (the run named graph) with semantic nodes + provenance."""
    _bind(g)
    repo_path = Path(repo_path)
    run = _run_node(g, run_id, pipeline_ver, model, prompt_hash, notes)
    stats = {"solutions": 0, "decisions": 0, "plans": 0,
             "topics": set(), "issues": 0}

    # ---------- docs/solutions/ : structured frontmatter ----------
    sol_dir = repo_path / "docs" / "solutions"
    for md in sorted(sol_dir.rglob("*.md")):
        rel = md.relative_to(repo_path).as_posix()
        try:
            post = frontmatter.load(md)
        except Exception:
            continue
        fm = post.metadata or {}
        if not fm:
            continue  # 2 malformed preamble-leak files fall out here (recon §2.5)
        norm = normalize.normalize_frontmatter(fm)
        lid = norm.get("learning_id") or ("sol:" + md.stem)
        node = iris.learning(repo_slug, "solutions", lid)
        doc_iri = iris.doc(repo_slug, rel)

        g.add((node, RDF.type, KG.Learning))
        title = norm.get("title") or post.metadata.get("title") or md.stem
        g.add((node, DCTERMS.title, Literal(str(title), datatype=XSD.string)))
        # mandatory provenance
        g.add((node, PROV.wasDerivedFrom, doc_iri))
        g.add((node, PROV.wasGeneratedBy, run))
        # scalar props
        for field, pred in (("status", KG.status), ("category", KG.category),
                            ("priority", KG.priority), ("severity_level", KG.severityLevel),
                            ("symptom", KG.symptom), ("root_cause", KG.rootCause),
                            ("impact", KG.impact), ("fix", KG.fix),
                            ("problem_type", KG.problemType),
                            ("resolution_type", KG.resolutionType)):
            v = norm.get(field)
            if v:
                g.add((node, pred, Literal(str(v)[:2000], datatype=XSD.string)))
        if norm.get("learning_id"):
            g.add((node, KG.learningId, Literal(norm["learning_id"])))
        if norm.get("found_date"):
            g.add((node, KG.foundDate, Literal(norm["found_date"], datatype=XSD.date)))
        if norm.get("component"):
            comp = norm["component"]
            comp = ", ".join(map(str, comp)) if isinstance(comp, (list, tuple)) else str(comp)
            g.add((node, KG.affectsComponent, Literal(comp[:500])))
        # tags -> topics
        for tag in norm["tags"]:
            g.add((node, KG.tagged, _topic(g, tag)))
            stats["topics"].add(tag)
        # cross-links: GitHub #issues -> filedAs (stubs), prs -> relatedPr
        for num in norm["issues"]:
            iss = iris.issue(repo_slug, num)
            g.add((iss, RDF.type, KG.Issue))
            g.add((iss, KG.number, Literal(int(num), datatype=XSD.integer)))
            g.add((node, KG.filedAs, iss))
            stats["issues"] += 1
        for num in norm["prs"]:
            g.add((node, KG.relatedPr, iris.pr(repo_slug, num)))
        # tracker keys (PROJ-123, …) -> the SAME issue node the tracker ingester
        # and the spine reference-scanner use.
        for key in norm["tracker"]:
            iss = iris.tracker_issue(key)
            g.add((iss, RDF.type, KG.Issue))
            g.add((iss, KG.trackerKey, Literal(key)))
            g.add((node, KG.filedAs, iss))
        # doc -> doc related_solutions (by basename; resolve within solutions dir)
        for target in norm["related_solutions"]:
            tp = _resolve_solution(sol_dir, repo_path, target)
            if tp:
                g.add((node, KG.relatedSolution, iris.doc(repo_slug, tp)))
        stats["solutions"] += 1

    # ---------- docs/adr/ : Architecture Decision Records -> Decision ----------
    for md in sorted((repo_path / "docs" / "adr").glob("*.md")):
        _add_adr(g, repo_path, repo_slug, run, md, stats)

    # ---------- docs/plans + docs/superpowers/plans : plan docs -> Decision ----------
    for plans_dir in (repo_path / "docs" / "plans",
                      repo_path / "docs" / "superpowers" / "plans"):
        for md in sorted(plans_dir.glob("*.md")):
            _add_plan(g, repo_path, repo_slug, run, md, stats)

    stats["topics"] = len(stats["topics"])
    return stats


_ADR_TITLE = re.compile(r"^#\s+ADR\s+0*(\d+)\s*[:\-]\s*(.+)$", re.M)
_ADR_FIELD = re.compile(r"^\*\*(Status|Date|References)\:\*\*\s*(.+)$", re.M | re.I)


def _add_adr(g: Graph, repo_path: Path, repo_slug: str, run: URIRef,
             md: Path, stats: dict) -> None:
    """docs/adr/NNN-*.md -> a Decision node (title, status, referenced issues)."""
    rel = md.relative_to(repo_path).as_posix()
    doc_iri = iris.doc(repo_slug, rel)
    text = md.read_text(encoding="utf-8", errors="replace")
    tm = _ADR_TITLE.search(text)
    adr_id = tm.group(1) if tm else md.stem
    title = (tm.group(2).strip() if tm else md.stem)[:300]
    node = URIRef(f"{iris.KGR}decision/{iris._seg(repo_slug)}/adr/{adr_id}")
    g.add((node, RDF.type, KG.Decision))
    g.add((node, DCTERMS.title, Literal(f"ADR {adr_id}: {title}", datatype=XSD.string)))
    g.add((node, PROV.wasDerivedFrom, doc_iri))
    g.add((node, PROV.wasGeneratedBy, run))
    fields = {m.group(1).lower(): m.group(2).strip() for m in _ADR_FIELD.finditer(text)}
    if fields.get("status"):
        st = normalize.normalize_status(fields["status"].split()[0]) or fields["status"].lower()
        g.add((node, KG.status, Literal(st)))
    if fields.get("references"):
        for key in normalize.parse_tracker_keys(fields["references"]):
            iss = iris.tracker_issue(key)
            g.add((iss, RDF.type, KG.Issue))
            g.add((iss, KG.trackerKey, Literal(key)))
            g.add((node, KG.filedAs, iss))
    stats["decisions"] = stats.get("decisions", 0) + 1


def _add_plan(g: Graph, repo_path: Path, repo_slug: str, run: URIRef,
              md: Path, stats: dict) -> None:
    """A plan doc with frontmatter -> a Decision node (design intent)."""
    rel = md.relative_to(repo_path).as_posix()
    try:
        post = frontmatter.load(md)
        fm = post.metadata or {}
        content = post.content
    except Exception:
        # malformed YAML frontmatter (e.g. an unquoted colon) — don't lose the
        # whole plan doc; fall back to a stem-title Decision with no fm fields.
        fm, content = {}, md.read_text(encoding="utf-8", errors="replace")
    title = str(fm.get("title") or md.stem)[:300]
    node = URIRef(f"{iris.KGR}decision/{iris._seg(repo_slug)}/plan/{iris._seg(md.stem)}")
    g.add((node, RDF.type, KG.Decision))
    g.add((node, DCTERMS.title, Literal(title, datatype=XSD.string)))
    g.add((node, PROV.wasDerivedFrom, iris.doc(repo_slug, rel)))
    g.add((node, PROV.wasGeneratedBy, run))
    if fm.get("status"):
        g.add((node, KG.status, Literal(normalize.normalize_status(fm["status"]) or str(fm["status"]))))
    if fm.get("type"):
        g.add((node, KG.category, Literal(str(fm["type"]))))
    d = normalize.parse_date(fm.get("date"))
    if d:
        g.add((node, KG.foundDate, Literal(d, datatype=XSD.date)))
    # tracker keys mentioned anywhere in the plan body -> filedAs links
    for key in set(normalize.parse_tracker_keys(content)):
        iss = iris.tracker_issue(key)
        g.add((iss, RDF.type, KG.Issue))
        g.add((iss, KG.trackerKey, Literal(key)))
        g.add((node, KG.filedAs, iss))
    stats["plans"] = stats.get("plans", 0) + 1


def _resolve_solution(sol_dir: Path, repo_path: Path, target: str) -> str | None:
    """Map a related_solutions value (basename or relpath) to a repo-relative path."""
    target = target.strip()
    cand = repo_path / target
    if cand.exists():
        return cand.relative_to(repo_path).as_posix()
    base = Path(target).name
    for p in sol_dir.rglob(base):
        return p.relative_to(repo_path).as_posix()
    return None
