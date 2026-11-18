"""Build the Stardog Knowledge Graph package from snapshot/parts/*.nt, offline (no token, no network).

Writes kit.yaml, ontology.ttl, data/<part>.nt (one named graph per part), data/cards.nt,
data/card-fields.nt and package.json into --out. The orchestrator imports the package.

Usage: kg-ingest package --out <dir> --database <name> [--parts-dir <dir>]
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import yaml
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from kg_query.store import NtPartsStore

from . import ontology, sources
from .cards import build_cards
from .iris import KG, KGR
from .materialize import SNAP_PARTS

SCHEMA = Namespace("https://schema.org/")
SYS = Namespace("http://system.stardog.com/")
SCHEMA_GRAPH = "tag:stardog:api:context:schema"
CARDS_GRAPH = f"{KGR}graph/cards"  # kg:cardText alone: the one graph Stardog embeds for semantic search
CARD_FIELDS_GRAPH = f"{KGR}graph/card-fields"  # title, type, snippet: returned with a hit, not embedded
KG_KEYS = ("group", "name", "version", "label", "description", "questions", "key_classes")


def card_graph(parts_dir: Path) -> Graph:
    """One index card per node, as the literals stardog_search.py searches and returns."""
    g = Graph()
    for c in build_cards(NtPartsStore(parts_dir)):
        node = URIRef(c["iri"])
        g.add((node, KG.cardText, Literal(c["card_text"])))
        g.add((node, KG.cardTitle, Literal(c["title"])))
        g.add((node, KG.cardType, Literal(c["type"])))
        if c["snippet"]:
            g.add((node, KG.cardSnippet, Literal(c["snippet"])))
    return g


def part_graph(part: Path) -> str:
    return f"{KGR}graph/part/{part.stem}"


class _Block(str):
    """Written as a YAML literal block (|): the description."""


yaml.SafeDumper.add_representer(_Block, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|"))


def build_package(out: Path, database: str, parts_dir: Path = SNAP_PARTS) -> Path:
    """Write the Knowledge Graph package into `out` (emptied first). Returns `out`."""
    parts = sorted(parts_dir.glob("*.nt"))
    if not parts:
        raise SystemExit(f"[package] no snapshot parts found in {parts_dir}")
    kg = sources.load().get("knowledge_graph")
    if not isinstance(kg, dict):
        raise SystemExit("[package] sources.yml has no knowledge_graph block")
    for key in KG_KEYS:
        if not kg.get(key):
            raise SystemExit(f"[package] sources.yml knowledge_graph.{key} is missing or empty")
    shutil.rmtree(out, ignore_errors=True)
    (out / "data").mkdir(parents=True)

    onto = ontology.load("kg.ttl")
    # Under reasoning rdfs:domain/range infer types nobody wants, at a cost; the platform reads the schema.org forms.
    for rdfs_p, schema_p in ((RDFS.domain, SCHEMA.domainIncludes), (RDFS.range, SCHEMA.rangeIncludes)):
        for s, o in list(onto.subject_objects(rdfs_p)):
            onto.remove((s, rdfs_p, o))
            onto.add((s, schema_p, o))
    onto.bind("schema", SCHEMA)
    onto.add((URIRef(str(KG)), RDF.type, SYS.DataModel))  # an ontology is a sys:DataModel node
    onto.add((URIRef(str(KG)), RDFS.label, Literal(kg["label"])))
    onto.serialize(out / "ontology.ttl", format="turtle")

    data = [{"file": "ontology.ttl", "graph": SCHEMA_GRAPH}]
    for part in parts:
        shutil.copyfile(part, out / "data" / part.name)
        data.append({"file": f"data/{part.name}", "graph": part_graph(part)})
    cards, fields = Graph(), Graph()
    for t in card_graph(parts_dir):
        (cards if t[1] == KG.cardText else fields).add(t)
    cards.serialize(out / "data" / "cards.nt", format="nt", encoding="utf-8")
    fields.serialize(out / "data" / "card-fields.nt", format="nt", encoding="utf-8")
    data += [{"file": "data/cards.nt", "graph": CARDS_GRAPH}, {"file": "data/card-fields.nt", "graph": CARD_FIELDS_GRAPH}]

    questions = list(kg["questions"])[:10]  # also the spotlight questions on chat home
    description = (kg["description"].rstrip() + "\n\n### Questions you can ask\n" + "".join(f"- {q}\n" for q in questions)
                   + "\n### Key classes\n" + ", ".join(kg["key_classes"]) + "\n")
    alias = kg["name"].replace("-", "_")
    manifest = {
        "name": kg["name"],
        "group": kg["group"],
        "version": str(kg["version"]),
        "label": kg["label"],
        "description": _Block(description),
        "options": {"database.name": database, "search.enabled": True, "graph.aliases": True,
                    "voicebox.enabled": True, "voicebox.example.questions": questions,
                    "search.index.contexts.excluded": True,
                    "search.semantic.index.contexts.filter": [part_graph(p) for p in parts] + [CARD_FIELDS_GRAPH]},
        "namespaces": {"kg": str(KG), "kgr": str(KGR), "schema": str(SCHEMA), "sys": str(SYS)},
        "alias": alias,
        "data": data,
    }
    (out / "kit.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")

    # package.json, read by the orchestrator. expectedTriples = the ontology triples
    # plus the lines of every data/*.nt file, as Stardog counts after the import.
    triples = len(onto) + sum(
        sum(1 for line in (out / item["file"]).read_text(encoding="utf-8").splitlines() if line.strip())
        for item in data if item["file"].endswith(".nt"))
    (out / "package.json").write_text(json.dumps(
        {"name": kg["name"], "version": str(kg["version"]), "alias": alias, "expectedTriples": triples}), encoding="utf-8")
    print(f"[package] {kg['name']} {len(parts)} parts, expectedTriples={triples}")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="kg-ingest package",
                                description="Build the Stardog KG package from snapshot/parts/, offline.")
    p.add_argument("--out", required=True, type=Path, help="Output directory (emptied first)")
    p.add_argument("--database", required=True, help="Stardog database name (written into kit.yaml)")
    p.add_argument("--parts-dir", type=Path, default=SNAP_PARTS, help="Parts directory (default: snapshot/parts/)")
    args = p.parse_args(argv)
    build_package(args.out, args.database, args.parts_dir)
