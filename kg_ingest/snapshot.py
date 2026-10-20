"""Snapshot writer — compact, git-diffable views of the graph.

The runtime graph (`out/graph.trig`, ~1.8 MB) is a gitignored build artifact:
one monolithic file that rewrites wholesale every ingest. That is fine for the
MCP server to load, but useless to review in git. This module writes two SMALL,
COMMITTED artifacts alongside it so data changes are reviewable over time:

  * snapshot/digest.md     — human-readable summary + a one-line-per-issue
                             inventory (status, labels, parent, learnings), so a
                             status change or a new learning is a clean 1-line diff.
  * snapshot/parts/<t>.nt  — the whole graph split BY SUBJECT TYPE (issue,
                             comment, file, commit, pr, topic, …) into sorted
                             N-Triples. A normal tracker refresh only touches
                             issue.nt / comment.nt; a code change only touches
                             file.nt / commit.nt / pr.nt. Unchanged parts serialize
                             byte-identically, so git shows nothing for them.
                             `cat snapshot/parts/*.nt` reconstitutes the graph, so
                             the parts also serve as a checked-in, diffable backup.

                             Large parts (> _PART_SIZE_CAP, default 50 MB) are
                             written as numbered pieces: <type>.<NN>.nt (e.g.
                             commit.00.nt, commit.01.nt).  Every triple for a given
                             subject IRI always lands in the same piece — chosen by
                             hashlib.sha256(iri).digest()[0] % k, where k is the
                             smallest power-of-two that keeps every piece under the
                             cap.  Readers that glob *.nt pick up all pieces with no
                             change.  Guard rules use the logical name (<type>.nt)
                             by aggregating piece line counts before comparison.

Determinism is the whole point: NO timestamps, everything sorted. The graph has
no blank nodes (stable IRIs from stable keys), so identical data => identical bytes.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import RDF

from .iris import KG as _KG_NS, KGR as _KGR_NS

RESOURCE = str(_KGR_NS)
KG = str(_KG_NS)

_PART_SIZE_CAP = 50 * 1024 * 1024        # 50 MB: pieces keep GitHub's 100 MB limit safe
_POST_WRITE_FAIL_THRESHOLD = 95 * 1024 * 1024  # 95 MB: fail fast before push


# --------------------------------------------------------------------------- #
# parts/<subject-type>.nt  — the graph broken up so only changed parts diff
# --------------------------------------------------------------------------- #
def _subject_type(s) -> str:
    """Bucket a triple by the resource kind of its subject (stable, IRI-derived)."""
    u = str(s)
    if u.startswith(RESOURCE):
        seg = u[len(RESOURCE):].split("/", 1)[0]
        return seg or "other"
    if u.startswith(KG) or "#" in u:
        return "onto"
    return "other"


def _subject_from_nt_line(line: str) -> str:
    """Extract the subject term (IRI or blank node) from an N-Triples line."""
    if line.startswith('<'):
        return line[:line.index('>') + 1]
    return line.split(' ', 1)[0]


def write_parts(union: Graph, parts_dir: Path, _cap: int = _PART_SIZE_CAP) -> dict[str, int]:
    """Split `union` into snapshot/parts/<type>.nt, each sorted for minimal diffs.

    When a type's serialized size exceeds _cap (default 50 MB), the type is written
    as numbered pieces <type>.<NN>.nt using a stable SHA-256 hash of each subject IRI
    modulo k (smallest power-of-two that keeps every piece under _cap).

    Clears stale *.nt first so a type that disappears doesn't linger.
    Returns {type_name: total_line_count} with logical names (no piece suffixes).
    """
    parts_dir.mkdir(parents=True, exist_ok=True)
    for old in parts_dir.glob("*.nt"):
        old.unlink()

    by_type: dict[str, Graph] = defaultdict(Graph)
    for s, p, o in union:
        by_type[_subject_type(s)].add((s, p, o))

    written: dict[str, int] = {}
    for t, g in by_type.items():
        all_lines = sorted(l for l in g.serialize(format="nt").splitlines() if l.strip())
        content = "\n".join(all_lines) + "\n"

        if len(content.encode("utf-8")) <= _cap:
            (parts_dir / f"{t}.nt").write_text(content)
            written[t] = len(all_lines)
            continue

        # Type exceeds cap: group triples by subject, then find smallest k (power of 2)
        # such that every piece stays under _cap.
        by_subject: dict[str, list[str]] = defaultdict(list)
        for line in all_lines:
            by_subject[_subject_from_nt_line(line)].append(line)

        k = 2
        pieces: dict[int, list[str]] = {}
        while k <= 256:
            pieces = defaultdict(list)
            for iri, triples in by_subject.items():
                idx = hashlib.sha256(iri.encode()).digest()[0] % k
                pieces[idx].extend(triples)

            if all(
                len(("\n".join(sorted(pieces[i])) + "\n").encode("utf-8")) <= _cap
                for i in pieces
            ):
                break
            k *= 2

        total = 0
        for idx in range(k):
            piece_lines = sorted(pieces.get(idx, []))
            if piece_lines:
                (parts_dir / f"{t}.{idx:02d}.nt").write_text("\n".join(piece_lines) + "\n")
                total += len(piece_lines)
        written[t] = total

    return dict(sorted(written.items()))


# --------------------------------------------------------------------------- #
# digest.md — compact human-readable summary + per-issue inventory
# --------------------------------------------------------------------------- #
def _q(g: Graph, sparql: str):
    return list(g.query(sparql, initNs={"kg": URIRef(KG), "rdf": RDF}))


def build_digest(union: Graph, ds: Dataset | None = None) -> str:
    L: list[str] = []
    L.append("# KG data digest")
    L.append("")
    L.append("Deterministic summary of `out/graph.trig` (regenerated by "
             "`python -m kg_ingest.snapshot`). No timestamps — diff this file "
             "across refreshes to see exactly what the data did.")
    L.append("")

    # ---- totals: named-graph sizes ----
    if ds is not None:
        L.append("## Named graphs (triples)")
        L.append("")
        rows = sorted((str(g.identifier), len(g)) for g in ds.graphs() if len(g))
        for iri, n in rows:
            L.append(f"- `{iri.split('/resource/graph/')[-1]}`: {n}")
        L.append("")

    # ---- node counts by type ----
    L.append("## Nodes by type")
    L.append("")
    for row in _q(union, """
        SELECT ?c (COUNT(?s) AS ?n) WHERE { ?s a ?c }
        GROUP BY ?c ORDER BY DESC(?n) ?c
    """):
        L.append(f"- {str(row.c).split('#')[-1]}: {int(row.n)}")
    L.append("")

    # ---- issues by board ----
    L.append("## Issues by board")
    L.append("")
    for row in _q(union, """
        SELECT ?team (COUNT(?i) AS ?n) WHERE { ?i a kg:Issue ; kg:trackerTeam ?team }
        GROUP BY ?team ORDER BY ?team
    """):
        L.append(f"- {row.team}: {int(row.n)}")
    L.append("")

    # ---- relationship edge counts ----
    L.append("## Relationship edges")
    L.append("")
    for pred in ("partOf", "dependsOn", "duplicateOf", "relatedTo", "inProject"):
        n = len(list(union.subject_objects(URIRef(KG + pred))))
        L.append(f"- {pred}: {n}")
    L.append("")

    # ---- learnings inventory (issue key + phase), sorted ----
    L.append("## Learnings (build-up / build-down comments)")
    L.append("")
    learn: list[str] = []
    for row in _q(union, """
        SELECT ?t WHERE { ?l a kg:Learning ; <http://purl.org/dc/terms/title> ?t }
    """):
        learn.append(str(row.t))
    for t in sorted(learn):
        L.append(f"- {t}")
    L.append("")

    # ---- topics with counts ----
    L.append("## Topics")
    L.append("")
    for row in _q(union, """
        SELECT ?p (COUNT(?l) AS ?n) WHERE {
          ?t a kg:Topic ; <http://www.w3.org/2004/02/skos/core#prefLabel> ?p .
          OPTIONAL { ?l kg:tagged ?t }
        } GROUP BY ?p ORDER BY DESC(?n) ?p
    """):
        L.append(f"- {row.p}: {int(row.n)}")
    L.append("")

    # ---- full issue inventory: one line per issue (the diff-friendly meat) ----
    L.append("## Issue inventory")
    L.append("")
    L.append("`KEY | status | project | labels | parent | learnings`")
    L.append("")
    # gather per-issue facts
    issues: dict[str, dict] = {}
    for row in _q(union, """
        SELECT ?k ?st ?parent ?proj WHERE {
          ?i a kg:Issue ; kg:trackerKey ?k .
          OPTIONAL { ?i kg:status ?st }
          OPTIONAL { ?i kg:partOf ?p . ?p kg:trackerKey ?parent }
          OPTIONAL { ?i kg:inProject ?pr . ?pr <http://purl.org/dc/terms/title> ?proj }
        }
    """):
        d = issues.setdefault(str(row.k), {"status": "", "parent": "", "proj": "",
                                           "labels": set(), "learn": set()})
        if row.st:
            d["status"] = str(row.st)
        if row.parent:
            d["parent"] = str(row.parent)
        if row.proj:
            d["proj"] = str(row.proj)
    for row in _q(union, """
        SELECT ?k ?lbl WHERE { ?i a kg:Issue ; kg:trackerKey ?k ; kg:label ?lbl }
    """):
        if str(row.k) in issues:
            issues[str(row.k)]["labels"].add(str(row.lbl))
    # learnings phase per issue key, parsed from the learning title
    for t in learn:
        key = t.split(" ", 1)[0]
        if key in issues:
            if "build-up" in t:
                issues[key]["learn"].add("up")
            if "build-down" in t:
                issues[key]["learn"].add("down")

    def _sortkey(k: str):
        team, _, num = k.partition("-")
        return (team, int(num) if num.isdigit() else 0)

    for k in sorted(issues, key=_sortkey):
        d = issues[k]
        labels = ",".join(sorted(d["labels"])) or "-"
        learn_s = ",".join(sorted(d["learn"])) or "-"
        L.append(f"- `{k}` | {d['status'] or '-'} | {d['proj'] or '-'} | "
                 f"[{labels}] | {d['parent'] or '-'} | {learn_s}")
    L.append("")

    # ---- semantic index (only if the embedding sidecar has been built) ----
    _meta = Path(__file__).resolve().parent.parent / "out" / "embeddings.meta.json"
    if _meta.exists():
        m = json.loads(_meta.read_text())
        L.append("## Semantic index")
        L.append("")
        L.append(f"- model: {m.get('model')}")
        L.append(f"- dim: {m.get('dim')}")
        L.append(f"- vectors: {m.get('count')}")
        L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def write_snapshot(union: Graph, snap_dir: Path, ds: Dataset | None = None) -> dict:
    # Snapshot the DATA graph only. When a Dataset is provided, derive the union
    # from its quads so the result never depends on what the caller folded into
    # `union` (the CLI adds the ontology for SHACL — that must not leak into the
    # snapshot; the ontology is tracked separately under ontology/). Guarantees
    # the CLI and standalone (`python -m kg_ingest.snapshot`) paths agree byte-for-byte.
    if ds is not None:
        union = Graph()
        for s, p, o, _ in ds.quads((None, None, None, None)):
            union.add((s, p, o))
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "digest.md").write_text(build_digest(union, ds))
    parts = write_parts(union, snap_dir / "parts")

    # Fail fast if any committed file still exceeds the safe limit after splitting.
    # A subject whose triples alone exceed _PART_SIZE_CAP cannot be split further;
    # the error names the file so the operator can act before the push is attempted.
    oversized = sorted(
        (f, f.stat().st_size)
        for f in snap_dir.rglob("*")
        if f.is_file() and f.stat().st_size > _POST_WRITE_FAIL_THRESHOLD
    )
    if oversized:
        items = "; ".join(
            f"{f.relative_to(snap_dir)} ({sz / 1024 / 1024:.1f} MB)"
            for f, sz in oversized
        )
        raise SystemExit(
            f"[snapshot] file(s) over 95 MB after write — ingest aborted: {items}. "
            f"A subject with more triples than the 50 MB piece cap cannot be split "
            f"further; raise _PART_SIZE_CAP or restructure the data."
        )

    return {"parts": parts, "part_files": len(parts)}


def _load_union_from_trig(trig: Path) -> tuple[Graph, Dataset]:
    ds = Dataset()
    ds.parse(str(trig), format="trig")
    union = Graph()
    for s, p, o, _ in ds.quads((None, None, None, None)):
        union.add((s, p, o))
    return union, ds


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    union, ds = _load_union_from_trig(root / "out" / "graph.trig")
    stats = write_snapshot(union, root / "snapshot", ds)
    print(f"wrote snapshot/digest.md + {stats['part_files']} parts:")
    for t, n in stats["parts"].items():
        print(f"   parts/{t}.nt: {n}")


if __name__ == "__main__":
    main()
